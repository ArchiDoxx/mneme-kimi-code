"""Host-CLI target abstraction (Kimi Code CLI vs Claude Code).

mneme integrates with two host CLIs that emit session traces differently:

- ``kimi``   — Kimi Code CLI. Config in ``~/.kimi-code``; session traces in
  ``~/.kimi-code/sessions/<wd>/<session>/wire.jsonl`` (npm wire format).
- ``claude`` — Claude Code. Config in ``~/.claude`` (or ``$CLAUDE_CONFIG_DIR``);
  session transcripts in ``~/.claude/projects/<encoded-cwd>/<session>.jsonl``.

Memory is stored *separately per target* under ``<config_dir>/mneme/`` so each
CLI keeps its own history. The active target is resolved from, in order:

1. the ``MNEME_TARGET`` environment variable (``kimi`` | ``claude``),
2. the marker file written by ``mneme bootstrap`` (``~/.mneme-target``),
3. auto-detection based on which CLI's trace directory exists.

Hook commands registered by ``mneme bootstrap`` bake ``--target <name>`` into
their invocation, and the MCP server registration injects ``MNEME_TARGET`` as an
env var, so runtime processes always resolve the same target they were set up
for. Only ad-hoc ``mneme`` CLI invocations fall back to the marker / auto-detect.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path

KIMI = "kimi"
CLAUDE = "claude"
_VALID = (KIMI, CLAUDE)

# A small dotfile in the user's home recording the last-bootstrapped target.
_MARKER = Path.home() / ".mneme-target"


@dataclass(frozen=True)
class Target:
    """Resolved paths/behaviour for a host CLI."""

    name: str
    config_dir: Path
    sessions_dir: Path  # where the host CLI writes its session traces

    @property
    def data_dir(self) -> Path:
        """mneme's own data directory for this target (DB, logs, config, hooks)."""
        return self.config_dir / "mneme"


def claude_config_dir() -> Path:
    """Return Claude Code's config dir, honouring ``$CLAUDE_CONFIG_DIR``."""
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".claude"


def kimi_config_dir() -> Path:
    """Return Kimi Code CLI's config dir."""
    return Path.home() / ".kimi-code"


def kimi_target() -> Target:
    base = kimi_config_dir()
    return Target(KIMI, base, base / "sessions")


def claude_target() -> Target:
    base = claude_config_dir()
    return Target(CLAUDE, base, base / "projects")


def get_target(name: str) -> Target:
    """Return the :class:`Target` for an explicit name (defaults to Kimi)."""
    return claude_target() if name == CLAUDE else kimi_target()


def _read_marker() -> str | None:
    try:
        if _MARKER.exists():
            val = _MARKER.read_text(encoding="utf-8").strip().lower()
            if val in _VALID:
                return val
    except OSError:
        pass
    return None


def write_marker(name: str) -> None:
    """Persist the last-bootstrapped target so ad-hoc ``mneme`` commands default to it."""
    if name not in _VALID:
        return
    with contextlib.suppress(OSError):
        _MARKER.write_text(name, encoding="utf-8")


def detect_target_name() -> str:
    """Resolve the active target name: env > marker > auto-detect."""
    env = os.environ.get("MNEME_TARGET", "").strip().lower()
    if env in _VALID:
        return env

    marker = _read_marker()
    if marker:
        return marker

    # Auto-detect: prefer the CLI whose trace directory actually exists.
    claude_has = claude_target().sessions_dir.exists()
    kimi_has = kimi_target().sessions_dir.exists()
    if claude_has and not kimi_has:
        return CLAUDE
    if kimi_has and not claude_has:
        return KIMI

    # Ambiguous (both/neither): stay backward-compatible with the Kimi fork.
    return KIMI


def active_target() -> Target:
    """Return the currently active :class:`Target`."""
    return get_target(detect_target_name())
