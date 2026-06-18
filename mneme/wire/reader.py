"""Read wire.jsonl, state.json and context.jsonl for a single session.

The npm Kimi Code CLI stores sessions as::

    ~/.kimi-code/sessions/<wd_hash>/<session>/state.json
    ~/.kimi-code/sessions/<wd_hash>/<session>/agents/<agent>/wire.jsonl

i.e. ``state.json`` lives in the session root while ``wire.jsonl`` lives one or
two levels deeper under ``agents/<agent>/``. The older Python ``kimi-cli`` layout
kept both files in the same directory. ``SessionReader`` accepts an explicit
``wire_path`` so both layouts work, and the discovery helpers below resolve the
real session id and working directory.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

from mneme.wire.models import SessionState, WireEvent
from mneme.wire.parser import parse_state_json, parse_wire_line


class SessionReader:
    """Tail-follow reader for a single Kimi CLI session directory."""

    def __init__(
        self,
        session_dir: Path | str,
        session_id: str,
        wire_path: Path | str | None = None,
    ) -> None:
        self.session_dir = Path(session_dir)
        self.session_id = session_id
        # wire.jsonl may live under agents/<agent>/; state.json in the root.
        self.wire_path = Path(wire_path) if wire_path else self.session_dir / "wire.jsonl"
        self.state_path = self.session_dir / "state.json"
        self.context_path = self.session_dir / "context.jsonl"
        self._wire_offset: int = 0
        self._state_mtime: float = 0.0

    # ------------------------------------------------------------------
    # Wire events
    # ------------------------------------------------------------------

    def read_new_events(self) -> list[WireEvent]:
        """Return all wire events written since last call."""
        if not self.wire_path.exists():
            return []

        events: list[WireEvent] = []
        with self.wire_path.open("r", encoding="utf-8") as fh:
            fh.seek(self._wire_offset)
            for line in fh:
                evt = parse_wire_line(self.session_id, line)
                if evt is not None:
                    events.append(evt)
            self._wire_offset = fh.tell()
        return events

    def reset(self) -> None:
        """Reset offset to re-read from the beginning."""
        self._wire_offset = 0

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def read_state(self) -> SessionState | None:
        """Read state.json if it has changed since last call."""
        if not self.state_path.exists():
            return None

        mtime = self.state_path.stat().st_mtime
        if mtime == self._state_mtime:
            return None

        try:
            raw: dict[str, Any] = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        self._state_mtime = mtime
        return parse_state_json(self.session_id, raw)

    # ------------------------------------------------------------------
    # Context (optional — heavy, read on demand)
    # ------------------------------------------------------------------

    def read_context_messages(self) -> list[dict[str, Any]]:
        """Read all messages from context.jsonl and context_N.jsonl files."""
        messages: list[dict[str, Any]] = []
        for path in sorted(self.session_dir.glob("context*.jsonl")):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            messages.append(json.loads(line))
            except (json.JSONDecodeError, OSError):
                continue
        return messages


# ----------------------------------------------------------------------
# Session discovery helpers (shared by the watcher and the reindex command)
# ----------------------------------------------------------------------


class SessionIdentity(NamedTuple):
    """Resolved identity for a wire.jsonl file."""

    session_id: str
    session_dir: Path
    wire_path: Path
    cwd: str


def _norm(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def load_workdir_map(kimi_dir: Path | None = None) -> dict[str, str]:
    """Map normalized session directory path -> real working directory.

    Reads ``~/.kimi-code/session_index.jsonl`` which records, per session,
    ``sessionId``, ``sessionDir`` (absolute path) and ``workDir``.
    """
    base = kimi_dir or (Path.home() / ".kimi-code")
    index_file = base / "session_index.jsonl"
    mapping: dict[str, str] = {}
    if not index_file.exists():
        return mapping
    try:
        with index_file.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_dir = row.get("sessionDir")
                work_dir = row.get("workDir")
                if session_dir and work_dir:
                    mapping[_norm(session_dir)] = work_dir
                # Also index by sessionId as a fallback key.
                sid = row.get("sessionId")
                if sid and work_dir:
                    mapping[sid] = work_dir
    except OSError:
        pass
    return mapping


def resolve_session_identity(
    wire_path: Path | str, workdir_map: dict[str, str] | None = None
) -> SessionIdentity:
    """Resolve (session_id, session_dir, wire_path, cwd) for a wire.jsonl path.

    The session root is the nearest ancestor that contains ``state.json``
    (falling back to the grandparent of ``agents/<agent>/``). Sub-agent wire
    files are namespaced as ``<session>:<agent>`` so they don't collide.
    """
    wire_path = Path(wire_path)
    if workdir_map is None:
        workdir_map = load_workdir_map()

    # Find the session root: nearest ancestor holding state.json.
    session_root: Path | None = None
    for parent in wire_path.parents:
        if (parent / "state.json").exists():
            session_root = parent
            break
    if session_root is None:
        # Fallback for agents/<agent>/wire.jsonl with no state.json present.
        parts = wire_path.parts
        if "agents" in parts:
            agents_idx = len(parts) - 1 - parts[::-1].index("agents")
            session_root = Path(*parts[:agents_idx]) if agents_idx > 0 else wire_path.parent
        else:
            session_root = wire_path.parent

    agent_name = wire_path.parent.name
    session_id = session_root.name
    if agent_name not in ("main", session_root.name):
        session_id = f"{session_root.name}:{agent_name}"

    cwd = workdir_map.get(_norm(session_root), "")
    if not cwd:
        # Try to match by sessionId substring (dir name carries the uuid).
        for key, val in workdir_map.items():
            if not key.startswith(("ses_", "session_")):
                continue
            if key in session_root.name or session_root.name.endswith(key):
                cwd = val
                break

    # Normalize to forward slashes so the same project is not split into two
    # by backslash/forward-slash differences in session_index.jsonl.
    if cwd:
        cwd = cwd.replace("\\", "/")

    return SessionIdentity(session_id, session_root, wire_path, cwd)


def iter_session_wires(
    sessions_dir: Path | str, workdir_map: dict[str, str] | None = None
) -> Iterator[SessionIdentity]:
    """Yield a resolved SessionIdentity for every wire.jsonl under sessions_dir.

    Handles both the npm layout (``<wd>/<session>/agents/<agent>/wire.jsonl``)
    and the legacy layout (``<hash>/<session>/wire.jsonl``).
    """
    sessions_dir = Path(sessions_dir)
    if workdir_map is None:
        workdir_map = load_workdir_map()
    if not sessions_dir.exists():
        return
    seen: set[str] = set()
    for wire_file in sessions_dir.rglob("wire.jsonl"):
        key = _norm(wire_file)
        if key in seen:
            continue
        seen.add(key)
        try:
            if wire_file.stat().st_size == 0:
                continue
        except OSError:
            continue
        yield resolve_session_identity(wire_file, workdir_map)
