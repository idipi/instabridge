from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class State:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS message_map (
                    tg_msg_id INTEGER PRIMARY KEY,
                    ig_msg_id TEXT NOT NULL,
                    ig_thread_id TEXT NOT NULL,
                    ig_client_context TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cursor (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS known_ig_message (
                    ig_msg_id TEXT PRIMARY KEY,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ig_to_tg_message (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ig_msg_id TEXT NOT NULL UNIQUE,
                    tg_msg_id INTEGER NOT NULL,
                    ig_thread_id TEXT NOT NULL,
                    ig_client_context TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._ensure_column(conn, "message_map", "ig_client_context", "TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_message_map_ig_msg_id ON message_map(ig_msg_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ig_to_tg_message_tg_msg_id ON ig_to_tg_message(tg_msg_id)"
            )

    def save_mapping(
        self,
        tg_msg_id: int,
        ig_msg_id: str,
        ig_thread_id: str,
        ig_client_context: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO message_map (
                    tg_msg_id, ig_msg_id, ig_thread_id, ig_client_context
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tg_msg_id) DO UPDATE SET
                    ig_msg_id = excluded.ig_msg_id,
                    ig_thread_id = excluded.ig_thread_id,
                    ig_client_context = excluded.ig_client_context
                """,
                (
                    tg_msg_id,
                    str(ig_msg_id),
                    str(ig_thread_id),
                    ig_client_context or "",
                ),
            )

    def get_mapping(self, tg_msg_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM message_map WHERE tg_msg_id = ?",
                (tg_msg_id,),
            ).fetchone()
            if not row:
                row = conn.execute(
                    """
                    SELECT tg_msg_id, ig_msg_id, ig_thread_id, ig_client_context, created_at
                    FROM ig_to_tg_message
                    WHERE tg_msg_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (tg_msg_id,),
                ).fetchone()
        return dict(row) if row else None

    def get_mapping_by_ig(self, ig_msg_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM message_map WHERE ig_msg_id = ? ORDER BY tg_msg_id ASC LIMIT 1",
                (str(ig_msg_id),),
            ).fetchone()
            if not row:
                row = conn.execute(
                    """
                    SELECT tg_msg_id, ig_msg_id, ig_thread_id, ig_client_context, created_at
                    FROM ig_to_tg_message
                    WHERE ig_msg_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (str(ig_msg_id),),
                ).fetchone()
        return dict(row) if row else None

    def save_ig_to_tg_mapping(
        self,
        ig_msg_id: str,
        tg_msg_id: int,
        ig_thread_id: str,
        ig_client_context: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ig_to_tg_message (
                    ig_msg_id, tg_msg_id, ig_thread_id, ig_client_context
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ig_msg_id) DO UPDATE SET
                    tg_msg_id = excluded.tg_msg_id,
                    ig_thread_id = excluded.ig_thread_id,
                    ig_client_context = excluded.ig_client_context
                """,
                (
                    str(ig_msg_id),
                    int(tg_msg_id),
                    str(ig_thread_id),
                    ig_client_context or "",
                ),
            )

    def save_known_ig_message(self, ig_msg_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO known_ig_message (ig_msg_id) VALUES (?)",
                (str(ig_msg_id),),
            )

    def is_known_ig_message(self, ig_msg_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM known_ig_message WHERE ig_msg_id = ?",
                (str(ig_msg_id),),
            ).fetchone()
        return row is not None

    def get_stats(self) -> dict[str, int]:
        with self._connect() as conn:
            mapped = conn.execute("SELECT COUNT(*) FROM message_map").fetchone()[0]
            sent = conn.execute("SELECT COUNT(*) FROM known_ig_message").fetchone()[0]
        return {"mapped": mapped, "sent_from_tg": sent}

    def get_cursor(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM cursor WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_cursor(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cursor (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, str(value)),
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # sqlite3.Connection used as `with conn:` only commits/rolls back the
        # transaction, it never closes the connection — without this wrapper every
        # call here leaked a file descriptor, and the bot polls every ~45s forever.
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
