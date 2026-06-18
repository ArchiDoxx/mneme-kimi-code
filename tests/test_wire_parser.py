"""Tests for the npm Kimi Code CLI wire format parser and session discovery."""

from __future__ import annotations

import json
import os

from mneme.wire.models import (
    CompactionBeginEvent,
    ContentPartEvent,
    StatusUpdateEvent,
    StepBeginEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnBeginEvent,
)
from mneme.wire.parser import parse_wire_line
from mneme.wire.reader import resolve_session_identity


def _line(obj: dict) -> str:
    return json.dumps(obj)


def test_turn_prompt_becomes_turn_begin():
    evt = parse_wire_line(
        "sess1",
        _line(
            {
                "type": "turn.prompt",
                "input": [{"type": "text", "text": "fix the bug"}],
                "origin": {"kind": "user"},
                "time": 1781270477101,
            }
        ),
    )
    assert isinstance(evt, TurnBeginEvent)
    assert evt.user_input == [{"type": "text", "text": "fix the bug"}]


def test_time_milliseconds_converted_to_seconds():
    evt = parse_wire_line("s", _line({"type": "turn.prompt", "input": [], "time": 1781270477101}))
    # ~2026, not year 58000 — i.e. divided by 1000.
    assert 1_700_000_000 < evt.timestamp < 1_900_000_000


def test_tool_call_and_result_from_loop_event():
    call = parse_wire_line(
        "s",
        _line(
            {
                "type": "context.append_loop_event",
                "time": 1781270477106,
                "event": {
                    "type": "tool.call",
                    "toolCallId": "tool_abc",
                    "name": "Read",
                    "args": {"path": "AGENTS.md"},
                },
            }
        ),
    )
    assert isinstance(call, ToolCallEvent)
    assert call.tool_name == "Read"
    assert call.tool_call_id == "tool_abc"
    assert json.loads(call.arguments) == {"path": "AGENTS.md"}

    result = parse_wire_line(
        "s",
        _line(
            {
                "type": "context.append_loop_event",
                "time": 1781270477200,
                "event": {
                    "type": "tool.result",
                    "toolCallId": "tool_abc",
                    "result": {"output": "file contents"},
                },
            }
        ),
    )
    assert isinstance(result, ToolResultEvent)
    assert result.tool_call_id == "tool_abc"
    assert result.output == "file contents"
    assert result.is_error is False


def test_tool_result_error_flag():
    result = parse_wire_line(
        "s",
        _line(
            {
                "type": "context.append_loop_event",
                "time": 1,
                "event": {
                    "type": "tool.result",
                    "toolCallId": "x",
                    "result": {"output": "boom", "isError": True},
                },
            }
        ),
    )
    assert isinstance(result, ToolResultEvent)
    assert result.is_error is True


def test_content_part_think_and_text():
    think = parse_wire_line(
        "s",
        _line(
            {
                "type": "context.append_loop_event",
                "time": 1,
                "event": {"type": "content.part", "part": {"type": "think", "think": "hmm"}},
            }
        ),
    )
    assert isinstance(think, ContentPartEvent)
    assert think.think == "hmm"
    assert think.text == ""

    text = parse_wire_line(
        "s",
        _line(
            {
                "type": "context.append_loop_event",
                "time": 1,
                "event": {"type": "content.part", "part": {"type": "text", "text": "hello"}},
            }
        ),
    )
    assert isinstance(text, ContentPartEvent)
    assert text.text == "hello"


def test_step_begin_and_usage_and_compaction():
    step = parse_wire_line(
        "s",
        _line(
            {
                "type": "context.append_loop_event",
                "time": 1,
                "event": {"type": "step.begin", "step": 3},
            }
        ),
    )
    assert isinstance(step, StepBeginEvent)
    assert step.step_number == 3

    usage = parse_wire_line(
        "s",
        _line({"type": "usage.record", "time": 1, "usage": {"output": 153, "inputOther": 17250}}),
    )
    assert isinstance(usage, StatusUpdateEvent)
    assert usage.output_tokens == 153

    comp = parse_wire_line("s", _line({"type": "full_compaction.begin", "time": 1}))
    assert isinstance(comp, CompactionBeginEvent)


def test_ignored_and_invalid_lines_return_none():
    assert parse_wire_line("s", _line({"type": "metadata", "app_version": "0.17.1"})) is None
    assert parse_wire_line("s", _line({"type": "config.update"})) is None
    assert parse_wire_line("s", _line({"type": "context.append_message", "message": {}})) is None
    assert parse_wire_line("s", "") is None
    assert parse_wire_line("s", "not json") is None


def test_unknown_loop_event_is_generic_not_dropped():
    evt = parse_wire_line(
        "s",
        _line(
            {
                "type": "context.append_loop_event",
                "time": 1,
                "event": {"type": "goal.update", "id": 1},
            }
        ),
    )
    # Forward-compatible: unknown loop events are kept as generic WireEvents.
    assert evt is not None
    assert evt.event_type == "goal.update"


def test_resolve_session_identity_uses_real_id_not_agent(tmp_path):
    # Layout: <wd>/<session>/agents/main/wire.jsonl  with state.json in <session>.
    session_root = tmp_path / "wd_proj" / "session_abc123"
    wire = session_root / "agents" / "main" / "wire.jsonl"
    wire.parent.mkdir(parents=True)
    wire.write_text("{}", encoding="utf-8")
    (session_root / "state.json").write_text("{}", encoding="utf-8")

    key = os.path.normcase(os.path.normpath(str(session_root)))
    workdir_map = {key: r"C:\Users\dev\Desktop\MyProject"}
    ident = resolve_session_identity(wire, workdir_map)

    # session id must be the session dir, NOT "main".
    assert ident.session_id == "session_abc123"
    # cwd resolved and normalized to forward slashes.
    assert ident.cwd == "C:/Users/dev/Desktop/MyProject"


def test_resolve_session_identity_namespaces_subagents(tmp_path):
    session_root = tmp_path / "wd_proj" / "session_abc123"
    wire = session_root / "agents" / "agent-0" / "wire.jsonl"
    wire.parent.mkdir(parents=True)
    wire.write_text("{}", encoding="utf-8")
    (session_root / "state.json").write_text("{}", encoding="utf-8")

    ident = resolve_session_identity(wire, {})
    assert ident.session_id == "session_abc123:agent-0"
