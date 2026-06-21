"""Unified cross-store query orchestration.

This is the single source of truth for searching and reading memory across
the four stores. The CLI, Kimi plugin, and MCP server all delegate here so
they return identical results instead of each re-implementing source
selection, merge, dedup, and output shaping (which had already drifted: the
plugin/CLI searched four sources while the MCP server searched two and
emitted an incompatible JSON shape).

The HTTP API intentionally keeps its own single-source endpoints because the
web UI composes ``/search`` and ``/structured_search`` separately.

Stores are injected so the service is unit-testable without a database; when
omitted they are constructed lazily on first use.
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

SOURCE_OBSERVATION = "observation"
SOURCE_STRUCTURED = "structured"
SOURCE_SEMANTIC = "semantic"
SOURCE_WIRE = "wire"

SNIPPET_MAX = 200
# Inline cap for a wire event's tool_input before the outer snippet truncation.
WIRE_TOOL_INPUT_MAX = 100


def _truncate(text: str, limit: int = SNIPPET_MAX) -> str:
    return text[:limit]


class SearchService:
    """Cross-store search/timeline/get shared by every surface."""

    def __init__(
        self,
        *,
        observation_store: Any | None = None,
        structured_store: Any | None = None,
        vector_store: Any | None = None,
        wire_store: Any | None = None,
    ) -> None:
        self._obs = observation_store
        self._structured = structured_store
        self._vec = vector_store
        self._wire = wire_store

    # ------------------------------------------------------------------
    # Lazily-constructed real stores (overridable via the constructor)
    # ------------------------------------------------------------------

    @property
    def observation_store(self) -> Any:
        if self._obs is None:
            from mneme.db.store import ObservationStore

            self._obs = ObservationStore()
        return self._obs

    @property
    def structured_store(self) -> Any:
        if self._structured is None:
            from mneme.db.structured_store import StructuredObservationStore

            self._structured = StructuredObservationStore()
        return self._structured

    @property
    def vector_store(self) -> Any:
        if self._vec is None:
            from mneme.db.vector import SQLiteVecStore

            self._vec = SQLiteVecStore()
        return self._vec

    @property
    def wire_store(self) -> Any:
        if self._wire is None:
            from mneme.db.wire_store import WireStore

            self._wire = WireStore()
        return self._wire

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        date_from: str | None = None,
        date_to: str | None = None,
        project: str | None = None,
        obs_type: str | None = None,
        semantic: bool = True,
    ) -> dict[str, Any]:
        """Search across raw, structured, semantic, and wire sources.

        Returns a dict with ``results`` (canonical result dicts), ``total``,
        ``query``, and a per-source ``sources`` breakdown. Every result dict
        carries the same keys regardless of source so consumers never have to
        branch on where a hit came from. ``obs_type`` optionally restricts
        results to a single ``type`` value; note structured and semantic hits
        default to type ``"structured"``/``"semantic"``, so an event-type filter
        like ``"PostToolUse"`` will keep only raw observations.
        """
        results: list[dict[str, Any]] = []
        results.extend(self._search_observations(query, limit, date_from, date_to))
        results.extend(self._search_structured(query, limit))
        if semantic:
            results.extend(self._search_semantic(query, project, limit, results))
        results.extend(self._search_wire(query, limit, results))

        if project:
            results = self._filter_by_project(results, project)
        if obs_type:
            results = [r for r in results if r.get("type") == obs_type]

        deduped = self._dedup_by_snippet(results)[:limit]

        return {
            "results": deduped,
            "total": len(deduped),
            "query": query,
            "sources": {
                "observations": _count(deduped, SOURCE_OBSERVATION),
                "structured": _count(deduped, SOURCE_STRUCTURED),
                "semantic": _count(deduped, SOURCE_SEMANTIC),
                "wire": _count(deduped, SOURCE_WIRE),
            },
        }

    def _search_observations(
        self, query: str, limit: int, date_from: str | None, date_to: str | None
    ) -> list[dict[str, Any]]:
        rows = self.observation_store.search(
            query=query, limit=limit, date_from=date_from, date_to=date_to
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            snippet = r.get("snippet") or _observation_fallback_snippet(r)
            out.append(
                {
                    "id": r["id"],
                    "session_id": r["session_id"],
                    "timestamp": r.get("created_at"),
                    "type": r.get("event_type"),
                    "title": None,
                    "snippet": _truncate(snippet),
                    "tool_name": r.get("tool_name"),
                    "file_path": r.get("file_path"),
                    "source": SOURCE_OBSERVATION,
                }
            )
        return out

    def _search_structured(self, query: str, limit: int) -> list[dict[str, Any]]:
        rows = self.structured_store.search_fts(query, limit=limit)
        out: list[dict[str, Any]] = []
        for r in rows:
            title = r.get("title", "")
            narrative = r.get("narrative", "")
            out.append(
                {
                    "id": f"structured_{r['id']}",
                    "session_id": r.get("session_id", ""),
                    "timestamp": r.get("created_at"),
                    "type": r.get("type", "structured"),
                    "title": title,
                    "snippet": _truncate(f"{title}: {narrative}"),
                    "tool_name": None,
                    "file_path": None,
                    "source": SOURCE_STRUCTURED,
                }
            )
        return out

    def _search_semantic(
        self, query: str, project: str | None, limit: int, existing: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        try:
            rows = self.vector_store.search_with_content(query=query, project=project, limit=limit)
        except Exception as e:  # semantic search is best-effort
            logger.debug("Semantic search skipped: {}", e)
            return []

        # search_with_content returns flat structured-observation rows whose `id`
        # is the structured_observations id, so a semantic hit for structured id N
        # duplicates the FTS result already keyed "structured_N". Dedup against
        # that key (not a "semantic_" key, which could never collide).
        seen_structured = {r["id"] for r in existing}
        out: list[dict[str, Any]] = []
        for sr in rows:
            obs_id = sr.get("id")
            if not obs_id:
                continue
            structured_key = f"structured_{obs_id}"
            if structured_key in seen_structured:
                continue
            seen_structured.add(structured_key)
            out.append(
                {
                    "id": f"semantic_{obs_id}",
                    "session_id": sr.get("session_id", ""),
                    "timestamp": sr.get("created_at"),
                    "type": sr.get("type", "semantic"),
                    "title": sr.get("title"),
                    "snippet": _truncate(sr.get("title", "") or ""),
                    "tool_name": sr.get("matched_field", ""),
                    "file_path": None,
                    "source": SOURCE_SEMANTIC,
                    "distance": sr.get("distance"),
                }
            )
        return out

    def _search_wire(
        self, query: str, limit: int, existing: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        rows = self.wire_store.search_wire_events(query=query, limit=limit)
        # Wire events are supplementary context: skip a session entirely if it is
        # already represented by a richer observation/structured/semantic hit
        # (session-level dedup, coarser than the snippet dedup applied later).
        existing_sessions = {r.get("session_id") for r in existing}
        out: list[dict[str, Any]] = []
        for wr in rows:
            if wr.get("session_id") in existing_sessions:
                continue
            out.append(
                {
                    "id": f"wire_{wr['id']}",
                    "session_id": wr.get("session_id", ""),
                    "timestamp": wr.get("timestamp"),
                    "type": wr.get("event_type", "WireEvent"),
                    "title": None,
                    "snippet": _truncate(_wire_payload_text(wr)),
                    "tool_name": None,
                    "file_path": wr.get("session_cwd"),
                    "source": SOURCE_WIRE,
                }
            )
        return out

    @staticmethod
    def _filter_by_project(results: list[dict[str, Any]], project: str) -> list[dict[str, Any]]:
        needle = project.lower()
        return [
            r
            for r in results
            if needle in (r.get("session_id") or "").lower()
            or needle in (r.get("file_path") or "").lower()
        ]

    @staticmethod
    def _dedup_by_snippet(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for r in results:
            snippet = r.get("snippet", "")
            if not snippet:
                deduped.append(r)
            elif snippet not in seen:
                seen.add(snippet)
                deduped.append(r)
        return deduped

    # ------------------------------------------------------------------
    # Timeline (raw observations around a point in time)
    # ------------------------------------------------------------------

    def timeline_raw(self, observation_id: int, radius: int = 5) -> dict[str, Any]:
        """Chronological context around a raw observation."""
        timeline = self.observation_store.get_timeline(observation_id, radius)
        center = timeline.get("center")
        return {
            "center": _format_timeline_obs(center) if center else None,
            "before": [_format_timeline_obs(o) for o in timeline.get("before", [])],
            "after": [_format_timeline_obs(o) for o in timeline.get("after", [])],
        }

    # ------------------------------------------------------------------
    # Get (full raw observations by id)
    # ------------------------------------------------------------------

    def get_observations(self, ids: list[int]) -> dict[str, Any]:
        """Fetch full raw observation records by id."""
        if not ids:
            return {"observations": [], "count": 0}
        rows = self.observation_store.get_observations(ids)
        observations = [_format_full_obs(obs) for obs in rows]
        return {"observations": observations, "count": len(observations)}


# ----------------------------------------------------------------------
# Module-level formatting helpers (shared, pure)
# ----------------------------------------------------------------------


def _count(results: list[dict[str, Any]], source: str) -> int:
    return sum(1 for r in results if r.get("source") == source)


def _observation_fallback_snippet(r: dict[str, Any]) -> str:
    parts = [
        r.get("prompt"),
        r.get("tool_output"),
        r.get("error"),
        r.get("tool_input"),
        r.get("tool_name"),
        r.get("file_path"),
    ]
    joined = " | ".join(str(p) for p in parts if p)
    return joined or "(no preview)"


def _wire_payload_text(wr: dict[str, Any]) -> str:
    # `or "{}"` (not a .get default) so a NULL payload_json column becomes "{}"
    # rather than None, which json.loads would choke on.
    raw = wr.get("payload_json") or "{}"
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return str(raw)
    if not isinstance(payload, dict):
        return str(payload)
    if "content" in payload:
        return str(payload["content"])
    if "message" in payload:
        return str(payload["message"])
    if "tool_name" in payload:
        tool_input = str(payload.get("tool_input", ""))[:WIRE_TOOL_INPUT_MAX]
        return f"{payload['tool_name']}: {tool_input}"
    return str(payload)


def _format_timeline_obs(obs: dict[str, Any]) -> dict[str, Any]:
    snippet = obs.get("tool_output") or obs.get("error") or obs.get("prompt") or ""
    return {
        "id": obs["id"],
        "timestamp": obs.get("created_at"),
        "type": obs.get("event_type"),
        "tool_name": obs.get("tool_name"),
        "file_path": obs.get("file_path"),
        "snippet": _truncate(snippet),
    }


def _format_full_obs(obs: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": obs["id"],
        "session_id": obs["session_id"],
        "timestamp": obs.get("created_at"),
        "type": obs.get("event_type"),
        "tool_name": obs.get("tool_name"),
        "tool_input": obs.get("tool_input"),
        "tool_output": obs.get("tool_output"),
        "error": obs.get("error"),
        "file_path": obs.get("file_path"),
        "prompt": obs.get("prompt"),
        "agent_name": obs.get("agent_name"),
    }
