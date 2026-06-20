"""Tests for the one-time Kimi session backfill.

Mirrors ``tests/test_claude_backfill.py`` for the Kimi wire format. The live
:class:`SessionWatcher` only indexes wire files that change while the server
runs; ``backfill_kimi_sessions`` is the startup pass that ingests already-saved
sessions, idempotently (a session that already has indexed wire events is
skipped, so re-runs do not duplicate observations).
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
from mneme.wire.watcher import backfill_kimi_sessions


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


def _prompt_line(text: str) -> dict:
    return {
        "type": "turn.prompt",
        "input": [{"type": "text", "text": text}],
        "origin": {"kind": "user"},
        "time": 1781270477101,
    }


def _write_kimi_session(sessions_dir: Path, session_id: str, lines: list[dict]) -> Path:
    """Write a session at the ``<wd>/<session>/wire.jsonl`` layout (+ state.json)."""
    sdir = sessions_dir / "wd_proj_abc123" / session_id
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "state.json").write_text(json.dumps({"sessionId": session_id}), encoding="utf-8")
    with (sdir / "wire.jsonl").open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    return sdir


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
    sessions = tmp_path / "sessions"
    _write_kimi_session(sessions, "ses-hist", [_prompt_line("fix the auth bug")])

    result = backfill_kimi_sessions(sessions, workdir_map={}, db_path=temp_db)

    assert result["sessions"] == 1
    assert result["skipped"] == 0
    assert result["events"] >= 1
    assert _count(temp_db, "wire_events", "ses-hist") >= 1
    assert _count(temp_db, "observations", "ses-hist") >= 1


def test_backfill_indexes_multiple_sessions(temp_db: str, tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    _write_kimi_session(sessions, "ses-a", [_prompt_line("task a")])
    _write_kimi_session(sessions, "ses-b", [_prompt_line("task b")])

    result = backfill_kimi_sessions(sessions, workdir_map={}, db_path=temp_db)

    assert result["sessions"] == 2
    assert _count(temp_db, "observations", "ses-a") >= 1
    assert _count(temp_db, "observations", "ses-b") >= 1


def test_backfill_is_idempotent(temp_db: str, tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    _write_kimi_session(sessions, "ses-hist", [_prompt_line("do the thing")])

    first = backfill_kimi_sessions(sessions, workdir_map={}, db_path=temp_db)
    obs_after_first = _count(temp_db, "observations", "ses-hist")

    second = backfill_kimi_sessions(sessions, workdir_map={}, db_path=temp_db)
    obs_after_second = _count(temp_db, "observations", "ses-hist")

    assert first["sessions"] == 1 and first["skipped"] == 0
    assert second["sessions"] == 0 and second["skipped"] == 1
    assert obs_after_second == obs_after_first


def test_backfill_handles_missing_sessions_dir(temp_db: str, tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    result = backfill_kimi_sessions(missing, workdir_map={}, db_path=temp_db)
    assert result == {"sessions": 0, "skipped": 0, "events": 0}


def test_backfill_uses_workdir_map_for_cwd(temp_db: str, tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sdir = _write_kimi_session(sessions, "ses-cwd", [_prompt_line("hello")])
    import os

    key = os.path.normcase(os.path.normpath(str(sdir)))
    backfill_kimi_sessions(sessions, workdir_map={key: "C:/Users/me/proj"}, db_path=temp_db)

    store = WireStore(temp_db)
    assert store.get_session_cwd("ses-cwd") == "C:/Users/me/proj"
