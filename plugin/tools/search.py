#!/usr/bin/env python3
"""Plugin tool: mneme_search — search memory index."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from mneme.compat import fix_windows_encoding

fix_windows_encoding()

from mneme.core.query import SearchService


def main() -> None:
    """Handle mneme_search tool call."""
    try:
        params = json.load(sys.stdin)

        query = params.get("query", "")
        limit = min(params.get("limit", 10), 50)
        date_from = params.get("date_from")
        date_to = params.get("date_to")
        project = params.get("project")

        output = SearchService().search(
            query,
            limit=limit,
            date_from=date_from,
            date_to=date_to,
            project=project,
            semantic=True,
        )

        print(json.dumps(output, ensure_ascii=False, indent=2))

    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
