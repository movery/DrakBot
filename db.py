import sqlite3
import datetime
import logging
import zoneinfo
from contextlib import contextmanager

log = logging.getLogger(__name__)

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

        conn.execute("""
            CREATE TABLE IF NOT EXISTS deathroll_games (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id        INTEGER NOT NULL,
                challenger_id   INTEGER NOT NULL,
                challenger_name TEXT,
                challengee_id   INTEGER NOT NULL,
                challengee_name TEXT,
                stake           INTEGER NOT NULL,
                status          TEXT NOT NULL,
                winner_id       INTEGER,
                outcome         TEXT,
                created_at      TEXT NOT NULL,
                finished_at     TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS blackjack_games (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                user_name   TEXT,
                wager       INTEGER NOT NULL,
                escrow      INTEGER NOT NULL,
                status      TEXT NOT NULL,
                net         INTEGER,
                outcome     TEXT,
                wins        INTEGER NOT NULL DEFAULT 0,
                losses      INTEGER NOT NULL DEFAULT 0,
                pushes      INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL,
                finished_at TEXT
            )
        """)
        # A round can produce several hands (splits), so W/L/P are counted per
        # hand rather than inferred from the round's net. Migrate older rows that
        # predate these columns.
        bj_columns = [row[1] for row in conn.execute("PRAGMA table_info(blackjack_games)").fetchall()]
        for col in ("wins", "losses", "pushes"):
            if col not in bj_columns:
                conn.execute(f"ALTER TABLE blackjack_games ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
        if "wins" not in bj_columns:
            # Best-effort backfill for single-hand rounds settled before this
            # change (only the net was stored). Split rounds can't be recovered.
            conn.execute("UPDATE blackjack_games SET wins=1 WHERE status='finished' AND net>0")
            conn.execute("UPDATE blackjack_games SET losses=1 WHERE status='finished' AND net<0")
            conn.execute("UPDATE blackjack_games SET pushes=1 WHERE status='finished' AND net=0")
    log.debug("database initialized at %s", DB_PATH)


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


def create_deathroll_game(
    guild_id: int,
    challenger_id: int,
    challengee_id: int,
    stake: int,
    challenger_name: str | None = None,
    challengee_name: str | None = None,
) -> int:
    """Record a newly accepted game (status 'active') and return its row id.

    Both players' stakes are deducted by the caller; this row is the persistent
    record of that escrow so it can be refunded if the bot stops mid-game.
    """
    now = datetime.datetime.now(_EST).isoformat()
    with _connect() as conn:
        cursor = conn.execute("""
            INSERT INTO deathroll_games
                (guild_id, challenger_id, challenger_name, challengee_id,
                 challengee_name, stake, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
        """, (guild_id, challenger_id, challenger_name, challengee_id,
              challengee_name, stake, now))
        return cursor.lastrowid


def finish_deathroll_game(game_id: int, winner_id: int, outcome: str):
    """Settle a game: mark it finished and record the winner and outcome."""
    now = datetime.datetime.now(_EST).isoformat()
    with _connect() as conn:
        conn.execute("""
            UPDATE deathroll_games
            SET status='finished', winner_id=?, outcome=?, finished_at=?
            WHERE id=? AND status='active'
        """, (winner_id, outcome, now, game_id))


def recover_deathroll_games() -> list:
    """Refund both stakes for any game left 'active' by a crash/restart.

    The in-memory game and its UI are gone after a restart, so interrupted
    games cannot resume — instead each player gets their stake back and the
    row is marked 'refunded'. Returns the refunded rows (for logging).
    """
    now = datetime.datetime.now(_EST).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM deathroll_games WHERE status='active'"
        ).fetchall()
        for row in rows:
            for uid, name in (
                (row["challenger_id"], row["challenger_name"]),
                (row["challengee_id"], row["challengee_name"]),
            ):
                conn.execute("""
                    INSERT INTO bullets (guild_id, user_id, amount, nickname)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET
                        amount = amount + excluded.amount,
                        nickname = COALESCE(excluded.nickname, nickname)
                """, (row["guild_id"], uid, row["stake"], name))
            conn.execute("""
                UPDATE deathroll_games
                SET status='refunded', outcome='refunded', finished_at=?
                WHERE id=?
            """, (now, row["id"]))
            log.info(
                "refunded deathroll game %d: %d bullets each to players %d and %d",
                row["id"], row["stake"], row["challenger_id"], row["challengee_id"]
            )
        return rows


