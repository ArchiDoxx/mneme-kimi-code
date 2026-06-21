"""Truncated-output bookkeeping (the ``truncated_outputs`` table).

Extracted from ObservationStore so each table-domain owns its own focused,
separately-testable store.
"""

from __future__ import annotations

import threading
from typing import Any

from loguru import logger

from mneme.config import load_config
from mneme.db.schema import get_connection


class TruncatedOutputStore:
    """Record and retrieve tool-output truncation metadata."""

    def __init__(self, db_path: str | None = None) -> None:
        config = load_config()
        self.db_path = db_path or config["db"]["path"]
        self._local = threading.local()

    def _get_conn(self) -> Any:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = get_connection(self.db_path)
        return self._local.conn

    def record_truncated_output(
        self,
        observation_id: int,
        original_size: int,
        truncated_size: int,
        summary: str | None = None,
        head_preview: str | None = None,
        tail_preview: str | None = None,
        line_count: int | None = None,
    ) -> int:
        """Record that a tool output was truncated."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                INSERT INTO truncated_outputs
                (observation_id, original_size, truncated_size, summary,
                 head_preview, tail_preview, line_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id,
                    original_size,
                    truncated_size,
                    summary,
                    head_preview,
                    tail_preview,
                    line_count,
                ),
            )
            record_id = cursor.lastrowid

        logger.debug(f"Truncated output recorded for observation {observation_id}")
        return record_id or 0

    def get_truncated_output(self, observation_id: int) -> dict[str, Any] | None:
        """Get truncation record for an observation."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM truncated_outputs WHERE observation_id = ?",
                (observation_id,),
            ).fetchone()

        return dict(row) if row else None
