"""Tests for the focused sub-stores split out of ObservationStore.

Covers PatternStore (cross-session patterns) and TruncatedOutputStore, plus the
composition wiring that exposes them as ObservationStore.patterns / .truncated
on the same database.
"""

from __future__ import annotations

import gc
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from mneme.db.pattern_store import PatternStore
from mneme.db.schema import init_db
from mneme.db.store import Observation, ObservationStore
from mneme.db.truncated_store import TruncatedOutputStore


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


# ---------------------------------------------------------------------------
# PatternStore
# ---------------------------------------------------------------------------


def test_add_pattern_then_find(temp_db: str) -> None:
    store = PatternStore(db_path=temp_db)
    pid = store.add_or_update_pattern(
        pattern_type="error",
        pattern_hash="hash-1",
        title="Recurring error in Bash",
        description="It failed",
        related_files=["a.py"],
    )
    assert pid > 0

    found = store.find_patterns(pattern_type="error")
    assert len(found) == 1
    assert found[0]["title"] == "Recurring error in Bash"
    assert found[0]["related_files"] == ["a.py"]  # JSON round-trips back to a list


def test_add_or_update_increments_occurrence_count(temp_db: str) -> None:
    store = PatternStore(db_path=temp_db)
    store.add_or_update_pattern(
        pattern_type="error", pattern_hash="dup", title="t", description="short"
    )
    store.add_or_update_pattern(
        pattern_type="error", pattern_hash="dup", title="t", description="a much longer description"
    )

    found = store.find_patterns(pattern_type="error")
    assert len(found) == 1  # same hash updates rather than inserts
    assert found[0]["occurrence_count"] == 2
    # description is replaced only when the new one is longer
    assert found[0]["description"] == "a much longer description"


def test_find_patterns_min_occurrences_filter(temp_db: str) -> None:
    store = PatternStore(db_path=temp_db)
    store.add_or_update_pattern(
        pattern_type="error", pattern_hash="once", title="once", description="x"
    )
    assert store.find_patterns(min_occurrences=2) == []


def test_find_patterns_query_matches_title_or_description(temp_db: str) -> None:
    store = PatternStore(db_path=temp_db)
    store.add_or_update_pattern(
        pattern_type="error", pattern_hash="a", title="timeout in fetch", description="d1"
    )
    store.add_or_update_pattern(
        pattern_type="error", pattern_hash="b", title="other", description="retry storm"
    )

    by_title = store.find_patterns(query="timeout")
    assert [p["pattern_hash"] for p in by_title] == ["a"]

    by_description = store.find_patterns(query="retry")
    assert [p["pattern_hash"] for p in by_description] == ["b"]


def test_get_patterns_for_project_matches_project_name(temp_db: str) -> None:
    store = PatternStore(db_path=temp_db)
    store.add_or_update_pattern(
        pattern_type="fix",
        pattern_hash="p1",
        title="Fix in myproject",
        description="touched myproject files",
        related_files=["/home/u/myproject/a.py"],
    )

    matches = store.get_patterns_for_project("/home/u/myproject", limit=5)
    assert len(matches) == 1
    assert matches[0]["title"] == "Fix in myproject"


# ---------------------------------------------------------------------------
# TruncatedOutputStore
# ---------------------------------------------------------------------------


def test_truncated_output_round_trip(temp_db: str) -> None:
    obs_store = ObservationStore(db_path=temp_db)
    obs_store.add_session("s1", "/proj")
    obs_id = obs_store.add_observation(
        Observation(session_id="s1", event_type="PostToolUse", tool_output="x" * 10)
    )

    trunc = TruncatedOutputStore(db_path=temp_db)
    rec_id = trunc.record_truncated_output(
        observation_id=obs_id,
        original_size=10000,
        truncated_size=100,
        summary="big output",
    )
    assert rec_id > 0

    got = trunc.get_truncated_output(obs_id)
    assert got is not None
    assert got["original_size"] == 10000
    assert got["summary"] == "big output"


def test_get_truncated_output_returns_none_when_absent(temp_db: str) -> None:
    trunc = TruncatedOutputStore(db_path=temp_db)
    assert trunc.get_truncated_output(99999) is None


# ---------------------------------------------------------------------------
# Composition on ObservationStore
# ---------------------------------------------------------------------------


def test_observation_store_exposes_substores_on_same_db(temp_db: str) -> None:
    store = ObservationStore(db_path=temp_db)
    assert isinstance(store.patterns, PatternStore)
    assert isinstance(store.truncated, TruncatedOutputStore)

    store.patterns.add_or_update_pattern(
        pattern_type="error", pattern_hash="shared", title="via facade", description="d"
    )

    # A fresh PatternStore on the same DB sees the write -> they share storage.
    other = PatternStore(db_path=temp_db)
    found = other.find_patterns(pattern_type="error")
    assert any(p["title"] == "via facade" for p in found)


def test_observation_store_truncated_substore_works(temp_db: str) -> None:
    store = ObservationStore(db_path=temp_db)
    store.add_session("s1", "/proj")
    obs_id = store.add_observation(
        Observation(session_id="s1", event_type="PostToolUse", tool_output="y")
    )

    store.truncated.record_truncated_output(
        observation_id=obs_id, original_size=5000, truncated_size=50
    )
    got = store.truncated.get_truncated_output(obs_id)
    assert got is not None
    assert got["truncated_size"] == 50
