import sqlite3
import datetime
import zoneinfo
from contextlib import contextmanager

_EST = zoneinfo.ZoneInfo("America/New_York")

DB_PATH = "bullets.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _connect():
    """Yield a connection, committing on success and always closing."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bullets (
                guild_id   INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                amount     INTEGER NOT NULL DEFAULT 0,
                nickname   TEXT,
                last_daily TEXT,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(bullets)").fetchall()]
        if "last_daily" not in columns:
            conn.execute("ALTER TABLE bullets ADD COLUMN last_daily TEXT")


def get_bullets(guild_id: int, user_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT amount FROM bullets WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        ).fetchone()
    return row["amount"] if row else 0


def add_bullets(guild_id: int, user_id: int, amount: int, nickname: str | None = None) -> int:
    with _connect() as conn:
        conn.execute("""
            INSERT INTO bullets (guild_id, user_id, amount, nickname) VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                amount = amount + excluded.amount,
                nickname = COALESCE(excluded.nickname, nickname)
        """, (guild_id, user_id, amount, nickname))
        return conn.execute(
            "SELECT amount FROM bullets WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        ).fetchone()["amount"]


def set_bullets(guild_id: int, user_id: int, amount: int, nickname: str | None = None):
    with _connect() as conn:
        conn.execute("""
            INSERT INTO bullets (guild_id, user_id, amount, nickname) VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                amount = excluded.amount,
                nickname = COALESCE(excluded.nickname, nickname)
        """, (guild_id, user_id, amount, nickname))


def transfer_bullets(guild_id: int, from_id: int, to_id: int, amount: int, from_name: str | None = None, to_name: str | None = None) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT amount FROM bullets WHERE guild_id=? AND user_id=?",
            (guild_id, from_id)
        ).fetchone()
        if not row or row["amount"] < amount:
            return False
        conn.execute(
            "UPDATE bullets SET amount = amount - ?, nickname = COALESCE(?, nickname) WHERE guild_id=? AND user_id=?",
            (amount, from_name, guild_id, from_id)
        )
        conn.execute("""
            INSERT INTO bullets (guild_id, user_id, amount, nickname) VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                amount = amount + excluded.amount,
                nickname = COALESCE(excluded.nickname, nickname)
        """, (guild_id, to_id, amount, to_name))
    return True


def deduct_bullets(guild_id: int, user_id: int, amount: int, nickname: str | None = None) -> bool:
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE bullets SET amount = amount - ?, nickname = COALESCE(?, nickname) "
            "WHERE guild_id=? AND user_id=? AND amount >= ?",
            (amount, nickname, guild_id, user_id, amount)
        )
        return cursor.rowcount > 0


def spend_bullet(guild_id: int, user_id: int, nickname: str | None = None) -> bool:
    return deduct_bullets(guild_id, user_id, 1, nickname)


def claim_daily(guild_id: int, user_id: int, amount: int, nickname: str | None = None) -> tuple[bool, datetime.timedelta | None, int]:
    """Returns (claimed, time_remaining, new_total). time_remaining is None when claimed successfully."""
    now_est = datetime.datetime.now(_EST)
    today = now_est.date().isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT amount, last_daily FROM bullets WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        ).fetchone()

        if row and row["last_daily"] == today:
            midnight = now_est.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
            return (False, midnight - now_est, row["amount"])

        conn.execute("""
            INSERT INTO bullets (guild_id, user_id, amount, nickname, last_daily) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                amount = amount + excluded.amount,
                nickname = COALESCE(excluded.nickname, nickname),
                last_daily = excluded.last_daily
        """, (guild_id, user_id, amount, nickname, today))
        new_total = conn.execute(
            "SELECT amount FROM bullets WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        ).fetchone()["amount"]
    return (True, None, new_total)
