"""Tests for the blackjack engine (pure logic) and the blackjack db layer.

The engine draws cards from the front of its shoe, so tests fix the draw order
with `_test_shoe`. Deal order is: player card 1, dealer up-card, player card 2,
dealer hole card, then any further draws in list order.
"""
import os
import tempfile
import unittest

import db
from blackjack_engine import (
    BlackjackGame,
    Card,
    card_value,
    hand_total,
    is_blackjack,
)

GUILD = 1
ALICE = 100
BOB = 200


def c(rank, suit="♠"):
    return Card(rank, suit)


class HandMathTests(unittest.TestCase):
    def test_card_value(self):
        self.assertEqual(card_value("2"), 2)
        self.assertEqual(card_value("10"), 10)
        self.assertEqual(card_value("K"), 10)
        self.assertEqual(card_value("A"), 11)

    def test_hard_total(self):
        self.assertEqual(hand_total([c("K"), c("Q"), c("2")]), (22, False))

    def test_soft_then_hard_ace(self):
        self.assertEqual(hand_total([c("A"), c("6")]), (17, True))
        self.assertEqual(hand_total([c("A"), c("6"), c("10")]), (17, False))

    def test_multiple_aces(self):
        self.assertEqual(hand_total([c("A"), c("A")]), (12, True))
        self.assertEqual(hand_total([c("A"), c("9"), c("A"), c("K")]), (21, False))

    def test_is_blackjack(self):
        self.assertTrue(is_blackjack([c("A"), c("K")]))
        self.assertFalse(is_blackjack([c("A"), c("9"), c("A")]))  # 21 but 3 cards


class NaturalTests(unittest.TestCase):
    def test_player_blackjack_pays_3_2(self):
        game = BlackjackGame(10, _test_shoe=[c("A"), c("9"), c("K"), c("7")])
        game.deal_initial()
        self.assertEqual(game.phase, "done")  # natural ends the round
        s = game.settle()
        self.assertEqual(s.hands[0].outcome, "blackjack")
        self.assertEqual(s.total_return, 25)  # 10 stake + 15 winnings

    def test_blackjack_payout_floored_on_odd_bet(self):
        game = BlackjackGame(5, _test_shoe=[c("A"), c("9"), c("K"), c("7")])
        game.deal_initial()
        s = game.settle()
        self.assertEqual(s.total_return, 12)  # 5 + floor(5*3/2)=7

    def test_both_blackjack_push(self):
        game = BlackjackGame(10, _test_shoe=[c("A"), c("A"), c("K"), c("K")])
        game.deal_initial()
        self.assertEqual(game.phase, "insurance")  # dealer shows an Ace first
        game.decline_insurance()
        self.assertEqual(game.phase, "done")
        s = game.settle()
        self.assertEqual(s.hands[0].outcome, "push")
        self.assertEqual(s.total_return, 10)  # stake back


class DealerPlayTests(unittest.TestCase):
    def test_dealer_stands_on_soft_17(self):
        # player 17, dealer up 6 / hole A => soft 17; dealer must not draw.
        game = BlackjackGame(10, _test_shoe=[c("10"), c("6"), c("7"), c("A"), c("10")])
        game.deal_initial()
        self.assertEqual(game.phase, "player")
        game.stand()
        self.assertEqual(game.phase, "dealer")
        game.play_dealer()
        self.assertEqual(len(game.dealer), 2)  # stood on soft 17, no extra card
        self.assertEqual(hand_total(game.dealer), (17, True))
        self.assertEqual(game.settle().hands[0].outcome, "push")

    def test_dealer_hits_below_17(self):
        # player stands 19; dealer 16 must draw, draws a 5 -> 21, player loses.
        game = BlackjackGame(10, _test_shoe=[c("K"), c("8"), c("9"), c("8"), c("5")])
        game.deal_initial()
        game.stand()
        game.play_dealer()
        self.assertEqual(hand_total(game.dealer)[0], 21)
        self.assertEqual(game.settle().hands[0].outcome, "loss")

    def test_player_bust_loses_immediately(self):
        # player 20 then hits into a bust.
        game = BlackjackGame(10, _test_shoe=[c("K"), c("9"), c("Q"), c("7"), c("5")])
        game.deal_initial()
        game.hit()  # K Q + 5 = 25 bust
        self.assertTrue(game.hands[0].is_bust)
        self.assertEqual(game.phase, "dealer")
        game.play_dealer()
        s = game.settle()
        self.assertEqual(s.hands[0].outcome, "loss")
        self.assertEqual(s.total_return, 0)

    def test_player_win_returns_double(self):
        game = BlackjackGame(10, _test_shoe=[c("K"), c("8"), c("9"), c("8"), c("2")])
        game.deal_initial()
        game.stand()  # 19 vs dealer 16 -> draws 2 -> 18
        game.play_dealer()
        s = game.settle()
        self.assertEqual(s.hands[0].outcome, "win")
        self.assertEqual(s.total_return, 20)


