"""SQLite persistence: intake log, FAQ store, flag queue, decisions.

All writes are guarded by a lock so the async handlers, intake tasks, and
the scheduler job never corrupt the database (same pattern as Prism bot).

There is deliberately NO table or column for enforcement outcomes beyond the
*human decision*: a decision records what D'Vaughn approved or dismissed.
Executing an approved action happens outside this bot, by him, manually.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS intake (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source    TEXT NOT NULL,          -- discord | email | mock-discord | mock-email
    author    TEXT,
    channel   TEXT,
    text      TEXT NOT NULL,
    outcome   TEXT,                   -- faq_answered | flagged | ignored | needs_human
    ts        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intake_ts ON intake(ts);

CREATE TABLE IF NOT EXISTS faq (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    question  TEXT NOT NULL,
    answer    TEXT NOT NULL,
    tags      TEXT DEFAULT '',
    hits      INTEGER NOT NULL DEFAULT 0,
    ts        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flags (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    intake_id      INTEGER,
    source         TEXT NOT NULL,
    author         TEXT,
    channel        TEXT,
    text           TEXT NOT NULL,
    category       TEXT NOT NULL,     -- spam | scam | abuse | nsfw | off_topic | needs_human
    confidence     REAL NOT NULL DEFAULT 0.0,
    recommendation TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | dismissed
    notified       INTEGER NOT NULL DEFAULT 0,
    ts             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flags_status ON flags(status);

CREATE TABLE IF NOT EXISTS decisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    flag_id    INTEGER NOT NULL,
    decision   TEXT NOT NULL,         -- approved | dismissed
    decided_by INTEGER NOT NULL,      -- Telegram user id (must be ADMIN_USER_ID)
    note       TEXT,
    ts         TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        log.info("Database ready at %s", path)

    # ------------------------------------------------------------ intake
    def log_intake(self, source: str, author: str, channel: str,
                   text: str, outcome: str = "") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO intake (source, author, channel, text, outcome, ts)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (source, author, channel, text, outcome, _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def set_intake_outcome(self, intake_id: int, outcome: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE intake SET outcome = ? WHERE id = ?", (outcome, intake_id)
            )
            self._conn.commit()

    # ------------------------------------------------------------ faq
    def faq_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM faq").fetchone()
            return int(row["n"])

    def faq_add(self, question: str, answer: str, tags: str = "") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO faq (question, answer, tags, ts) VALUES (?, ?, ?, ?)",
                (question, answer, tags, _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def faq_all(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute("SELECT * FROM faq ORDER BY id"))

    def faq_record_hit(self, faq_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE faq SET hits = hits + 1 WHERE id = ?", (faq_id,)
            )
            self._conn.commit()

    # ------------------------------------------------------------ flags
    def flag_create(self, intake_id: int | None, source: str, author: str,
                    channel: str, text: str, category: str, confidence: float,
                    recommendation: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO flags (intake_id, source, author, channel, text,"
                " category, confidence, recommendation, ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (intake_id, source, author, channel, text, category,
                 confidence, recommendation, _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def flag_get(self, flag_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM flags WHERE id = ?", (flag_id,)
            ).fetchone()

    def flag_pending(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT * FROM flags WHERE status = 'pending' ORDER BY id LIMIT ?",
                (limit,),
            ))

    def flag_mark_notified(self, flag_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE flags SET notified = 1 WHERE id = ?", (flag_id,)
            )
            self._conn.commit()

    def flag_set_status(self, flag_id: int, status: str) -> bool:
        """Set pending -> approved/dismissed. Returns False if not pending."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE flags SET status = ? WHERE id = ? AND status = 'pending'",
                (status, flag_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

    # ------------------------------------------------------------ decisions
    def decision_record(self, flag_id: int, decision: str, decided_by: int,
                        note: str = "") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO decisions (flag_id, decision, decided_by, note, ts)"
                " VALUES (?, ?, ?, ?, ?)",
                (flag_id, decision, decided_by, note, _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def decisions_for_flag(self, flag_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT * FROM decisions WHERE flag_id = ? ORDER BY id", (flag_id,)
            ))

    # ------------------------------------------------------------ stats
    def counts(self) -> dict[str, int]:
        with self._lock:
            def one(sql: str) -> int:
                return int(self._conn.execute(sql).fetchone()["n"])

            return {
                "intake": one("SELECT COUNT(*) AS n FROM intake"),
                "faq": one("SELECT COUNT(*) AS n FROM faq"),
                "faq_answered": one(
                    "SELECT COUNT(*) AS n FROM intake WHERE outcome = 'faq_answered'"
                ),
                "flags_pending": one(
                    "SELECT COUNT(*) AS n FROM flags WHERE status = 'pending'"
                ),
                "flags_approved": one(
                    "SELECT COUNT(*) AS n FROM flags WHERE status = 'approved'"
                ),
                "flags_dismissed": one(
                    "SELECT COUNT(*) AS n FROM flags WHERE status = 'dismissed'"
                ),
            }