def deathroll_leaderboard(guild_id: int) -> list[dict]:
    """Net bullets won/lost per player across finished deathroll games.

    Each finished game moves `stake` from the loser to the winner, so the
    winner's net for that game is +stake and the loser's is -stake. Active and
    refunded games net zero and are excluded. Returns one dict per player —
    {user_id, name, net, wins, losses} — sorted by net descending.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT challenger_id, challenger_name, challengee_id, challengee_name, "
            "stake, winner_id FROM deathroll_games "
            "WHERE guild_id=? AND status='finished' ORDER BY finished_at",
            (guild_id,)
        ).fetchall()

    stats: dict[int, dict] = {}
    for row in rows:
        for uid, name in (
            (row["challenger_id"], row["challenger_name"]),
            (row["challengee_id"], row["challengee_name"]),
        ):
            entry = stats.setdefault(
                uid, {"user_id": uid, "name": name, "net": 0, "wins": 0, "losses": 0}
            )
            if name:  # rows are time-ordered, so this keeps the most recent name
                entry["name"] = name
            if row["winner_id"] == uid:
                entry["net"] += row["stake"]
                entry["wins"] += 1
            else:
                entry["net"] -= row["stake"]
                entry["losses"] += 1
    return sorted(stats.values(), key=lambda e: e["net"], reverse=True)


def create_blackjack_game(
    guild_id: int, user_id: int, wager: int, user_name: str | None = None
) -> int:
    """Record a newly started round (status 'active') and return its row id.

    `escrow` starts at `wager` — the bullets already deducted from the player —
    and is bumped as the player commits more (double/split/insurance) so the
    full amount can be refunded if the bot stops mid-round.
    """
    now = datetime.datetime.now(_EST).isoformat()
    with _connect() as conn:
        cursor = conn.execute("""
            INSERT INTO blackjack_games
                (guild_id, user_id, user_name, wager, escrow, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'active', ?)
        """, (guild_id, user_id, user_name, wager, wager, now))
        return cursor.lastrowid


def set_blackjack_escrow(game_id: int, escrow: int):
    """Update the held-bullets total after the player commits more."""
    with _connect() as conn:
        conn.execute(
            "UPDATE blackjack_games SET escrow=? WHERE id=? AND status='active'",
            (escrow, game_id)
        )


def finish_blackjack_game(game_id: int, net: int, outcome: str,
                          wins: int = 0, losses: int = 0, pushes: int = 0):
    """Settle a round: mark it finished and record the net result, outcome, and
    the per-hand win/loss/push tallies (a split round resolves several hands)."""
    now = datetime.datetime.now(_EST).isoformat()
    with _connect() as conn:
        conn.execute("""
            UPDATE blackjack_games
            SET status='finished', net=?, outcome=?, wins=?, losses=?, pushes=?, finished_at=?
            WHERE id=? AND status='active'
        """, (net, outcome, wins, losses, pushes, now, game_id))


def recover_blackjack_games() -> list:
    """Refund the held escrow for any round left 'active' by a crash/restart.

    The in-memory game and its UI are gone after a restart, so an interrupted
    round can't resume — the player gets every committed bullet back and the row
    is marked 'refunded'. Returns the refunded rows (for logging).
    """
    now = datetime.datetime.now(_EST).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM blackjack_games WHERE status='active'"
        ).fetchall()
        for row in rows:
            conn.execute("""
                INSERT INTO bullets (guild_id, user_id, amount, nickname)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    amount = amount + excluded.amount,
                    nickname = COALESCE(excluded.nickname, nickname)
            """, (row["guild_id"], row["user_id"], row["escrow"], row["user_name"]))
            conn.execute("""
                UPDATE blackjack_games
                SET status='refunded', outcome='refunded', finished_at=?
                WHERE id=?
            """, (now, row["id"]))
            log.info(
                "refunded blackjack game %d: %d bullets to player %d",
                row["id"], row["escrow"], row["user_id"]
            )
        return rows


def blackjack_leaderboard(guild_id: int) -> list[dict]:
    """Net bullets won/lost per player across finished blackjack rounds.

    A round's `net` is the player's profit (+) or loss (-) against the house.
    Win/loss/push are counted per hand (so a split round can add to more than
    one tally). Active and refunded rounds are excluded. Returns one dict per
    player — {user_id, name, net, wins, losses, pushes} — sorted by net descending.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT user_id, user_name, net, wins, losses, pushes FROM blackjack_games "
            "WHERE guild_id=? AND status='finished' ORDER BY finished_at",
            (guild_id,)
        ).fetchall()

    stats: dict[int, dict] = {}
    for row in rows:
        entry = stats.setdefault(
            row["user_id"],
            {"user_id": row["user_id"], "name": row["user_name"],
             "net": 0, "wins": 0, "losses": 0, "pushes": 0},
        )
        if row["user_name"]:  # rows are time-ordered, so this keeps the latest name
            entry["name"] = row["user_name"]
        entry["net"] += row["net"] or 0
        entry["wins"] += row["wins"]
        entry["losses"] += row["losses"]
        entry["pushes"] += row["pushes"]
    return sorted(stats.values(), key=lambda e: e["net"], reverse=True)