class DoubleTests(unittest.TestCase):
    def test_double_doubles_bet_and_takes_one_card(self):
        game = BlackjackGame(10, _test_shoe=[c("6"), c("9"), c("5"), c("7"), c("10"), c("2")])
        game.deal_initial()  # player 11, dealer up 9 / hole 7 = 16
        self.assertIn("double", game.available_actions())
        game.double()
        self.assertEqual(game.hands[0].bet, 20)
        self.assertTrue(game.hands[0].doubled)
        self.assertEqual(len(game.hands[0].cards), 3)  # exactly one extra card
        self.assertEqual(game.phase, "dealer")
        game.play_dealer()  # 16 -> draws 2 -> 18; player 21 wins
        s = game.settle()
        self.assertEqual(s.total_return, 40)  # 20 stake + 20 winnings


class InsuranceTests(unittest.TestCase):
    def test_insurance_offered_only_on_ace(self):
        game = BlackjackGame(10, _test_shoe=[c("9"), c("A"), c("9"), c("K")])
        game.deal_initial()
        self.assertEqual(game.phase, "insurance")
        self.assertEqual(game.insurance_cost(), 5)

    def test_insurance_wins_when_dealer_has_blackjack(self):
        game = BlackjackGame(10, _test_shoe=[c("9"), c("A"), c("9"), c("K")])
        game.deal_initial()
        game.take_insurance()
        self.assertEqual(game.phase, "done")  # dealer natural ends round
        s = game.settle()
        self.assertEqual(s.hands[0].outcome, "loss")
        self.assertEqual(s.insurance_outcome, "win")
        self.assertEqual(s.insurance_return, 15)  # 5 stake + 10 at 2:1
        # net to player: returned 15 vs escrow 10+5=15 => break even on the round
        self.assertEqual(s.total_return, 15)

    def test_insurance_lost_when_no_blackjack(self):
        game = BlackjackGame(10, _test_shoe=[c("10"), c("A"), c("8"), c("7"), c("5")])
        game.deal_initial()
        game.take_insurance()
        self.assertEqual(game.phase, "player")  # dealer A/7, no natural
        game.stand()  # player 18
        game.play_dealer()  # dealer 18 -> stands
        s = game.settle()
        self.assertEqual(s.insurance_outcome, "loss")
        self.assertEqual(s.insurance_return, 0)
        self.assertEqual(s.hands[0].outcome, "push")

    def test_decline_insurance_continues(self):
        game = BlackjackGame(10, _test_shoe=[c("9"), c("A"), c("9"), c("7"), c("5")])
        game.deal_initial()
        game.decline_insurance()
        self.assertEqual(game.insurance_bet, 0)
        self.assertEqual(game.phase, "player")


