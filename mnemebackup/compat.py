"""Cross-platform compatibility helpers."""

from __future__ import annotations

import io
import os
import sys


def apply_target_from_argv() -> None:
    """Set ``MNEME_TARGET`` from hook argv before any config/path resolution.

    ``mneme bootstrap`` bakes ``--target <name>`` (or a bare ``kimi`` / ``claude``)
    into each registered hook command so the hook writes to the correct
    per-target memory DB. This must run before the first ``load_config()`` /
    store instantiation; hooks call it as the first statement of ``main()``.
    A pre-existing ``MNEME_TARGET`` in the environment is left untouched.
    """
    if os.environ.get("MNEME_TARGET", "").strip().lower() in ("kimi", "claude"):
        return
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        token = arg.strip().lower()
        if arg == "--target" and i + 1 < len(args):
            value = args[i + 1].strip().lower()
            if value in ("kimi", "claude"):
                os.environ["MNEME_TARGET"] = value
                return
        elif token in ("kimi", "claude"):
            os.environ["MNEME_TARGET"] = token
            return


def fix_windows_encoding() -> None:
    """Force UTF-8 for stdin/stdout/stderr on Windows.

    On Windows the default console encoding (cp1252/cp866) cannot handle
    Unicode characters emitted by our JSON output. This must be called
    before any read from stdin or write to stdout/stderr.
    """
    if sys.platform != "win32":
        return
    if hasattr(sys.stdin, "buffer"):
        sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
