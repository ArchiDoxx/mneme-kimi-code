"""Tests for the unified SearchService (mneme.core.query).

These pin the cross-store search/timeline/get behavior that the CLI, Kimi
plugin, and MCP server all delegate to, so the four surfaces can no longer
drift apart. Fake stores are injected so the suite runs without a database.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from mneme.core.query import SearchService


class FakeObservationStore:
    def __init__(
        self,
        search_rows: list[dict[str, Any]] | None = None,
        timeline: dict[str, Any] | None = None,
        observations: list[dict[str, Any]] | None = None,
    ) -> None:
        self._search_rows = search_rows or []
        self._timeline = timeline or {"center": None, "before": [], "after": []}
        self._observations = observations or []
        self.search_calls: list[dict[str, Any]] = []

    def search(
        self,
        query: str,
        limit: int = 10,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        self.search_calls.append(
            {"query": query, "limit": limit, "date_from": date_from, "date_to": date_to}
        )
        return list(self._search_rows)

    def get_timeline(self, observation_id: int, radius: int = 5) -> dict[str, Any]:
        return self._timeline

    def get_observations(self, ids: list[int]) -> list[dict[str, Any]]:
        return list(self._observations)


class FakeStructuredStore:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []

    def search_fts(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return list(self._rows)


class FakeVectorStore:
    def __init__(self, rows: list[dict[str, Any]] | None = None, raises: bool = False) -> None:
        self._rows = rows or []
        self._raises = raises
        self.called = False

    def search_with_content(
        self, query: str, project: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        self.called = True
        if self._raises:
            raise RuntimeError("embedding model unavailable")
        return list(self._rows)


class FakeWireStore:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []

    def search_wire_events(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        return list(self._rows)


def make_service(
    *,
    obs: FakeObservationStore | None = None,
    structured: FakeStructuredStore | None = None,
    vec: FakeVectorStore | None = None,
    wire: FakeWireStore | None = None,
) -> SearchService:
    return SearchService(
        observation_store=obs or FakeObservationStore(),
        structured_store=structured or FakeStructuredStore(),
        vector_store=vec or FakeVectorStore(),
        wire_store=wire or FakeWireStore(),
    )


# Canonical keys every search result must expose, regardless of source.
CANONICAL_KEYS = {
    "id",
    "session_id",
    "timestamp",
    "type",
    "title",
    "snippet",
    "tool_name",
    "file_path",
    "source",
}


def test_search_merges_all_four_sources_with_source_counts() -> None:
    obs = FakeObservationStore(
        search_rows=[
            {
                "id": 1,
                "session_id": "s1",
                "event_type": "ToolUse",
                "tool_name": "Read",
                "file_path": "a.py",
                "created_at": "2026-01-01",
                "snippet": "obs snippet",
            }
        ]
    )
    structured = FakeStructuredStore(
        rows=[
            {
                "id": 7,
                "session_id": "s1",
                "type": "discovery",
                "title": "Found it",
                "narrative": "narr",
                "created_at": "2026-01-02",
            }
        ]
    )
    # search_with_content returns FLAT structured rows (id == structured id),
    # not a nested {"observation": ...} shape.
    vec = FakeVectorStore(
        rows=[
            {
                "id": 9,
                "session_id": "s2",
                "type": "feature",
                "title": "sem title",
                "created_at": "2026-01-03",
                "distance": 0.12,
                "matched_field": "title",
            }
        ]
    )
    wire = FakeWireStore(
        rows=[
            {
                "id": 3,
                "session_id": "s3",
                "event_type": "WireEvent",
                "timestamp": "2026-01-04",
                "session_cwd": "/proj",
                "payload_json": json.dumps({"content": "wire content"}),
            }
        ]
    )
    svc = make_service(obs=obs, structured=structured, vec=vec, wire=wire)

    out = svc.search("query", limit=10)

    assert out["query"] == "query"
    assert out["total"] == 4
    assert out["sources"] == {
        "observations": 1,
        "structured": 1,
        "semantic": 1,
        "wire": 1,
    }
    for result in out["results"]:
        assert CANONICAL_KEYS.issubset(result.keys())


def test_wire_result_uses_canonical_timestamp_and_type_keys() -> None:
    # Regression: the old plugin/cli wire branch emitted created_at/event_type
    # instead of the canonical timestamp/type, breaking the unified shape.
    wire = FakeWireStore(
        rows=[
            {
                "id": 3,
                "session_id": "s3",
                "event_type": "WireEvent",
                "timestamp": "2026-01-04",
                "session_cwd": "/proj",
                "payload_json": json.dumps({"message": "hi"}),
            }
        ]
    )
    svc = make_service(wire=wire)

    result = svc.search("q")["results"][0]

    assert result["source"] == "wire"
    assert result["timestamp"] == "2026-01-04"
    assert result["type"] == "WireEvent"
    assert "created_at" not in result
    assert "event_type" not in result


def test_semantic_disabled_skips_vector_store() -> None:
    vec = FakeVectorStore(rows=[{"id": 9, "session_id": "s2", "title": "x", "distance": 0.1}])
    svc = make_service(vec=vec)

    out = svc.search("q", semantic=False)

    assert vec.called is False
    assert out["sources"]["semantic"] == 0


def test_semantic_results_appear_with_flat_shape() -> None:
    # Regression: the old code read sr["observation"]["id"], a key that
    # search_with_content never returns, so semantic hits were silently dropped.
    vec = FakeVectorStore(
        rows=[
            {
                "id": 42,
                "session_id": "s9",
                "type": "feature",
                "title": "sem hit",
                "created_at": "t",
                "distance": 0.3,
                "matched_field": "title",
            }
        ]
    )
    svc = make_service(vec=vec)

    out = svc.search("q", semantic=True)

    assert vec.called is True
    assert out["sources"]["semantic"] == 1
    sem = next(r for r in out["results"] if r["source"] == "semantic")
    assert sem["id"] == "semantic_42"
    assert sem["distance"] == 0.3
    assert sem["title"] == "sem hit"


def test_semantic_deduped_against_existing_structured_hit() -> None:
    # A semantic hit for structured id 7 must not be re-added when FTS already
    # returned structured id 7 (both come from structured_observations).
    structured = FakeStructuredStore(
        rows=[
            {
                "id": 7,
                "session_id": "s1",
                "type": "feature",
                "title": "dup",
                "narrative": "n",
                "created_at": "t",
            }
        ]
    )
    vec = FakeVectorStore(
        rows=[
            {
                "id": 7,
                "session_id": "s1",
                "type": "feature",
                "title": "dup",
                "created_at": "t",
                "distance": 0.1,
                "matched_field": "title",
            }
        ]
    )
    svc = make_service(structured=structured, vec=vec)

    out = svc.search("q", semantic=True)

    assert out["sources"]["structured"] == 1
    assert out["sources"]["semantic"] == 0


def test_semantic_search_is_best_effort_when_vector_store_raises() -> None:
    obs = FakeObservationStore(
        search_rows=[
            {
                "id": 1,
                "session_id": "s1",
                "event_type": "ToolUse",
                "created_at": "t",
                "snippet": "ok",
            }
        ]
    )
    vec = FakeVectorStore(raises=True)
    svc = make_service(obs=obs, vec=vec)

    out = svc.search("q", semantic=True)

    # Vector failure must not break the whole search.
    assert out["total"] == 1
    assert out["sources"]["observations"] == 1
    assert out["sources"]["semantic"] == 0


def test_dedup_drops_duplicate_snippets() -> None:
    obs = FakeObservationStore(
        search_rows=[
            {"id": 1, "session_id": "s1", "event_type": "E", "created_at": "t", "snippet": "same"},
            {"id": 2, "session_id": "s1", "event_type": "E", "created_at": "t", "snippet": "same"},
            {"id": 3, "session_id": "s1", "event_type": "E", "created_at": "t", "snippet": "other"},
        ]
    )
    svc = make_service(obs=obs)

    out = svc.search("q")

    snippets = [r["snippet"] for r in out["results"]]
    assert snippets == ["same", "other"]


def test_project_filter_handles_none_file_path_without_crashing() -> None:
    # Regression: r.get("file_path", "") returns None when the key exists with a
    # None value, so the old `.lower()` raised AttributeError under a project filter.
    structured = FakeStructuredStore(
        rows=[
            {
                "id": 7,
                "session_id": "unrelated",
                "type": "discovery",
                "title": "t",
                "narrative": "n",
                "created_at": "t",
            }
        ]
    )
    svc = make_service(structured=structured)

    out = svc.search("q", project="myproj")  # must not raise

    assert out["total"] == 0


def test_project_filter_matches_session_id_and_file_path() -> None:
    obs = FakeObservationStore(
        search_rows=[
            {
                "id": 1,
                "session_id": "myproj-x",
                "event_type": "E",
                "created_at": "t",
                "snippet": "a",
            },
            {
                "id": 2,
                "session_id": "other",
                "event_type": "E",
                "created_at": "t",
                "file_path": "/x/myproj/y.py",
                "snippet": "b",
            },
            {
                "id": 3,
                "session_id": "other",
                "event_type": "E",
                "created_at": "t",
                "file_path": "/z/q.py",
                "snippet": "c",
            },
        ]
    )
    svc = make_service(obs=obs)

    out = svc.search("q", project="myproj")

    ids = {r["id"] for r in out["results"]}
    assert ids == {1, 2}


def test_obs_type_filter_restricts_to_matching_type() -> None:
    obs = FakeObservationStore(
        search_rows=[
            {
                "id": 1,
                "session_id": "s",
                "event_type": "ToolUse",
                "created_at": "t",
                "snippet": "a",
            },
            {
                "id": 2,
                "session_id": "s",
                "event_type": "UserPromptSubmit",
                "created_at": "t",
                "snippet": "b",
            },
        ]
    )
    svc = make_service(obs=obs)

    out = svc.search("q", obs_type="UserPromptSubmit")

    assert out["total"] == 1
    assert out["results"][0]["type"] == "UserPromptSubmit"


def test_observation_snippet_falls_back_to_other_fields_when_empty() -> None:
    obs = FakeObservationStore(
        search_rows=[
            {
                "id": 1,
                "session_id": "s1",
                "event_type": "ToolUse",
                "tool_name": "Bash",
                "created_at": "t",
                "snippet": "",
                "tool_output": "the output",
            }
        ]
    )
    svc = make_service(obs=obs)

    result = svc.search("q")["results"][0]

    assert "the output" in result["snippet"]


def test_search_respects_limit() -> None:
    obs = FakeObservationStore(
        search_rows=[
            {
                "id": i,
                "session_id": "s",
                "event_type": "E",
                "created_at": "t",
                "snippet": f"snip{i}",
            }
            for i in range(10)
        ]
    )
    svc = make_service(obs=obs)

    out = svc.search("q", limit=3)

    assert out["total"] == 3
    assert len(out["results"]) == 3
    # the limit must be forwarded to the store call, not only applied to the slice
    assert obs.search_calls[0]["limit"] == 3


def test_timeline_raw_formats_center_before_after() -> None:
    timeline = {
        "center": {
            "id": 5,
            "created_at": "t5",
            "event_type": "ToolUse",
            "tool_name": "Read",
            "file_path": "a.py",
            "tool_output": "center out",
        },
        "before": [
            {"id": 4, "created_at": "t4", "event_type": "E", "error": "boom"},
        ],
        "after": [
            {"id": 6, "created_at": "t6", "event_type": "E", "prompt": "next"},
        ],
    }
    obs = FakeObservationStore(timeline=timeline)
    svc = make_service(obs=obs)

    out = svc.timeline_raw(5, radius=1)

    assert out["center"]["id"] == 5
    assert out["center"]["timestamp"] == "t5"
    assert out["center"]["type"] == "ToolUse"
    assert out["center"]["snippet"] == "center out"
    assert out["before"][0]["snippet"] == "boom"
    assert out["after"][0]["snippet"] == "next"


def test_timeline_raw_handles_missing_center() -> None:
    svc = make_service(
        obs=FakeObservationStore(timeline={"center": None, "before": [], "after": []})
    )

    out = svc.timeline_raw(999)

    assert out["center"] is None
    assert out["before"] == []
    assert out["after"] == []


def test_get_observations_returns_full_raw_fields() -> None:
    obs = FakeObservationStore(
        observations=[
            {
                "id": 1,
                "session_id": "s1",
                "created_at": "t",
                "event_type": "ToolUse",
                "tool_name": "Edit",
                "tool_input": "in",
                "tool_output": "out",
                "error": None,
                "file_path": "a.py",
                "prompt": None,
                "agent_name": "claude",
            }
        ]
    )
    svc = make_service(obs=obs)

    out = svc.get_observations([1])

    assert out["count"] == 1
    record = out["observations"][0]
    assert record["timestamp"] == "t"
    assert record["type"] == "ToolUse"
    assert record["tool_input"] == "in"
    assert record["tool_output"] == "out"
    assert record["agent_name"] == "claude"


def test_get_observations_empty_ids_returns_empty() -> None:
    svc = make_service()

    out = svc.get_observations([])

    assert out == {"observations": [], "count": 0}


# ---------------------------------------------------------------------------
# Integration: real stores against a temp DB. Catches signature/return-shape
# mismatches that the in-memory fakes cannot, proving the service is wired to
# the genuine store APIs.
# ---------------------------------------------------------------------------


@pytest.fixture
def real_stores() -> Iterator[dict[str, Any]]:
    import gc
    import sqlite3
    import tempfile
    import time
    from pathlib import Path

    from mneme.db.schema import init_db
    from mneme.db.store import ObservationStore
    from mneme.db.structured_store import StructuredObservationStore
    from mneme.db.wire_store import WireStore

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    init_db(db_path)

    yield {
        "path": db_path,
        "obs": ObservationStore(db_path=db_path),
        "structured": StructuredObservationStore(db_path=db_path),
        "wire": WireStore(db_path=db_path),
    }

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


def test_search_against_real_stores_finds_raw_and_structured(
    real_stores: dict[str, Any],
) -> None:
    from mneme.core.prompts.json_parser import ParsedObservation
    from mneme.db.store import Observation

    token = "zzytokenqueryxyz"
    obs_store = real_stores["obs"]
    obs_store.add_session("sess_int", "/proj")
    obs_store.add_observation(
        Observation(
            session_id="sess_int",
            event_type="PostToolUse",
            tool_name="WriteFile",
            tool_output=f"wrote {token} to disk",
        )
    )
    real_stores["structured"].add_structured(
        ParsedObservation(
            type="feature",
            title=f"Implemented {token}",
            subtitle="",
            facts=[],
            narrative="did the thing",
            concepts=[],
            files_read=[],
            files_modified=[],
            source="ai",
        ),
        session_id="sess_int",
        project="proj",
        source="ai",
    )

    svc = SearchService(
        observation_store=obs_store,
        structured_store=real_stores["structured"],
        wire_store=real_stores["wire"],
    )

    out = svc.search(token, limit=10, semantic=False)

    assert out["sources"]["observations"] >= 1
    assert out["sources"]["structured"] >= 1
    for result in out["results"]:
        assert CANONICAL_KEYS.issubset(result.keys())


def test_get_and_timeline_against_real_stores(real_stores: dict[str, Any]) -> None:
    from mneme.db.store import Observation

    obs_store = real_stores["obs"]
    obs_store.add_session("sess_int", "/proj")
    obs_id = obs_store.add_observation(
        Observation(
            session_id="sess_int",
            event_type="PostToolUse",
            tool_name="Bash",
            tool_output="ran a command",
        )
    )

    svc = SearchService(observation_store=obs_store)

    got = svc.get_observations([obs_id])
    assert got["count"] == 1
    assert got["observations"][0]["id"] == obs_id
    assert got["observations"][0]["type"] == "PostToolUse"

    tl = svc.timeline_raw(obs_id, radius=2)
    assert tl["center"] is not None
    assert tl["center"]["id"] == obs_id
