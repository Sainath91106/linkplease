"""
Real simple sqlite wrapper. No ORM, didn't feel worth it for this.
Using sqlite because I need this to survive a restart -- an in-memory
dict would lose every pending DM the second the process dies, and the
whole point of this assignment is not losing DMs.
"""
import sqlite3
import os
import threading

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "linkplease.db"))

# sqlite + threads is a bit finicky, one lock for all writes keeps it sane
_lock = threading.Lock()


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")  # so reads don't block on writes
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS rules (
            rule_id TEXT PRIMARY KEY,
            keyword TEXT NOT NULL,
            dm_message TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        -- so a redelivered event_id doesn't get processed twice
        CREATE TABLE IF NOT EXISTS processed_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT,
            received_at TEXT NOT NULL
        );

        -- if a comment.deleted shows up before we've DMed that comment
        CREATE TABLE IF NOT EXISTS deleted_comments (
            comment_id TEXT PRIMARY KEY,
            deleted_at TEXT NOT NULL
        );

        -- one row per (user, rule) match. UNIQUE constraint here is what
        -- actually stops the double-DM, not some in-app check -- if two
        -- events race each other, sqlite itself only lets one INSERT win.
        CREATE TABLE IF NOT EXISTS dm_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            rule_id TEXT NOT NULL,
            comment_id TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            -- pending -> queued_remote -> sent
            --                          -> failed
            -- pending -> cancelled (comment got deleted before we sent)
            dm_id TEXT,
            idempotency_key TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, rule_id)
        );

        CREATE TABLE IF NOT EXISTS stats_counters (
            name TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        );

        INSERT OR IGNORE INTO stats_counters(name, value) VALUES ('duplicates_blocked', 0);
        """
    )
    conn.commit()
    conn.close()


def bump_counter(name: str, by: int = 1):
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE stats_counters SET value = value + ? WHERE name = ?", (by, name)
        )
        conn.commit()
        conn.close()
