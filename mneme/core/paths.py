"""Filesystem and interpreter path helpers shared by the CLI and installer.

Kept separate from both so neither has to import the other (the installer needs
these, and the CLI still uses a couple of them in non-install commands).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def get_project_root() -> Path:
    """Directory that contains the bundled ``plugin/``.

    For a source checkout that is the repo root (which holds both ``mneme/`` and
    ``plugin/``); for a pip/uv install the plugin is bundled inside the installed
    ``mneme`` package, so the package directory is returned. Computed relative to
    the ``mneme`` package so it is independent of which module lives here.
    """
    mneme_pkg = Path(__file__).resolve().parents[1]  # .../mneme
    repo_root = mneme_pkg.parent  # checkout root (contains mneme/ and plugin/)
    if (repo_root / "plugin").exists():
        return repo_root
    if (mneme_pkg / "plugin").exists():
        return mneme_pkg
    return repo_root


def get_kimi_dir() -> Path:
    """The Kimi CLI configuration directory (``~/.kimi-code``)."""
    return Path.home() / ".kimi-code"


def get_mneme_dir() -> Path:
    """mneme data directory for the active target.

    Resolves to ``~/.kimi-code/mneme`` or ``~/.claude/mneme`` depending on the
    active target (env ``MNEME_TARGET`` / marker / auto-detect). ``bootstrap``
    sets ``MNEME_TARGET`` up front so every step writes to the right place.
    """
    from mneme.targets import active_target

    return active_target().data_dir


def mneme_python() -> str:
    """Return the most stable Python interpreter that has mneme installed.

    Prefers the persistent uv-tool interpreter (survives uvx cache purges) over
    an ephemeral ``sys.executable``.
    """
    candidates = [
        Path.home()
        / "AppData"
        / "Roaming"
        / "uv"
        / "tools"
        / "mneme-kimi-code"
        / "Scripts"
        / "python.exe",
        Path.home() / ".local" / "share" / "uv" / "tools" / "mneme-kimi-code" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def atomic_write_json(path: Path, data: object) -> None:
    """Write JSON to ``path`` atomically (temp file + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
