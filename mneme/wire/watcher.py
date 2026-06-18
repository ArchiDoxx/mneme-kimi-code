"""Filesystem watcher for Kimi CLI session directories."""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from pathlib import Path

from loguru import logger
from watchdog.events import FileSystemEvent, FileSystemEventHandler

# Cross-platform observer selection
if sys.platform == "win32":
    # Native Windows API observer — most efficient and stable on Windows
    from watchdog.observers.read_directory_changes import WindowsApiObserver as PlatformObserver
elif sys.platform == "darwin":
    # Native macOS FSEvents observer
    from watchdog.observers.fsevents import FSEventsObserver as PlatformObserver
elif sys.platform.startswith("linux"):
    # Native Linux inotify observer
    from watchdog.observers.inotify import InotifyObserver as PlatformObserver
else:
    # Fallback polling for other platforms (iOS, BSD, etc.)
    from watchdog.observers.polling import PollingObserver as PlatformObserver

from mneme.wire.indexer import WireIndexer
from mneme.wire.reader import (
    SessionIdentity,
    SessionReader,
    iter_session_wires,
    load_workdir_map,
    resolve_session_identity,
)


class _WireEventHandler(FileSystemEventHandler):
    """Handle filesystem events for wire.jsonl and state.json."""

    def __init__(self, watcher: SessionWatcher) -> None:
        self.watcher = watcher

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.name == "wire.jsonl":
            self.watcher._on_wire_changed(path)
        elif path.name == "state.json":
            self.watcher._on_state_changed(path.parent)

    def on_created(self, event: FileSystemEvent) -> None:
        # Directory creation is covered by the recursive observer; the
        # wire.jsonl / state.json file events do the actual work.
        if event.is_directory:
            return
        self.on_modified(event)


_global_watcher: SessionWatcher | None = None
_global_lock = threading.Lock()


def get_global_watcher(db_path: str | None = None) -> SessionWatcher:
    """Get or create the global singleton watcher."""
    global _global_watcher
    with _global_lock:
        if _global_watcher is None:
            _global_watcher = SessionWatcher(db_path)
        return _global_watcher


def stop_global_watcher() -> None:
    """Stop the global singleton watcher."""
    global _global_watcher
    with _global_lock:
        if _global_watcher is not None:
            _global_watcher.stop()
            _global_watcher = None


class SessionWatcher:
    """Watch ~/.kimi-code/sessions/ and index wire data in real time."""

    def __init__(self, db_path: str | None = None) -> None:
        self.sessions_dir = Path.home() / ".kimi-code" / "sessions"
        self.indexer = WireIndexer(db_path)
        self._readers: dict[str, SessionReader] = {}
        self._lock = threading.Lock()
        self._observer: PlatformObserver | None = None
        self._running = False
        self.on_ingest: Callable[[str, dict[str, int]], None] | None = None
        # Normalized session directory path -> real working directory,
        # loaded from ~/.kimi-code/session_index.jsonl.
        self._workdir_map = load_workdir_map()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start watching."""
        if self._running:
            return
        self._running = True

        if self.sessions_dir.exists():
            handler = _WireEventHandler(self)
            self._observer = PlatformObserver()
            self._observer.schedule(handler, str(self.sessions_dir), recursive=True)
            self._observer.start()
            logger.info(
                f"SessionWatcher started on {self.sessions_dir} "
                f"(observer: {type(self._observer).__name__})"
            )
            # NOTE: Background scan disabled by default. It causes server
            # instability on large databases (40k+ observations). Sessions
            # are indexed lazily when accessed via API or when new wire
            # events arrive via the filesystem watcher.
            # threading.Thread(target=self._scan_all, daemon=True).start()

    def stop(self) -> None:
        """Stop watching."""
        self._running = False
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
            logger.info("SessionWatcher stopped")

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _scan_all(self) -> None:
        """Scan recent existing sessions on startup (lazy — skips old sessions)."""
        import time

        logger.info("Starting background scan of recent sessions...")
        try:
            if not self.sessions_dir.exists():
                logger.info("Sessions directory does not exist, skipping scan")
                return

            # Only scan sessions modified in the last 7 days to avoid
            # blocking startup with huge historical backlogs.
            cutoff = time.time() - (7 * 24 * 3600)
            count = 0
            skipped = 0
            self._workdir_map = load_workdir_map()

            for identity in iter_session_wires(self.sessions_dir, self._workdir_map):
                try:
                    if identity.wire_path.stat().st_mtime < cutoff:
                        skipped += 1
                        continue
                except OSError:
                    pass
                try:
                    self._register_session(identity)
                    count += 1
                except Exception:
                    logger.exception(f"Failed to register session {identity.session_id}")

            logger.info(
                f"Background scan complete: {count} sessions registered, "
                f"{skipped} old sessions skipped (cutoff: 7 days)"
            )
        except Exception:
            logger.exception("Background scan failed")

    def _is_session_dir(self, path: Path) -> bool:
        """Check if path looks like a session directory."""
        return path.is_dir() and (path / "wire.jsonl").exists()

    def _register_session(self, identity: SessionIdentity) -> None:
        """Register a resolved session and do an initial read."""
        with self._lock:
            if identity.session_id in self._readers:
                return
            reader = SessionReader(
                identity.session_dir, identity.session_id, wire_path=identity.wire_path
            )
            self._readers[identity.session_id] = reader

        self.indexer.store.ensure_session(identity.session_id, identity.cwd)
        # Initial ingestion
        self._ingest(reader)
        logger.debug(f"Registered session {identity.session_id} (cwd={identity.cwd})")

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def _ingest(self, reader: SessionReader) -> None:
        """Read new wire events and state for a session."""
        counts: dict[str, int] = {}
        try:
            events = reader.read_new_events()
            if events:
                counts = self.indexer.index_events(events)
                logger.debug(f"Indexed {len(events)} events for {reader.session_id}: {counts}")

            state = reader.read_state()
            if state:
                self.indexer.index_state(state)
        except Exception:
            logger.exception(f"Failed to ingest session {reader.session_id}")
        finally:
            if counts and self.on_ingest:
                try:
                    self.on_ingest(reader.session_id, counts)
                except Exception:
                    logger.exception("Ingest callback failed")

    def _on_wire_changed(self, wire_path: Path) -> None:
        """Callback when a wire.jsonl is modified."""
        identity = resolve_session_identity(wire_path, self._workdir_map)
        with self._lock:
            reader = self._readers.get(identity.session_id)
            if reader is None:
                reader = SessionReader(
                    identity.session_dir, identity.session_id, wire_path=identity.wire_path
                )
                self._readers[identity.session_id] = reader
        self.indexer.store.ensure_session(identity.session_id, identity.cwd)
        self._ingest(reader)

    def _on_state_changed(self, session_dir: Path) -> None:
        """Callback when state.json is modified."""
        import os

        session_id = session_dir.name
        key = os.path.normcase(os.path.normpath(str(session_dir)))
        cwd = self._workdir_map.get(key, "")
        with self._lock:
            reader = self._readers.get(session_id)
            if reader is None:
                reader = SessionReader(session_dir, session_id)
                self._readers[session_id] = reader
        if cwd:
            self.indexer.store.ensure_session(session_id, cwd)
        try:
            state = reader.read_state()
            if state:
                self.indexer.index_state(state)
        except Exception:
            logger.exception(f"Failed to index state for {session_id}")
