"""Cross-session pattern storage (the ``patterns`` table).

Extracted from ObservationStore so each table-domain owns its own focused,
separately-testable store, following the same single-table-ownership pattern as
WireStore / StructuredObservationStore.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from loguru import logger

from mneme.config import load_config
from mneme.db.schema import get_connection


class PatternStore:
    """Store and query cross-session patterns."""

    def __init__(self, db_path: str | None = None) -> None:
        config = load_config()
        self.db_path = db_path or config["db"]["path"]
        self._local = threading.local()

    def _get_conn(self) -> Any:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = get_connection(self.db_path)
        return self._local.conn

    def add_or_update_pattern(
        self,
        pattern_type: str,
        pattern_hash: str,
        title: str,
        description: str,
        session_id: str | None = None,
        related_files: list[str] | None = None,
        related_observation_ids: list[int] | None = None,
    ) -> int:
        """Add a new pattern or update existing one."""
        with self._get_conn() as conn:
            # Try to update existing
            existing = conn.execute(
                "SELECT id, occurrence_count FROM patterns WHERE pattern_hash = ?",
                (pattern_hash,),
            ).fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE patterns
                    SET occurrence_count = occurrence_count + 1,
                        last_seen_session_id = ?,
                        updated_at = CURRENT_TIMESTAMP,
                        description = CASE WHEN LENGTH(?) > LENGTH(description) THEN ? ELSE description END
                    WHERE id = ?
                    """,
                    (session_id, description, description, existing["id"]),
                )
                logger.debug(f"Pattern updated: {pattern_hash}")
                return int(existing["id"])

            # Insert new
            cursor = conn.execute(
                """
                INSERT INTO patterns
                (pattern_type, pattern_hash, title, description,
                 first_seen_session_id, last_seen_session_id,
                 related_files, related_observation_ids)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pattern_type,
                    pattern_hash,
                    title,
                    description,
                    session_id,
                    session_id,
                    json.dumps(related_files or []),
                    json.dumps(related_observation_ids or []),
                ),
            )
            pattern_id = cursor.lastrowid

        logger.info(f"New pattern added: {title}")
        return pattern_id or 0

    def find_patterns(
        self,
        pattern_type: str | None = None,
        query: str | None = None,
        min_occurrences: int = 1,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Find patterns matching criteria."""
        with self._get_conn() as conn:
            sql = "SELECT * FROM patterns WHERE occurrence_count >= ?"
            params: list[Any] = [min_occurrences]

            if pattern_type:
                sql += " AND pattern_type = ?"
                params.append(pattern_type)

            if query:
                sql += " AND (title LIKE ? OR description LIKE ?)"
                params.extend([f"%{query}%", f"%{query}%"])

            sql += " ORDER BY occurrence_count DESC, updated_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()

        results = []
        for row in rows:
            r = dict(row)
            r["related_files"] = json.loads(r.get("related_files") or "[]")
            r["related_observation_ids"] = json.loads(r.get("related_observation_ids") or "[]")
            results.append(r)
        return results

    def get_patterns_for_project(self, cwd: str, limit: int = 10) -> list[dict[str, Any]]:
        """Get patterns relevant to current project."""
        project_name = os.path.basename(cwd.rstrip("/\\"))

        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM patterns
                WHERE related_files LIKE ? OR title LIKE ? OR description LIKE ?
                ORDER BY occurrence_count DESC, updated_at DESC
                LIMIT ?
                """,
                (f"%{project_name}%", f"%{project_name}%", f"%{project_name}%", limit),
            ).fetchall()

        results = []
        for row in rows:
            r = dict(row)
            r["related_files"] = json.loads(r.get("related_files") or "[]")
            r["related_observation_ids"] = json.loads(r.get("related_observation_ids") or "[]")
            results.append(r)
        return results
