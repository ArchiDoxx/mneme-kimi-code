"""Filesystem watcher for Claude Code transcripts (``~/.claude/projects``).

The Claude counterpart of :class:`mneme.wire.watcher.SessionWatcher`. It watches
the ``projects`` directory for changes to ``*.jsonl`` transcript files, tails
each one incrementally via :class:`ClaudeTranscriptReader`, and feeds the parsed
events into the shared :class:`~mneme.wire.indexer.WireIndexer`. Exposes the same
``start`` / ``stop`` / ``on_ingest`` interface as the Kimi watcher so the server
and the global-watcher factory can treat the two interchangeably.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from pathlib import Path

from loguru import logger
from watchdog.events import FileSystemEvent, FileSystemEventHandler

# Cross-platform observer selection (mirrors mneme.wire.watcher).
if sys.platform == "win32":
    from watchdog.observers.read_directory_changes import WindowsApiObserver as PlatformObserver
elif sys.platform == "darwin":
    from watchdog.observers.fsevents import FSEventsObserver as PlatformObserver
elif sys.platform.startswith("linux"):
    from watchdog.observers.inotify import InotifyObserver as PlatformObserver
else:
    from watchdog.observers.polling import PollingObserver as PlatformObserver

from mneme.targets import claude_target
from mneme.wire.claude_transcript import ClaudeTranscriptReader
from mneme.wire.indexer import WireIndexer


class _TranscriptEventHandler(FileSystemEventHandler):
    """Dispatch filesystem events for ``*.jsonl`` transcript files."""

    def __init__(self, watcher: ClaudeProjectsWatcher) -> None:
        self.watcher = watcher

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix == ".jsonl":
            self.watcher._on_transcript_changed(path)

    def on_created(self, event: FileSystemEvent) -> None:
        self.on_modified(event)


class ClaudeProjectsWatcher:
    """Watch ``~/.claude/projects`` and index Claude transcripts in real time."""

    def __init__(self, db_path: str | None = None) -> None:
        self.projects_dir = claude_target().sessions_dir
        self.indexer = WireIndexer(db_path)
        self._readers: dict[str, ClaudeTranscriptReader] = {}
        self._lock = threading.Lock()
        self._observer: PlatformObserver | None = None
        self._running = False
        self.on_ingest: Callable[[str, dict[str, int]], None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        if self.projects_dir.exists():
            handler = _TranscriptEventHandler(self)
            self._observer = PlatformObserver()
            self._observer.schedule(handler, str(self.projects_dir), recursive=True)
            self._observer.start()
            logger.info(
                f"ClaudeProjectsWatcher started on {self.projects_dir} "
                f"(observer: {type(self._observer).__name__})"
            )
        else:
            logger.info(f"Claude projects dir not found: {self.projects_dir}")

    def stop(self) -> None:
        self._running = False
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
            logger.info("ClaudeProjectsWatcher stopped")

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def _on_transcript_changed(self, path: Path) -> None:
        session_id = path.stem
        with self._lock:
            reader = self._readers.get(session_id)
            if reader is None:
                reader = ClaudeTranscriptReader(path, session_id)
                self._readers[session_id] = reader
        self._ingest(reader)

    def _ingest(self, reader: ClaudeTranscriptReader) -> None:
        counts: dict[str, int] = {}
        try:
            events = reader.read_new_events()
            if events:
                # Seed the session row with its cwd before the indexer creates
                # observations (ON CONFLICT preserves any existing cwd).
                self.indexer.store.ensure_session(reader.session_id, reader.cwd)
                counts = self.indexer.index_events(events)
                logger.debug(
                    f"Indexed {len(events)} events for {reader.session_id}: {counts}"
                )
        except Exception:
            logger.exception(f"Failed to ingest transcript {reader.session_id}")
        finally:
            if counts and self.on_ingest:
                try:
                    self.on_ingest(reader.session_id, counts)
                except Exception:
                    logger.exception("Ingest callback failed")
