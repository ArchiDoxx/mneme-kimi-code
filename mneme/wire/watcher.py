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
    SessionReader,
    iter_session_wires,
    load_workdir_map,
    resolve_session_identity,
)


def backfill_kimi_sessions(
    sessions_dir: Path | str,
    workdir_map: dict[str, str] | None = None,
    db_path: str | None = None,
) -> dict[str, int]:
    """Index every saved Kimi session not already in the database.

    The live :class:`SessionWatcher` only catches wire files that change while
    the server runs; this is the startup pass that ingests historical sessions.
    It is idempotent: a session that already has indexed wire events is skipped,
    so it is safe to run repeatedly and concurrently with the live watcher (raw
    ``wire_events`` deduplicate, but ``observations`` do not, so re-indexing a
    session would otherwise duplicate rows). Mirrors
    :func:`mneme.wire.claude_watcher.backfill_claude_transcripts`.

    Uses its own :class:`WireIndexer` (with structuring queueing disabled) so the
    background thread neither races on the live indexer's per-session counters
    nor floods the structuring queue with thousands of old observations.
    """
    if workdir_map is None:
        workdir_map = load_workdir_map()
    indexer = WireIndexer(db_path, queue_structuring=False)
    indexed = skipped = events = 0
    for identity in iter_session_wires(sessions_dir, workdir_map):
        sid = identity.session_id
        try:
            if indexer.store.session_has_wire_events(sid):
                skipped += 1
                continue
            reader = SessionReader(identity.session_dir, sid, wire_path=identity.wire_path)
            new_events = reader.read_new_events()
            if not new_events:
                continue
            indexer.store.ensure_session(sid, identity.cwd)
            counts = indexer.index_events(new_events)
            indexer.index_state(reader.read_state())
            events += sum(counts.values())
            indexed += 1
        except Exception:
            logger.exception(f"Backfill failed for Kimi session {sid}")
    if indexed or skipped:
        logger.info(
            f"Kimi backfill: {indexed} sessions indexed, {skipped} already present, {events} events"
        )
    return {"sessions": indexed, "skipped": skipped, "events": events}


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


_global_watcher: object | None = None
_global_lock = threading.Lock()


def get_global_watcher(db_path: str | None = None):
    """Get or create the global singleton watcher for the active target.

    Returns a Kimi :class:`SessionWatcher` or a
    :class:`~mneme.wire.claude_watcher.ClaudeProjectsWatcher` depending on the
    active target. Both expose the same ``start`` / ``stop`` / ``on_ingest``
    interface, so callers (server lifespan, hooks) are target-agnostic.
    """
    global _global_watcher
    with _global_lock:
        if _global_watcher is None:
            from mneme.targets import CLAUDE, detect_target_name

            if detect_target_name() == CLAUDE:
                from mneme.wire.claude_watcher import ClaudeProjectsWatcher

                _global_watcher = ClaudeProjectsWatcher(db_path)
            else:
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
        self._db_path = db_path
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
            # One-time backfill of saved sessions so historical sessions show up
            # immediately. Idempotent (skips already-indexed sessions) and run
            # off-thread so it never blocks server startup.
            threading.Thread(target=self._backfill, daemon=True, name="kimi-backfill").start()

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

    def _backfill(self) -> None:
        """Index saved sessions not yet in the DB (runs once, in a thread)."""
        try:
            # Load the workdir map fresh (not the copy captured at __init__) so
            # cwds written to session_index.jsonl shortly before startup are seen.
            backfill_kimi_sessions(self.sessions_dir, None, self._db_path)
        except Exception:
            logger.exception("Kimi session backfill thread failed")

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
