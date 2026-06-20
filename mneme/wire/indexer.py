"""Index wire events into SQLite."""

from __future__ import annotations

import json
from typing import Any

from mneme.db.wire_store import WireStore
from mneme.wire.models import (
    ContentPartEvent,
    SessionState,
    StatusUpdateEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnBeginEvent,
    WireEvent,
)


class WireIndexer:
    """Consume wire events and persist them to the database."""

    def __init__(self, db_path: str | None = None, *, queue_structuring: bool = True) -> None:
        self.store = WireStore(db_path)
        # When False, observations are not queued for background AI structuring.
        # Used by the bulk historical backfill so it does not flood the live
        # structuring queue (and the worker's competing DB writes) with thousands
        # of old observations.
        self._queue_structuring = queue_structuring
        # Runtime counters per session for turn/step tracking
        self._turns: dict[str, int] = {}
        self._steps: dict[str, int] = {}
        # Tool call cache: session_id -> {tool_call_id: {name, arguments}}
        self._tool_calls: dict[str, dict[str, dict[str, str]]] = {}

    def index_events(self, events: list[WireEvent]) -> dict[str, int]:
        """Index a batch of events. Returns counts by type."""
        counts: dict[str, int] = {}
        for evt in events:
            counts[evt.event_type] = counts.get(evt.event_type, 0) + 1
            self._index_single(evt)
        return counts

    def _index_single(self, evt: WireEvent) -> None:
        sid = evt.session_id

        # Track turn / step numbers. Counters run for EVERY event (including
        # replays) so newly-stored events get correct turn/step numbers.
        match evt:
            case TurnBeginEvent():
                self._turns[sid] = self._turns.get(sid, 0) + 1
                self._steps[sid] = 0  # reset steps on new turn
                self.store.ensure_session(sid)
            case _ if hasattr(evt, "step_number") and evt.step_number:
                self._steps[sid] = max(self._steps.get(sid, 0), evt.step_number)
            case _ if evt.event_type == "StepBegin":
                # StepBegin doesn't have step_number attr in our model,
                # but payload has it.  Handle generically.
                step_n = evt.payload.get("n", 0)
                self._steps[sid] = max(self._steps.get(sid, 0), step_n)

        # Attach runtime counters to event for storage
        turn_n = self._turns.get(sid)
        step_n = self._steps.get(sid)
        if turn_n is not None:
            object.__setattr__(evt, "turn_number", turn_n)
        if step_n is not None:
            object.__setattr__(evt, "step_number", step_n)

        # Persist raw event. wire_events dedupe on (session_id, timestamp,
        # event_type), so is_new is False when this exact event is already
        # stored. Derived rows (observations, thinking, stats) have no such
        # unique key — creating them only for NEW wire events keeps re-indexing
        # idempotent (restart re-reads, or a backfill racing the live watcher on
        # the same session) instead of duplicating observations.
        is_new = self.store.add_wire_event(evt)

        # Typed storage — row creation is gated on is_new; the in-memory
        # tool-call cache must update regardless so ToolResults pair correctly
        # within this run even when the events themselves are replays.
        match evt:
            case TurnBeginEvent():
                # Also store the prompt as an observation for backward compat.
                prompt_text = _extract_prompt_text(evt)
                if is_new and prompt_text:
                    self.store.add_observation_from_wire(
                        session_id=sid,
                        event_type="UserPromptSubmit",
                        prompt=prompt_text,
                        turn_number=turn_n,
                        step_number=0,
                        timestamp=evt.timestamp,
                        cwd=self.store.get_session_cwd(sid),
                        queue_structuring=self._queue_structuring,
                    )
            case StatusUpdateEvent():
                if is_new:
                    self.store.add_session_stat(evt)
            case ContentPartEvent():
                if is_new and evt.think:
                    self.store.add_thinking(evt)
                if is_new and evt.text:
                    self.store.add_assistant_message(evt)
            case ToolCallEvent():
                # Cache tool call info for pairing with ToolResult
                sid_tools = self._tool_calls.setdefault(sid, {})
                sid_tools[evt.tool_call_id] = {
                    "name": evt.tool_name,
                    "arguments": evt.arguments,
                }
            case ToolResultEvent():
                self.store.ensure_session(sid)
                # Pop the paired ToolCall regardless so the cache stays correct.
                tool_info = self._tool_calls.get(sid, {}).pop(evt.tool_call_id, {})
                if is_new:
                    self.store.add_observation_from_wire(
                        session_id=sid,
                        event_type="PostToolUseFailure" if evt.is_error else "PostToolUse",
                        tool_name=tool_info.get("name"),
                        tool_input=tool_info.get("arguments"),
                        tool_output=_normalize_output(evt.output),
                        turn_number=turn_n,
                        step_number=step_n,
                        timestamp=evt.timestamp,
                        cwd=self.store.get_session_cwd(sid),
                        queue_structuring=self._queue_structuring,
                    )

    def index_state(self, state: SessionState | None) -> None:
        """Update session metadata from state.json."""
        if state is None:
            return
        self.store.sync_todos(state)
        # Update session title if available
        if state.custom_title:
            with self.store._get_conn() as conn:
                conn.execute(
                    "UPDATE sessions SET summary = COALESCE(summary, ?) WHERE id = ?",
                    (state.custom_title, state.session_id),
                )


def _extract_prompt_text(evt: TurnBeginEvent) -> str:
    """Extract plain text from TurnBegin user_input."""
    parts: list[str] = []
    for item in evt.user_input:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text", ""))
        elif isinstance(item, str):
            parts.append(item)
    return " ".join(parts).strip()


def _normalize_output(value: Any) -> str:
    """Convert tool output to string (handles text, list, dict)."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        texts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                texts.append(item.get("text", json.dumps(item, ensure_ascii=False)))
            else:
                texts.append(str(item))
        return "\n".join(texts)
    if value is None:
        return ""
    return str(value)
