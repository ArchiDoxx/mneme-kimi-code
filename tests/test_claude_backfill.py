"""Tests for the one-time Claude transcript backfill.

The live :class:`ClaudeProjectsWatcher` only indexes transcripts that change
while the server runs. ``backfill_claude_transcripts`` is the startup pass that
indexes already-saved transcripts so historical sessions show up in the UI.

The key property under test is idempotency: a session that already has indexed
wire events is skipped, so the backfill is safe to run repeatedly and alongside
the live watcher (``observations`` are not content-deduplicated, so re-indexing
the same session would otherwise create duplicate rows).
"""

from __future__ import annotations

import gc
import json
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from mneme.db.schema import init_db
from mneme.db.wire_store import WireStore
from mneme.wire.claude_watcher import backfill_claude_transcripts

CWD = "C:\\Users\\luceb\\Desktop\\mneme-kimi-code"


@pytest.fixture
def temp_db() -> Iterator[str]:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    init_db(db_path)
    yield db_path

    gc.collect()
    for obj in gc.get_objects():
        try:
            if type(obj) is sqlite3.Connection:
                obj.close()
        except Exception:
            pass
    for _ in range(10):
        try:
            Path(db_path).unlink(missing_ok=True)
            break
        except PermissionError:
            time.sleep(0.1)


def _prompt(text: str, sid: str) -> dict:
    return {
        "type": "user",
        "timestamp": "2026-06-18T22:41:33.765Z",
        "cwd": CWD,
        "sessionId": sid,
        "message": {"role": "user", "content": text},
    }


def _write_transcript(projects_dir: Path, session_id: str, records: list[dict]) -> Path:
    """Write a transcript at the realistic ``<encoded-cwd>/<session>.jsonl`` path."""
    encoded = projects_dir / "C--Users-luceb-Desktop-mneme-kimi-code"
    encoded.mkdir(parents=True, exist_ok=True)
    path = encoded / f"{session_id}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return path


def _count(db_path: str, table: str, session_id: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def test_backfill_indexes_saved_session(temp_db: str, tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    _write_transcript(projects, "ses-hist", [_prompt("fix the auth bug", "ses-hist")])

    result = backfill_claude_transcripts(projects, db_path=temp_db)

    assert result["sessions"] == 1
    assert result["skipped"] == 0
    assert result["events"] >= 1
    # The session row, its wire events and at least one observation are present.
    assert _count(temp_db, "wire_events", "ses-hist") >= 1
    assert _count(temp_db, "observations", "ses-hist") >= 1
    store = WireStore(temp_db)
    assert store.get_session_cwd("ses-hist") == CWD


def test_backfill_indexes_multiple_sessions(temp_db: str, tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    _write_transcript(projects, "ses-a", [_prompt("task a", "ses-a")])
    _write_transcript(projects, "ses-b", [_prompt("task b", "ses-b")])

    result = backfill_claude_transcripts(projects, db_path=temp_db)

    assert result["sessions"] == 2
    assert _count(temp_db, "observations", "ses-a") >= 1
    assert _count(temp_db, "observations", "ses-b") >= 1


def test_backfill_is_idempotent(temp_db: str, tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    _write_transcript(projects, "ses-hist", [_prompt("do the thing", "ses-hist")])

    first = backfill_claude_transcripts(projects, db_path=temp_db)
    obs_after_first = _count(temp_db, "observations", "ses-hist")

    second = backfill_claude_transcripts(projects, db_path=temp_db)
    obs_after_second = _count(temp_db, "observations", "ses-hist")

    assert first["sessions"] == 1 and first["skipped"] == 0
    # Second run skips the already-indexed session — no duplicate observations.
    assert second["sessions"] == 0 and second["skipped"] == 1
    assert obs_after_second == obs_after_first


def test_backfill_handles_missing_projects_dir(temp_db: str, tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    result = backfill_claude_transcripts(missing, db_path=temp_db)
    assert result == {"sessions": 0, "skipped": 0, "events": 0}


def test_reindexing_same_events_is_idempotent(temp_db: str) -> None:
    """Indexing the same wire events twice must not duplicate observations.

    Guards the backfill-vs-live-watcher race and restart re-reads: wire_events
    dedupe on (session_id, timestamp, event_type) and derived observations are
    only created for newly-inserted wire events.
    """
    from mneme.wire.claude_transcript import parse_transcript_record
    from mneme.wire.indexer import WireIndexer

    rec = {
        "type": "user",
        "timestamp": "2026-06-18T22:41:33.765Z",
        "cwd": CWD,
        "sessionId": "s1",
        "message": {"role": "user", "content": "do the work"},
    }
    events = parse_transcript_record("s1", rec)
    assert events

    WireIndexer(temp_db).index_events(events)
    obs1 = _count(temp_db, "observations", "s1")
    we1 = _count(temp_db, "wire_events", "s1")
    assert obs1 >= 1 and we1 >= 1

    # Second independent pass over the SAME events (a fresh indexer = restart /
    # concurrent backfill) — counts must be unchanged.
    WireIndexer(temp_db).index_events(events)
    assert _count(temp_db, "observations", "s1") == obs1
    assert _count(temp_db, "wire_events", "s1") == we1


def test_session_has_wire_events_reflects_indexing(temp_db: str, tmp_path: Path) -> None:
    store = WireStore(temp_db)
    assert store.session_has_wire_events("ses-hist") is False

    projects = tmp_path / "projects"
    _write_transcript(projects, "ses-hist", [_prompt("hello", "ses-hist")])
    backfill_claude_transcripts(projects, db_path=temp_db)

    assert store.session_has_wire_events("ses-hist") is True
