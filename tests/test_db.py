"""Characterization tests for db.py — these lock in the current behavior of the
bullet economy so refactors can be verified to preserve it.

Each test runs against a throwaway SQLite file; db.DB_PATH is pointed at it in
setUp and the connection helper reads that global at call time.
"""
import os
import tempfile
import unittest

import db

GUILD = 1
ALICE = 100
BOB = 200


class DbTestCase(unittest.TestCase):
    def setUp(self):
        fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig_path = db.DB_PATH
        db.DB_PATH = self._path
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self._orig_path
        os.remove(self._path)


class InitDbTests(DbTestCase):
    def test_idempotent(self):
        db.init_db()  # second call must not raise
        self.assertEqual(db.get_bullets(GUILD, ALICE), 0)

    def test_migration_adds_last_daily(self):
        # Simulate an old schema without the last_daily column.
        os.remove(self._path)
        conn = db.get_connection()
        conn.execute(
            "CREATE TABLE bullets (guild_id INTEGER, user_id INTEGER, "
            "amount INTEGER NOT NULL DEFAULT 0, nickname TEXT, "
            "PRIMARY KEY (guild_id, user_id))"
        )
        conn.commit()
        conn.close()

        db.init_db()

        conn = db.get_connection()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(bullets)").fetchall()]
        conn.close()
        self.assertIn("last_daily", cols)


class GetBulletsTests(DbTestCase):
    def test_unknown_user_is_zero(self):
        self.assertEqual(db.get_bullets(GUILD, ALICE), 0)


class AddBulletsTests(DbTestCase):
    def test_creates_and_returns_total(self):
        self.assertEqual(db.add_bullets(GUILD, ALICE, 5, "alice"), 5)
        self.assertEqual(db.get_bullets(GUILD, ALICE), 5)

    def test_accumulates(self):
        db.add_bullets(GUILD, ALICE, 5)
        self.assertEqual(db.add_bullets(GUILD, ALICE, 3), 8)

    def test_nickname_preserved_when_none(self):
        db.add_bullets(GUILD, ALICE, 5, "alice")
        db.add_bullets(GUILD, ALICE, 1, None)
        conn = db.get_connection()
        nick = conn.execute(
            "SELECT nickname FROM bullets WHERE guild_id=? AND user_id=?", (GUILD, ALICE)
        ).fetchone()["nickname"]
        conn.close()
        self.assertEqual(nick, "alice")


class SetBulletsTests(DbTestCase):
    def test_absolute_set(self):
        db.add_bullets(GUILD, ALICE, 5)
        db.set_bullets(GUILD, ALICE, 2)
        self.assertEqual(db.get_bullets(GUILD, ALICE), 2)

    def test_set_zero(self):
        db.add_bullets(GUILD, ALICE, 5)
        db.set_bullets(GUILD, ALICE, 0)
        self.assertEqual(db.get_bullets(GUILD, ALICE), 0)


class TransferBulletsTests(DbTestCase):
    def test_success(self):
        db.add_bullets(GUILD, ALICE, 10)
        self.assertTrue(db.transfer_bullets(GUILD, ALICE, BOB, 4, "alice", "bob"))
        self.assertEqual(db.get_bullets(GUILD, ALICE), 6)
        self.assertEqual(db.get_bullets(GUILD, BOB), 4)

    def test_insufficient_funds_unchanged(self):
        db.add_bullets(GUILD, ALICE, 3)
        self.assertFalse(db.transfer_bullets(GUILD, ALICE, BOB, 4))
        self.assertEqual(db.get_bullets(GUILD, ALICE), 3)
        self.assertEqual(db.get_bullets(GUILD, BOB), 0)

    def test_unknown_sender(self):
        self.assertFalse(db.transfer_bullets(GUILD, ALICE, BOB, 1))


class DeductBulletsTests(DbTestCase):
    def test_success(self):
        db.add_bullets(GUILD, ALICE, 10)
        self.assertTrue(db.deduct_bullets(GUILD, ALICE, 4))
        self.assertEqual(db.get_bullets(GUILD, ALICE), 6)

    def test_exact_balance(self):
        db.add_bullets(GUILD, ALICE, 6)
        self.assertTrue(db.deduct_bullets(GUILD, ALICE, 6))
        self.assertEqual(db.get_bullets(GUILD, ALICE), 0)

    def test_insufficient_unchanged(self):
        db.add_bullets(GUILD, ALICE, 5)
        self.assertFalse(db.deduct_bullets(GUILD, ALICE, 6))
        self.assertEqual(db.get_bullets(GUILD, ALICE), 5)

    def test_unknown_user(self):
        self.assertFalse(db.deduct_bullets(GUILD, ALICE, 1))


class SpendBulletTests(DbTestCase):
    def test_success(self):
        db.add_bullets(GUILD, ALICE, 3)
        self.assertTrue(db.spend_bullet(GUILD, ALICE))
        self.assertEqual(db.get_bullets(GUILD, ALICE), 2)

    def test_no_bullets(self):
        self.assertFalse(db.spend_bullet(GUILD, ALICE))


