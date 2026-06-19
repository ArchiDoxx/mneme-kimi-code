"""Parse Claude Code session transcripts into mneme wire events.

Claude Code writes one JSONL transcript per session at::

    ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl

Each line is a record with a top-level ``type``. The records that carry
observation value:

- ``type: "user"`` with ``message.content`` as a **string** → a user prompt.
- ``type: "user"`` with ``message.content`` as a **list** of ``tool_result``
  blocks → tool outputs (paired with the originating ``tool_use`` by id).
- ``type: "assistant"`` with ``message.content`` blocks ``text`` / ``thinking``
  / ``tool_use`` → assistant text, reasoning and tool calls.
- ``assistant.message.usage`` → token accounting.

Unlike the Kimi wire format (one line → one event), a single Claude assistant
record fans out into several events, so :func:`parse_transcript_record` returns
a **list**. The emitted objects are the same :mod:`mneme.wire.models`
dataclasses the Kimi pipeline produces, so the downstream
:class:`~mneme.wire.indexer.WireIndexer` is reused unchanged.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from mneme.wire.models import (
    ContentPartEvent,
    StatusUpdateEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnBeginEvent,
    WireEvent,
)

# Record types that carry no observation value (metadata, attachments, state).
_IGNORED_TYPES = frozenset(
    {
        "attachment",
        "last-prompt",
        "mode",
        "permission-mode",
        "ai-title",
        "queue-operation",
        "system",
        "file-history-snapshot",
        "summary",
    }
)


def _iso_to_epoch(value: Any) -> float:
    """Convert an ISO-8601 timestamp (``2026-06-18T22:41:33.765Z``) to epoch seconds."""
    if not value or not isinstance(value, str):
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _content_to_text(content: Any) -> str:
    """Flatten a ``tool_result`` content value (str | list[block]) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def parse_transcript_record(session_id: str, rec: dict[str, Any]) -> list[WireEvent]:
    """Parse a decoded Claude transcript record into zero or more wire events."""
    rec_type = rec.get("type", "")
    if rec_type in _IGNORED_TYPES:
        return []

    message = rec.get("message")
    if not isinstance(message, dict):
        return []

    timestamp = _iso_to_epoch(rec.get("timestamp"))

    if rec_type == "user":
        return _parse_user(session_id, timestamp, message, rec)
    if rec_type == "assistant":
        return _parse_assistant(session_id, timestamp, message, rec)
    return []


def parse_transcript_line(session_id: str, line: str) -> list[WireEvent]:
    """Parse a single raw Claude transcript JSONL line into wire events."""
    line = line.strip()
    if not line:
        return []
    try:
        rec: dict[str, Any] = json.loads(line)
    except json.JSONDecodeError:
        return []
    return parse_transcript_record(session_id, rec)


def _parse_user(
    session_id: str, timestamp: float, message: dict[str, Any], rec: dict[str, Any]
) -> list[WireEvent]:
    content = message.get("content")
    events: list[WireEvent] = []

    # A real user prompt: content is a plain string.
    if isinstance(content, str):
        text = content.strip()
        if text:
            events.append(
                TurnBeginEvent(
                    session_id=session_id,
                    timestamp=timestamp,
                    event_type="turn.prompt",
                    payload={"text": text},
                    raw=rec,
                    user_input=[{"type": "text", "text": text}],
                )
            )
        return events

    # Tool outputs: content is a list of tool_result blocks.
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            events.append(
                ToolResultEvent(
                    session_id=session_id,
                    timestamp=timestamp,
                    event_type="tool.result",
                    payload=block,
                    raw=rec,
                    tool_call_id=block.get("tool_use_id", ""),
                    is_error=bool(block.get("is_error")),
                    output=_content_to_text(block.get("content")),
                )
            )
    return events


def _parse_assistant(
    session_id: str, timestamp: float, message: dict[str, Any], rec: dict[str, Any]
) -> list[WireEvent]:
    events: list[WireEvent] = []
    content = message.get("content")

    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")

            if block_type == "text":
                text = block.get("text", "")
                if text:
                    events.append(
                        ContentPartEvent(
                            session_id=session_id,
                            timestamp=timestamp,
                            event_type="content.part",
                            payload=block,
                            raw=rec,
                            content_type="text",
                            text=text,
                        )
                    )

            elif block_type == "thinking":
                think = block.get("thinking", "")
                if think:
                    events.append(
                        ContentPartEvent(
                            session_id=session_id,
                            timestamp=timestamp,
                            event_type="content.part",
                            payload=block,
                            raw=rec,
                            content_type="think",
                            think=think,
                        )
                    )

            elif block_type == "tool_use":
                raw_input = block.get("input", {})
                arguments = (
                    raw_input
                    if isinstance(raw_input, str)
                    else json.dumps(raw_input, ensure_ascii=False)
                )
                events.append(
                    ToolCallEvent(
                        session_id=session_id,
                        timestamp=timestamp,
                        event_type="tool.call",
                        payload=block,
                        raw=rec,
                        tool_call_id=block.get("id", ""),
                        tool_name=block.get("name", ""),
                        arguments=arguments,
                    )
                )

    usage = message.get("usage")
    if isinstance(usage, dict):
        events.append(
            StatusUpdateEvent(
                session_id=session_id,
                timestamp=timestamp,
                event_type="usage.record",
                payload={"usage": usage},
                raw=rec,
                input_cache_read=usage.get("cache_read_input_tokens", 0) or 0,
                input_cache_creation=usage.get("cache_creation_input_tokens", 0) or 0,
                input_other=usage.get("input_tokens", 0) or 0,
                output_tokens=usage.get("output_tokens", 0) or 0,
            )
        )
    return events


class ClaudeTranscriptReader:
    """Tail-follow reader for a single Claude Code transcript file.

    Tracks a byte offset so each call returns only newly-appended events. Only
    complete (newline-terminated) lines are consumed; a partial trailing line
    written mid-append is left buffered for the next read.
    """

    def __init__(self, transcript_path: Path | str, session_id: str) -> None:
        self.transcript_path = Path(transcript_path)
        self.session_id = session_id
        self._offset = 0
        self.cwd = ""

    def read_new_events(self) -> list[WireEvent]:
        """Return all wire events parsed from transcript lines written since last call."""
        if not self.transcript_path.exists():
            return []

        with self.transcript_path.open("rb") as fh:
            fh.seek(self._offset)
            data = fh.read()

        if not data:
            return []

        # Only process up to the last complete line; keep any partial tail.
        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            return []
        chunk = data[: last_nl + 1]
        self._offset += len(chunk)

        events: list[WireEvent] = []
        for raw_line in chunk.decode("utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not self.cwd:
                cwd = rec.get("cwd")
                if isinstance(cwd, str) and cwd:
                    self.cwd = cwd
            events.extend(parse_transcript_record(self.session_id, rec))
        return events

    def reset(self) -> None:
        """Reset the offset to re-read the transcript from the beginning."""
        self._offset = 0


def iter_claude_transcripts(projects_dir: Path | str):
    """Yield every ``*.jsonl`` transcript path under a Claude ``projects`` directory."""
    projects_dir = Path(projects_dir)
    if not projects_dir.exists():
        return
    for jsonl in projects_dir.rglob("*.jsonl"):
        if jsonl.is_file():
            yield jsonl
