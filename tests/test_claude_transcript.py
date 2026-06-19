"""Tests for the Claude Code transcript parser/reader.

Sample records mirror the real Claude Code JSONL schema verified on disk:
``user``/``assistant`` records carry a nested Anthropic ``message`` object; tool
calls are ``tool_use`` content blocks on assistant messages; tool outputs are
``tool_result`` blocks on user messages, paired by ``tool_use_id``/``id``.
"""

from __future__ import annotations

import json

from mneme.wire.claude_transcript import (
    ClaudeTranscriptReader,
    parse_transcript_record,
)
from mneme.wire.models import (
    ContentPartEvent,
    StatusUpdateEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnBeginEvent,
)

CWD = "C:\\Users\\luceb\\Desktop\\mneme-kimi-code"


def _user_prompt(text: str) -> dict:
    return {
        "type": "user",
        "timestamp": "2026-06-18T22:41:33.765Z",
        "cwd": CWD,
        "sessionId": "ses-1",
        "message": {"role": "user", "content": text},
    }


def _assistant(blocks: list[dict], usage: dict | None = None) -> dict:
    message: dict = {"role": "assistant", "content": blocks}
    if usage is not None:
        message["usage"] = usage
    return {
        "type": "assistant",
        "timestamp": "2026-06-18T22:42:00.000Z",
        "cwd": CWD,
        "sessionId": "ses-1",
        "message": message,
    }


def _tool_result(tool_use_id: str, content, is_error=None) -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error is not None:
        block["is_error"] = is_error
    return {
        "type": "user",
        "timestamp": "2026-06-18T22:42:05.000Z",
        "cwd": CWD,
        "sessionId": "ses-1",
        "message": {"role": "user", "content": [block]},
    }


def test_user_string_content_becomes_turn_begin():
    events = parse_transcript_record("ses-1", _user_prompt("fix the auth bug"))

    assert len(events) == 1
    evt = events[0]
    assert isinstance(evt, TurnBeginEvent)
    assert evt.event_type == "turn.prompt"
    assert evt.user_input == [{"type": "text", "text": "fix the auth bug"}]
    assert evt.timestamp > 0


def test_empty_user_prompt_is_ignored():
    assert parse_transcript_record("ses-1", _user_prompt("   ")) == []


def test_assistant_text_and_thinking_and_tool_use():
    blocks = [
        {"type": "thinking", "thinking": "Let me read the file first."},
        {"type": "text", "text": "I'll read the README."},
        {
            "type": "tool_use",
            "id": "toolu_abc",
            "name": "Read",
            "input": {"file_path": "README.md"},
        },
    ]
    events = parse_transcript_record("ses-1", _assistant(blocks))

    thinking = [e for e in events if isinstance(e, ContentPartEvent) and e.content_type == "think"]
    text = [e for e in events if isinstance(e, ContentPartEvent) and e.content_type == "text"]
    calls = [e for e in events if isinstance(e, ToolCallEvent)]

    assert len(thinking) == 1 and thinking[0].think == "Let me read the file first."
    assert len(text) == 1 and text[0].text == "I'll read the README."
    assert len(calls) == 1
    assert calls[0].tool_call_id == "toolu_abc"
    assert calls[0].tool_name == "Read"
    assert json.loads(calls[0].arguments) == {"file_path": "README.md"}


def test_parallel_tool_calls_each_emit_an_event():
    blocks = [
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}},
        {"type": "tool_use", "id": "toolu_2", "name": "Read", "input": {"file_path": "a.py"}},
    ]
    calls = [e for e in parse_transcript_record("ses-1", _assistant(blocks)) if isinstance(e, ToolCallEvent)]

    assert {c.tool_call_id for c in calls} == {"toolu_1", "toolu_2"}


def test_assistant_usage_becomes_status_update():
    usage = {
        "input_tokens": 20526,
        "cache_creation_input_tokens": 28611,
        "cache_read_input_tokens": 21109,
        "output_tokens": 483,
    }
    events = parse_transcript_record("ses-1", _assistant([{"type": "text", "text": "hi"}], usage=usage))

    stats = [e for e in events if isinstance(e, StatusUpdateEvent)]
    assert len(stats) == 1
    assert stats[0].input_other == 20526
    assert stats[0].input_cache_creation == 28611
    assert stats[0].input_cache_read == 21109
    assert stats[0].output_tokens == 483


def test_tool_result_string_content():
    rec = _tool_result("toolu_abc", "file contents here", is_error=False)
    events = parse_transcript_record("ses-1", rec)

    assert len(events) == 1
    evt = events[0]
    assert isinstance(evt, ToolResultEvent)
    assert evt.tool_call_id == "toolu_abc"
    assert evt.is_error is False
    assert evt.output == "file contents here"


def test_tool_result_error_flag_truthy():
    rec = _tool_result("toolu_x", "boom", is_error=True)
    evt = parse_transcript_record("ses-1", rec)[0]
    assert evt.is_error is True


def test_tool_result_list_content_is_flattened():
    rec = _tool_result("toolu_y", [{"type": "text", "text": "line1"}, {"type": "text", "text": "line2"}])
    evt = parse_transcript_record("ses-1", rec)[0]
    assert evt.output == "line1\nline2"


def test_ignored_record_types_yield_nothing():
    for rec_type in ("attachment", "ai-title", "summary", "file-history-snapshot"):
        assert parse_transcript_record("ses-1", {"type": rec_type}) == []


def test_reader_tails_incrementally_and_captures_cwd(tmp_path):
    transcript = tmp_path / "ses-1.jsonl"
    transcript.write_text(json.dumps(_user_prompt("first prompt")) + "\n", encoding="utf-8")

    reader = ClaudeTranscriptReader(transcript, "ses-1")
    first = reader.read_new_events()
    assert len(first) == 1
    assert isinstance(first[0], TurnBeginEvent)
    assert reader.cwd == CWD

    # No new data → no events.
    assert reader.read_new_events() == []

    # Append a second record; only the new one is returned.
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_assistant([{"type": "text", "text": "done"}])) + "\n")
    second = reader.read_new_events()
    assert len(second) == 1
    assert isinstance(second[0], ContentPartEvent)


def test_reader_ignores_partial_trailing_line(tmp_path):
    transcript = tmp_path / "ses-2.jsonl"
    # One complete line followed by a partial (no trailing newline) line.
    complete = json.dumps(_user_prompt("complete")) + "\n"
    partial = '{"type": "user", "message": {"role": "user", "content": "partial'
    transcript.write_text(complete + partial, encoding="utf-8")

    reader = ClaudeTranscriptReader(transcript, "ses-2")
    events = reader.read_new_events()
    assert len(events) == 1  # only the complete line

    # Finish the partial line; now it is consumed.
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(' world"}}\n')
    events2 = reader.read_new_events()
    assert len(events2) == 1
    assert isinstance(events2[0], TurnBeginEvent)