class ClaimDailyTests(DbTestCase):
    def test_first_claim_grants(self):
        claimed, remaining, total = db.claim_daily(GUILD, ALICE, 5, "alice")
        self.assertTrue(claimed)
        self.assertIsNone(remaining)
        self.assertEqual(total, 5)

    def test_second_claim_same_day_blocked(self):
        db.claim_daily(GUILD, ALICE, 5)
        claimed, remaining, total = db.claim_daily(GUILD, ALICE, 5)
        self.assertFalse(claimed)
        self.assertIsNotNone(remaining)
        self.assertEqual(total, 5)  # unchanged

    def test_claim_after_reset(self):
        db.claim_daily(GUILD, ALICE, 5)
        # Roll the stored claim date back a day.
        conn = db.get_connection()
        conn.execute(
            "UPDATE bullets SET last_daily='2000-01-01' WHERE guild_id=? AND user_id=?",
            (GUILD, ALICE),
        )
        conn.commit()
        conn.close()
        claimed, remaining, total = db.claim_daily(GUILD, ALICE, 5)
        self.assertTrue(claimed)
        self.assertEqual(total, 10)

    def test_claim_adds_to_existing_balance(self):
        db.add_bullets(GUILD, ALICE, 7)
        claimed, _, total = db.claim_daily(GUILD, ALICE, 5)
        self.assertTrue(claimed)
        self.assertEqual(total, 12)


class DeathrollGameTests(DbTestCase):
    def _status(self, game_id):
        conn = db.get_connection()
        row = conn.execute(
            "SELECT status, winner_id, outcome FROM deathroll_games WHERE id=?",
            (game_id,),
        ).fetchone()
        conn.close()
        return row

    def test_create_returns_id_and_marks_active(self):
        gid = db.create_deathroll_game(GUILD, ALICE, BOB, 10, "alice", "bob")
        self.assertIsInstance(gid, int)
        row = self._status(gid)
        self.assertEqual(row["status"], "active")
        self.assertIsNone(row["winner_id"])

    def test_finish_records_winner_and_outcome(self):
        gid = db.create_deathroll_game(GUILD, ALICE, BOB, 10)
        db.finish_deathroll_game(gid, BOB, "rolled_one")
        row = self._status(gid)
        self.assertEqual(row["status"], "finished")
        self.assertEqual(row["winner_id"], BOB)
        self.assertEqual(row["outcome"], "rolled_one")

    def test_finish_ignores_already_settled_game(self):
        gid = db.create_deathroll_game(GUILD, ALICE, BOB, 10)
        db.finish_deathroll_game(gid, BOB, "rolled_one")
        db.finish_deathroll_game(gid, ALICE, "timeout")  # must not overwrite
        row = self._status(gid)
        self.assertEqual(row["winner_id"], BOB)
        self.assertEqual(row["outcome"], "rolled_one")

    def test_recover_refunds_both_players_and_marks_refunded(self):
        gid = db.create_deathroll_game(GUILD, ALICE, BOB, 10, "alice", "bob")
        refunded = db.recover_deathroll_games()
        self.assertEqual(len(refunded), 1)
        self.assertEqual(db.get_bullets(GUILD, ALICE), 10)
        self.assertEqual(db.get_bullets(GUILD, BOB), 10)
        self.assertEqual(self._status(gid)["status"], "refunded")

    def test_recover_skips_finished_games(self):
        gid = db.create_deathroll_game(GUILD, ALICE, BOB, 10)
        db.finish_deathroll_game(gid, BOB, "rolled_one")
        refunded = db.recover_deathroll_games()
        self.assertEqual(refunded, [])
        self.assertEqual(db.get_bullets(GUILD, ALICE), 0)
        self.assertEqual(db.get_bullets(GUILD, BOB), 0)

    def test_recover_is_idempotent(self):
        db.create_deathroll_game(GUILD, ALICE, BOB, 10)
        db.recover_deathroll_games()
        second = db.recover_deathroll_games()  # nothing left active
        self.assertEqual(second, [])
        self.assertEqual(db.get_bullets(GUILD, ALICE), 10)  # not double-refunded


class DeathrollLeaderboardTests(DbTestCase):
    def _finished_game(self, challenger, challengee, stake, winner):
        gid = db.create_deathroll_game(
            GUILD, challenger, challengee, stake, f"u{challenger}", f"u{challengee}"
        )
        db.finish_deathroll_game(gid, winner, "rolled_one")
        return gid

    def test_empty_when_no_games(self):
        self.assertEqual(db.deathroll_leaderboard(GUILD), [])

    def test_net_win_and_loss(self):
        self._finished_game(ALICE, BOB, 10, winner=ALICE)
        board = {row["user_id"]: row for row in db.deathroll_leaderboard(GUILD)}
        self.assertEqual(board[ALICE]["net"], 10)
        self.assertEqual(board[ALICE]["wins"], 1)
        self.assertEqual(board[ALICE]["losses"], 0)
        self.assertEqual(board[BOB]["net"], -10)
        self.assertEqual(board[BOB]["losses"], 1)

    def test_accumulates_across_games_and_sorts_by_net(self):
        self._finished_game(ALICE, BOB, 10, winner=ALICE)   # A +10, B -10
        self._finished_game(BOB, ALICE, 5, winner=BOB)      # B +5,  A -5
        board = db.deathroll_leaderboard(GUILD)
        self.assertEqual([r["user_id"] for r in board], [ALICE, BOB])  # sorted by net desc
        nets = {r["user_id"]: r["net"] for r in board}
        self.assertEqual(nets[ALICE], 5)
        self.assertEqual(nets[BOB], -5)

    def test_active_and_refunded_games_excluded(self):
        db.create_deathroll_game(GUILD, ALICE, BOB, 10)  # active, never finished
        db.recover_deathroll_games()                     # -> refunded
        self.assertEqual(db.deathroll_leaderboard(GUILD), [])

    def test_other_guild_excluded(self):
        self._finished_game(ALICE, BOB, 10, winner=ALICE)
        self.assertEqual(db.deathroll_leaderboard(GUILD + 1), [])


if __name__ == "__main__":
    unittest.main()
