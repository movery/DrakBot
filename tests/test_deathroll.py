"""Characterization tests for cogs/deathroll.py — covers the pure message
builders and the in-memory game/pending state tracking on DeathrollCog.

Discord objects are replaced with lightweight stand-ins exposing just the
attributes the code reads (.id, .mention, .name).
"""
import unittest
from types import SimpleNamespace

from cogs.deathroll import (
    DeathrollGame,
    DeathrollCog,
    _build_message,
    _turn_footer,
)

GUILD = 1


def player(uid, name):
    return SimpleNamespace(id=uid, name=name, mention=f"<@{uid}>")


def make_game(challenger, challengee, stake=10):
    return DeathrollGame(
        guild_id=GUILD,
        challenger=challenger,
        challengee=challengee,
        stake=stake,
        current_max=stake,
        current_turn_id=challenger.id,
    )


class MessageBuilderTests(unittest.TestCase):
    def setUp(self):
        self.alice = player(100, "alice")
        self.bob = player(200, "bob")
        self.game = make_game(self.alice, self.bob)

    def test_build_message_includes_header_history_footer(self):
        self.game.history.append("rolled 5")
        out = _build_message(self.game, "footer line")
        lines = out.split("\n")
        self.assertIn("Deathroll", lines[0])
        self.assertEqual(lines[1], "rolled 5")
        self.assertEqual(lines[-1], "footer line")

    def test_turn_footer_points_at_current_player(self):
        self.assertIn(self.alice.mention, _turn_footer(self.game))
        self.game.current_turn_id = self.bob.id
        self.assertIn(self.bob.mention, _turn_footer(self.game))


class CogStateTests(unittest.TestCase):
    def setUp(self):
        self.cog = DeathrollCog()
        self.alice = player(100, "alice")
        self.bob = player(200, "bob")

    def test_register_game_tracks_both_players(self):
        game = make_game(self.alice, self.bob)
        self.cog._register_game(game)
        self.assertIn((GUILD, self.alice.id), self.cog._players)
        self.assertIn((GUILD, self.bob.id), self.cog._players)

    def test_end_game_clears_both_players(self):
        game = make_game(self.alice, self.bob)
        self.cog._register_game(game)
        self.cog._end_game(game)
        self.assertNotIn((GUILD, self.alice.id), self.cog._players)
        self.assertNotIn((GUILD, self.bob.id), self.cog._players)
        self.assertEqual(self.cog._games, {})

    def test_register_assigns_unique_ids(self):
        g1 = make_game(self.alice, self.bob)
        g2 = make_game(player(300, "carol"), player(400, "dave"))
        id1 = self.cog._register_game(g1)
        id2 = self.cog._register_game(g2)
        self.assertNotEqual(id1, id2)

    def test_clear_pending_removes_entries(self):
        self.cog._pending.add((GUILD, self.alice.id))
        self.cog._pending.add((GUILD, self.bob.id))
        self.cog._clear_pending(GUILD, self.alice.id, self.bob.id)
        self.assertEqual(self.cog._pending, set())

    def test_clear_pending_open_challenge_uses_zero(self):
        # Open challenges register the challenger only; challengee id is 0.
        self.cog._pending.add((GUILD, self.alice.id))
        self.cog._clear_pending(GUILD, self.alice.id, 0)
        self.assertNotIn((GUILD, self.alice.id), self.cog._pending)


if __name__ == "__main__":
    unittest.main()
