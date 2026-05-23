import sqlite3
import json
import logging
from datetime import datetime
from config import DB_PATH

logger = logging.getLogger(__name__)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id                 INTEGER PRIMARY KEY,
                timezone                TEXT    NOT NULL DEFAULT 'Asia/Tashkent',
                currency                TEXT    NOT NULL DEFAULT 'USD',
                symbol                  TEXT    NOT NULL DEFAULT '$',
                setup_done              INTEGER NOT NULL DEFAULT 0,
                reminder_hour           INTEGER NOT NULL DEFAULT 9,
                reminder_day_before_hour INTEGER NOT NULL DEFAULT 9
            );

            CREATE TABLE IF NOT EXISTS conversation_state (
                user_id     INTEGER PRIMARY KEY,
                state       TEXT,
                data        TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS sheets_queue (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                sheet_name  TEXT    NOT NULL,
                row_data    TEXT    NOT NULL,
                created_at  TEXT    NOT NULL,
                retries     INTEGER NOT NULL DEFAULT 0
            );
        """)
    # Migrate: add columns for reminder hours if upgrading from older schema
    with get_conn() as conn:
        for col, default in [
            ("reminder_hour", "9"),
            ("reminder_day_before_hour", "9"),
        ]:
            try:
                conn.execute(
                    f"ALTER TABLE user_settings ADD COLUMN {col} INTEGER NOT NULL DEFAULT {default}"
                )
            except Exception:
                pass  # column already exists
    logger.info("Database initialised at %s", DB_PATH)


def get_user_settings(user_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row:
            return dict(row)
        return {"timezone": "UTC", "currency": "USD", "symbol": "$", "setup_done": 0}


def save_user_settings(user_id: int, **kwargs) -> None:
    keys = ", ".join(kwargs.keys())
    placeholders = ", ".join("?" for _ in kwargs)
    updates = ", ".join(f"{k} = excluded.{k}" for k in kwargs)
    values = list(kwargs.values())
    with get_conn() as conn:
        conn.execute(
            f"""INSERT INTO user_settings (user_id, {keys})
                VALUES (?, {placeholders})
                ON CONFLICT(user_id) DO UPDATE SET {updates}""",
            [user_id] + values,
        )


def get_state(user_id: int) -> tuple[str | None, dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT state, data FROM conversation_state WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row:
            return row["state"], json.loads(row["data"] or "{}")
        return None, {}


def set_state(user_id: int, state: str | None, data: dict | None = None) -> None:
    if data is None:
        data = {}
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO conversation_state (user_id, state, data)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET state = excluded.state, data = excluded.data""",
            (user_id, state, json.dumps(data)),
        )


def clear_state(user_id: int) -> None:
    set_state(user_id, None, {})


def enqueue_sheet_write(sheet_name: str, row_data: list) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sheets_queue (sheet_name, row_data, created_at) VALUES (?, ?, ?)",
            (sheet_name, json.dumps(row_data), datetime.utcnow().isoformat()),
        )
    logger.warning("Queued offline write to sheet '%s'", sheet_name)


def pop_queued_writes() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sheets_queue ORDER BY id LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]


def delete_queued_write(row_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sheets_queue WHERE id = ?", (row_id,))


def increment_queue_retries(row_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE sheets_queue SET retries = retries + 1 WHERE id = ?", (row_id,)
        )