class SplitTests(unittest.TestCase):
    def test_split_creates_two_hands_each_with_base_bet(self):
        shoe = [c("8"), c("9"), c("8"), c("6"), c("3"), c("5"), c("10")]
        game = BlackjackGame(10, _test_shoe=shoe)
        game.deal_initial()  # player 8,8 vs dealer up 9
        self.assertIn("split", game.available_actions())
        game.split()
        self.assertEqual(len(game.hands), 2)
        self.assertEqual(game.hands[0].cards, [c("8"), c("3")])
        self.assertEqual(game.hands[1].cards, [c("8")])  # dealt lazily on advance
        game.stand()  # finish hand 1; hand 2 now draws its second card
        self.assertEqual(game.hands[1].cards, [c("8"), c("5")])
        self.assertEqual(game.hands[0].bet, 10)
        self.assertEqual(game.hands[1].bet, 10)

    def test_double_after_split_allowed(self):
        shoe = [c("8"), c("9"), c("8"), c("6"), c("3"), c("5"), c("10"), c("2")]
        game = BlackjackGame(10, _test_shoe=shoe)
        game.deal_initial()
        game.split()
        self.assertIn("double", game.available_actions())  # DAS

    def test_split_aces_get_one_card_each_and_lock(self):
        shoe = [c("A"), c("9"), c("A"), c("6"), c("K"), c("9"), c("5")]
        game = BlackjackGame(10, _test_shoe=shoe)
        game.deal_initial()  # player A,A vs dealer up 9
        game.split()
        # each ace hand received exactly one card and play moved to the dealer
        self.assertEqual(len(game.hands), 2)
        self.assertEqual(game.hands[0].cards, [c("A"), c("K")])
        self.assertEqual(game.hands[1].cards, [c("A"), c("9")])
        self.assertEqual(game.phase, "dealer")
        # A+K on a split is 21 but NOT a natural blackjack (pays 1:1, not 3:2)
        self.assertFalse(game.hands[0].is_natural_blackjack)

    def test_resplit_up_to_four_hands(self):
        shoe = [c("8"), c("9"), c("8"), c("2")] + [c("8")] * 12
        game = BlackjackGame(10, _test_shoe=shoe)
        game.deal_initial()
        splits = 0
        while "split" in game.available_actions():
            game.split()
            splits += 1
            if splits > 5:
                self.fail("split did not stop at the hand cap")
        self.assertEqual(len(game.hands), 4)
        self.assertNotIn("split", game.available_actions())  # capped at four hands


class BlackjackDbTests(unittest.TestCase):
    def setUp(self):
        fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig_path = db.DB_PATH
        db.DB_PATH = self._path
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self._orig_path
        os.remove(self._path)

    def test_create_and_finish(self):
        db.add_bullets(GUILD, ALICE, 100, "alice")
        db.deduct_bullets(GUILD, ALICE, 10, "alice")
        gid = db.create_blackjack_game(GUILD, ALICE, 10, "alice")
        db.add_bullets(GUILD, ALICE, 20, "alice")  # simulate a win payout
        db.finish_blackjack_game(gid, 10, "win", wins=1)
        rows = db.blackjack_leaderboard(GUILD)
        self.assertEqual(rows[0]["net"], 10)
        self.assertEqual(rows[0]["wins"], 1)

    def test_escrow_update_and_recovery_refunds(self):
        db.add_bullets(GUILD, BOB, 100, "bob")
        db.deduct_bullets(GUILD, BOB, 10, "bob")  # base bet
        gid = db.create_blackjack_game(GUILD, BOB, 10, "bob")
        db.deduct_bullets(GUILD, BOB, 10, "bob")  # a split/double
        db.set_blackjack_escrow(gid, 20)
        self.assertEqual(db.get_bullets(GUILD, BOB), 80)
        refunded = db.recover_blackjack_games()
        self.assertEqual(len(refunded), 1)
        self.assertEqual(db.get_bullets(GUILD, BOB), 100)  # full escrow returned
        # a refunded round does not show on the leaderboard
        self.assertEqual(db.blackjack_leaderboard(GUILD), [])

    def test_leaderboard_counts_wins_losses_pushes(self):
        db.add_bullets(GUILD, ALICE, 100, "alice")
        for net, outcome, w, l, p in (
            (10, "win", 1, 0, 0),
            (-5, "loss", 0, 1, 0),
            (0, "push", 0, 0, 1),
        ):
            gid = db.create_blackjack_game(GUILD, ALICE, 5, "alice")
            db.finish_blackjack_game(gid, net, outcome, w, l, p)
        row = db.blackjack_leaderboard(GUILD)[0]
        self.assertEqual((row["net"], row["wins"], row["losses"], row["pushes"]), (5, 1, 1, 1))

    def test_split_round_counts_each_hand(self):
        # A single split round that lost one hand and pushed the other must
        # record both a loss and a push, even though the net is negative.
        db.add_bullets(GUILD, BOB, 100, "bob")
        gid = db.create_blackjack_game(GUILD, BOB, 5, "bob")
        db.finish_blackjack_game(gid, -5, "loss", wins=0, losses=1, pushes=1)
        row = db.blackjack_leaderboard(GUILD)[0]
        self.assertEqual((row["net"], row["wins"], row["losses"], row["pushes"]), (-5, 0, 1, 1))


if __name__ == "__main__":
    unittest.main()
