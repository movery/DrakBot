import sqlite3

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
            PRIMARY KEY (guild_id, user_id)
        )
    """)
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


def add_bullets(guild_id: int, user_id: int, amount: int) -> int:
    conn = get_connection()
    conn.execute("""
        INSERT INTO bullets (guild_id, user_id, amount) VALUES (?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET amount = amount + excluded.amount
    """, (guild_id, user_id, amount))
    conn.commit()
    new_total = conn.execute(
        "SELECT amount FROM bullets WHERE guild_id=? AND user_id=?",
        (guild_id, user_id)
    ).fetchone()["amount"]
    conn.close()
    return new_total


def set_bullets(guild_id: int, user_id: int, amount: int):
    conn = get_connection()
    conn.execute("""
        INSERT INTO bullets (guild_id, user_id, amount) VALUES (?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET amount = excluded.amount
    """, (guild_id, user_id, amount))
    conn.commit()
    conn.close()


def spend_bullet(guild_id: int, user_id: int) -> bool:
    if get_bullets(guild_id, user_id) < 1:
        return False
    conn = get_connection()
    conn.execute(
        "UPDATE bullets SET amount = amount - 1 WHERE guild_id=? AND user_id=?",
        (guild_id, user_id)
    )
    conn.commit()
    conn.close()
    return True
