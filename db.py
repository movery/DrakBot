import sqlite3
import datetime
import zoneinfo

_EST = zoneinfo.ZoneInfo("America/New_York")

DB_PATH = "bullets.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
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
    conn.commit()
    conn.close()


def get_bullets(guild_id: int, user_id: int) -> int:
    conn = get_connection()
    row = conn.execute(
        "SELECT amount FROM bullets WHERE guild_id=? AND user_id=?",
        (guild_id, user_id)
    ).fetchone()
    conn.close()
    return row["amount"] if row else 0


def add_bullets(guild_id: int, user_id: int, amount: int, nickname: str = None) -> int:
    conn = get_connection()
    conn.execute("""
        INSERT INTO bullets (guild_id, user_id, amount, nickname) VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            amount = amount + excluded.amount,
            nickname = COALESCE(excluded.nickname, nickname)
    """, (guild_id, user_id, amount, nickname))
    conn.commit()
    new_total = conn.execute(
        "SELECT amount FROM bullets WHERE guild_id=? AND user_id=?",
        (guild_id, user_id)
    ).fetchone()["amount"]
    conn.close()
    return new_total


def set_bullets(guild_id: int, user_id: int, amount: int, nickname: str = None):
    conn = get_connection()
    conn.execute("""
        INSERT INTO bullets (guild_id, user_id, amount, nickname) VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            amount = excluded.amount,
            nickname = COALESCE(excluded.nickname, nickname)
    """, (guild_id, user_id, amount, nickname))
    conn.commit()
    conn.close()


def transfer_bullets(guild_id: int, from_id: int, to_id: int, amount: int, from_name: str = None, to_name: str = None) -> bool:
    conn = get_connection()
    row = conn.execute(
        "SELECT amount FROM bullets WHERE guild_id=? AND user_id=?",
        (guild_id, from_id)
    ).fetchone()
    if not row or row["amount"] < amount:
        conn.close()
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
    conn.commit()
    conn.close()
    return True


def spend_bullet(guild_id: int, user_id: int, nickname: str = None) -> bool:
    if get_bullets(guild_id, user_id) < 1:
        return False
    conn = get_connection()
    conn.execute(
        "UPDATE bullets SET amount = amount - 1, nickname = COALESCE(?, nickname) WHERE guild_id=? AND user_id=?",
        (nickname, guild_id, user_id)
    )
    conn.commit()
    conn.close()
    return True


def claim_daily(guild_id: int, user_id: int, amount: int, nickname: str = None) -> tuple[bool, datetime.timedelta | None, int]:
    """Returns (claimed, time_remaining, new_total). time_remaining is None when claimed successfully."""
    now_est = datetime.datetime.now(_EST)
    today = now_est.date().isoformat()
    conn = get_connection()
    row = conn.execute(
        "SELECT amount, last_daily FROM bullets WHERE guild_id=? AND user_id=?",
        (guild_id, user_id)
    ).fetchone()

    if row and row["last_daily"] == today:
        midnight = now_est.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        conn.close()
        return (False, midnight - now_est, row["amount"])

    conn.execute("""
        INSERT INTO bullets (guild_id, user_id, amount, nickname, last_daily) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            amount = amount + excluded.amount,
            nickname = COALESCE(excluded.nickname, nickname),
            last_daily = excluded.last_daily
    """, (guild_id, user_id, amount, nickname, today))
    conn.commit()
    new_total = conn.execute(
        "SELECT amount FROM bullets WHERE guild_id=? AND user_id=?",
        (guild_id, user_id)
    ).fetchone()["amount"]
    conn.close()
    return (True, None, new_total)
