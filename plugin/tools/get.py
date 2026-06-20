#!/usr/bin/env python3
"""Plugin tool: mneme_get — fetch full observation details."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from mneme.compat import fix_windows_encoding

fix_windows_encoding()

from mneme.core.query import SearchService


def main() -> None:
    """Handle mneme_get tool call."""
    try:
        params = json.load(sys.stdin)

        ids = params.get("ids", [])
        output = SearchService().get_observations(ids)

        print(json.dumps(output, ensure_ascii=False, indent=2))

    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
