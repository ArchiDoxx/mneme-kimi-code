"""Parse wire.jsonl lines into typed event objects.

The npm-based **Kimi Code CLI** (``~/.kimi-code``) writes a flat wire format:
each line has a top-level ``type`` in dot-notation (``turn.prompt``,
``context.append_loop_event``, ``usage.record`` …) and an epoch-**millisecond**
``time`` field. Granular step/tool/content events are nested inside
``context.append_loop_event`` under ``event.type`` (``step.begin``, ``tool.call``,
``tool.result``, ``content.part``, ``step.end``).

This differs from the Python ``kimi-cli`` (``~/.kimi``) wire format, which wrapped
events in ``message.type`` / ``message.payload``. This module targets the npm CLI.
"""

from __future__ import annotations

import json
from typing import Any

from mneme.wire.models import (
    CompactionBeginEvent,
    CompactionEndEvent,
    ContentPartEvent,
    SessionState,
    StatusUpdateEvent,
    StepBeginEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnBeginEvent,
    WireEvent,
)

# Top-level event types that carry no observation value (headers, config,
# tool registry, permissions, plan-mode toggles). ``context.append_message``
# duplicates the text already captured by ``turn.prompt`` / ``content.part``,
# so it is skipped to avoid double-storing prompts and replies.
_IGNORED_TOP_TYPES = frozenset(
    {
        "metadata",
        "config.update",
        "tools.set_active_tools",
        "tools.update_store",
        "permission.record_approval_result",
        "plan_mode.enter",
        "plan_mode.cancel",
        "context.append_message",
    }
)


def parse_wire_line(session_id: str, line: str) -> WireEvent | None:
    """Parse a single wire.jsonl line (Kimi Code CLI npm format)."""
    line = line.strip()
    if not line:
        return None

    try:
        raw: dict[str, Any] = json.loads(line)
    except json.JSONDecodeError:
        return None

    top_type = raw.get("type", "")

    # Epoch milliseconds -> seconds. Downstream code calls
    # datetime.fromtimestamp(timestamp); passing raw milliseconds would push
    # every created_at thousands of years into the future.
    time_ms = raw.get("time", 0) or 0
    timestamp = time_ms / 1000.0 if time_ms else 0.0

    if top_type in _IGNORED_TOP_TYPES:
        return None

    # User prompt at the start of a turn.
    if top_type == "turn.prompt":
        return TurnBeginEvent(
            session_id=session_id,
            timestamp=timestamp,
            event_type="turn.prompt",
            payload=raw,
            raw=raw,
            user_input=raw.get("input", []),
        )

    # Context compaction lifecycle.
    if top_type == "full_compaction.begin":
        return CompactionBeginEvent(
            session_id=session_id, timestamp=timestamp, event_type=top_type, payload=raw, raw=raw
        )
    if top_type in ("full_compaction.complete", "context.apply_compaction"):
        return CompactionEndEvent(
            session_id=session_id, timestamp=timestamp, event_type=top_type, payload=raw, raw=raw
        )

    # Turn-level token accounting.
    if top_type == "usage.record":
        usage = raw.get("usage", {}) or {}
        return StatusUpdateEvent(
            session_id=session_id,
            timestamp=timestamp,
            event_type="usage.record",
            payload=raw,
            raw=raw,
            input_cache_read=usage.get("inputCacheRead", 0),
            input_cache_creation=usage.get("inputCacheCreation", 0),
            input_other=usage.get("inputOther", 0),
            output_tokens=usage.get("output", 0),
        )

    # Granular loop events (steps, tool calls/results, content parts).
    if top_type == "context.append_loop_event":
        ev = raw.get("event", {}) or {}
        et = ev.get("type", "")
        base = {
            "session_id": session_id,
            "timestamp": timestamp,
            "event_type": et or "loop_event",
            "payload": ev,
            "raw": raw,
        }

        if et == "step.begin":
            return StepBeginEvent(**base, step_number=ev.get("step", 0))

        if et == "tool.call":
            args = ev.get("args", {})
            arguments = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            return ToolCallEvent(
                **base,
                tool_call_id=ev.get("toolCallId", ""),
                tool_name=ev.get("name", ""),
                arguments=arguments,
            )

        if et == "tool.result":
            result = ev.get("result", {}) or {}
            is_error = bool(
                result.get("isError") or result.get("is_error") or result.get("error")
            )
            return ToolResultEvent(
                **base,
                tool_call_id=ev.get("toolCallId", "") or ev.get("parentUuid", ""),
                is_error=is_error,
                output=result.get("output", ""),
            )

        if et == "content.part":
            part = ev.get("part", {}) or {}
            return ContentPartEvent(
                **base,
                content_type=part.get("type", ""),
                text=part.get("text", ""),
                think=part.get("think", ""),
            )

        # step.end and any other loop event: keep generic. (usage.record is the
        # canonical token-accounting source, so we don't double-count here.)
        return WireEvent(**base)

    # Unknown / future top-level event — keep generic for forward-compat.
    return WireEvent(
        session_id=session_id, timestamp=timestamp, event_type=top_type, payload=raw, raw=raw
    )


def parse_state_json(session_id: str, raw: dict[str, Any]) -> SessionState:
    """Parse state.json dict into SessionState."""
    approval = raw.get("approval", {})
    return SessionState(
        session_id=session_id,
        custom_title=raw.get("custom_title", ""),
        todos=raw.get("todos", []),
        plan_mode=raw.get("plan_mode", False),
        archived=raw.get("archived", False),
        approval_yolo=approval.get("yolo", False),
        approval_afk=approval.get("afk", False),
        auto_approve_actions=approval.get("auto_approve_actions", []),
        raw=raw,
    )
