import json
import logging
import aiosqlite
from config import DB_PATH

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tfr_entries (
    notam_id      TEXT PRIMARY KEY,
    facility      TEXT,
    state         TEXT,
    description   TEXT,
    creation_date TEXT,
    data_hash     TEXT NOT NULL,
    full_data     TEXT NOT NULL,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at  TEXT NOT NULL DEFAULT (datetime('now')),
    is_active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=30000")
        await db.executescript(SCHEMA)
        await db.commit()
    logger.info(f"Database initialised at {DB_PATH}")


async def get_state(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM app_state WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_state(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (key, value)
        )
        await db.commit()


async def load_active_tfrs() -> dict[str, dict]:
    """Return {notam_id: {hash, data}} for all currently active TFR entries."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT notam_id, data_hash, full_data FROM tfr_entries WHERE is_active = 1"
        ) as cur:
            rows = await cur.fetchall()
    return {
        row["notam_id"]: {
            "hash": row["data_hash"],
            "data": json.loads(row["full_data"]),
        }
        for row in rows
    }


async def upsert_tfr_entry(tfr: dict, data_hash: str) -> None:
    notam_id = tfr["notam_id"]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO tfr_entries
                (notam_id, facility, state, description, creation_date, data_hash, full_data, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(notam_id) DO UPDATE SET
                facility      = excluded.facility,
                state         = excluded.state,
                description   = excluded.description,
                creation_date = excluded.creation_date,
                data_hash     = excluded.data_hash,
                full_data     = excluded.full_data,
                last_seen_at  = datetime('now'),
                is_active     = 1
            """,
            (
                notam_id,
                tfr.get("facility", ""),
                tfr.get("state", ""),
                tfr.get("description", ""),
                tfr.get("creation_date", ""),
                data_hash,
                json.dumps(tfr),
            ),
        )
        await db.commit()


async def mark_tfr_expired(notam_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tfr_entries SET is_active = 0, last_seen_at = datetime('now') WHERE notam_id = ?",
            (notam_id,),
        )
        await db.commit()
