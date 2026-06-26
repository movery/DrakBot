"""Pure blackjack game engine — no Discord, no DB, so it can be unit-tested.

House rules (fixed): 6-deck shoe with a continuous shuffler (a fresh shuffled
shoe is built for every round, so there is nothing to count), blackjack pays
3:2, dealer stands on all 17s including soft 17 (S17), dealer peeks for
blackjack on an Ace or ten-value up-card, double down on any first two cards,
double after split (DAS) allowed, split/re-split up to four hands, split aces
receive one card each and cannot be re-split, insurance offered on a dealer
Ace, and no surrender.

Bullets are integers, so the 3:2 blackjack win is floored: a blackjack on a
bet of B returns B + (B * 3) // 2.
"""

import random
from dataclasses import dataclass, field

RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
SUITS = ["♠", "♥", "♦", "♣"]  # ♠ ♥ ♦ ♣
NUM_DECKS = 6
MAX_HANDS = 4  # a hand may be split up to a total of four hands
DEALER_STANDS_ON = 17  # dealer hits below this; stands on all 17s (S17)


@dataclass(frozen=True)
class Card:
    rank: str
    suit: str

    def __str__(self) -> str:
        return f"{self.rank}{self.suit}"


def card_value(rank: str) -> int:
    """Blackjack value of a rank; aces count as 11 here (soft-adjusted later)."""
    if rank == "A":
        return 11
    if rank in ("K", "Q", "J"):
        return 10
    return int(rank)


def build_shoe(num_decks: int = NUM_DECKS, rng: random.Random | None = None) -> list[Card]:
    rng = rng or random
    shoe = [Card(rank, suit) for _ in range(num_decks) for suit in SUITS for rank in RANKS]
    rng.shuffle(shoe)
    return shoe


def hand_total(cards: list[Card]) -> tuple[int, bool]:
    """Return (best total, is_soft). Soft means an ace is still counted as 11."""
    total = sum(card_value(c.rank) for c in cards)
    aces = sum(1 for c in cards if c.rank == "A")
    # Demote aces from 11 to 1 while we are over 21.
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    is_soft = aces > 0  # a remaining ace-as-11 makes the hand soft
    return total, is_soft


def is_blackjack(cards: list[Card]) -> bool:
    return len(cards) == 2 and hand_total(cards)[0] == 21


@dataclass
class Hand:
    cards: list[Card]
    bet: int
    doubled: bool = False
    done: bool = False
    is_ace_split: bool = False  # split from a pair of aces — one card only, no actions
    is_natural_blackjack: bool = False  # untouched two-card 21 (pays 3:2)

    @property
    def total(self) -> int:
        return hand_total(self.cards)[0]

    @property
    def is_soft(self) -> bool:
        return hand_total(self.cards)[1]

    @property
    def is_bust(self) -> bool:
        return self.total > 21


@dataclass
class HandResult:
    hand: Hand
    outcome: str  # 'blackjack' | 'win' | 'push' | 'loss'
    payout: int   # bullets returned to the player for this hand (stake + winnings)


@dataclass
class Settlement:
    hands: list[HandResult]
    total_return: int          # total bullets to credit back to the player
    insurance_outcome: str | None  # 'win' | 'loss' | None (not taken)
    insurance_return: int


class BlackjackGame:
    """A single round of blackjack for one player against the dealer.

    Phases: 'insurance' -> 'player' -> 'dealer' -> 'done'. The caller drives the
    round by reading `phase`/`available_actions()` and invoking action methods;
    all bullet accounting lives in the caller (the cog).
    """

    def __init__(self, base_bet: int, rng: random.Random | None = None,
                 shoe: list[Card] | None = None, dealer: list[Card] | None = None,
                 _test_shoe: list[Card] | None = None):
        self.base_bet = base_bet
        self.rng = rng or random.Random()
        # Cards are drawn from the front of the shoe. `_test_shoe` lets tests fix
        # the draw order; `shoe`/`dealer` let a BlackjackTable share one shoe and
        # one dealer hand across every seat (the game is then a single seat).
        if _test_shoe is not None:
            self.shoe = _test_shoe
        elif shoe is not None:
            self.shoe = shoe
        else:
            self.shoe = build_shoe(NUM_DECKS, self.rng)
        self.shared = dealer is not None  # seat of a shared-dealer table
        self.hands: list[Hand] = []
        self.dealer: list[Card] = dealer if dealer is not None else []
        self.active = 0
        self.insurance_bet = 0
        self.phase = "init"

    # --- dealing -----------------------------------------------------------
    def _draw(self) -> Card:
        if not self.shoe:  # CSM safety net: never run dry mid-round
            self.shoe = build_shoe(NUM_DECKS, self.rng)
        return self.shoe.pop(0)

    def deal_initial(self) -> None:
        player = [self._draw()]
        self.dealer = [self._draw()]
        player.append(self._draw())
        self.dealer.append(self._draw())
        hand = Hand(cards=player, bet=self.base_bet)
        hand.is_natural_blackjack = is_blackjack(player)
        self.hands = [hand]
        if self.dealer[0].rank == "A":
            self.phase = "insurance"  # offer insurance before peeking
        else:
            self._finish_dealing()

    def _finish_dealing(self) -> None:
        """After any insurance decision: peek for naturals, else start play."""
        if is_blackjack(self.dealer) or self.hands[0].is_natural_blackjack:
            self.phase = "done"  # natural(s) end the round immediately
        else:
            self.phase = "player"
            self.active = 0
            self._normalize()

    # --- shared-dealer table seat ------------------------------------------
    # When this game is a seat of a BlackjackTable the table owns the dealer:
    # it deals the dealer's cards, runs one insurance round + peek for everyone,
    # and plays the dealer once. The seat only deals/plays its own hand.
    def deal_seat(self) -> None:
        """Deal this seat's two player cards (the table already dealt the dealer)."""
        assert self.shared
        player = [self._draw(), self._draw()]
        hand = Hand(cards=player, bet=self.base_bet)
        hand.is_natural_blackjack = is_blackjack(player)
        self.hands = [hand]
        # Insurance is offered when the dealer shows an Ace; the table coordinates
        # the peek, so a seat just parks in 'insurance'/'dealt' until told to play.
        self.phase = "insurance" if self.dealer[0].rank == "A" else "dealt"

    def start_turn(self) -> None:
        """Begin this seat's player turn (table has peeked, no dealer blackjack)."""
        self.phase = "player"
        self.active = 0
        self._normalize()

    # --- insurance ---------------------------------------------------------
    @property
    def dealer_upcard(self) -> Card:
        return self.dealer[0]

    def insurance_cost(self) -> int:
        """Standard insurance is up to half the base bet (floored)."""
        return self.base_bet // 2

    def take_insurance(self) -> None:
        assert self.phase == "insurance"
        self.insurance_bet = self.insurance_cost()
        # In a shared table the table peeks once for everyone after the insurance
        # round, so a seat just waits in 'dealt'.
        if self.shared:
            self.phase = "dealt"
        else:
            self._finish_dealing()

    def decline_insurance(self) -> None:
        assert self.phase == "insurance"
        if self.shared:
            self.phase = "dealt"
        else:
            self._finish_dealing()

    # --- player turn -------------------------------------------------------
    def _normalize(self) -> None:
        """Top up freshly split hands and skip past any that can't be played,
        advancing to the dealer once every hand is resolved."""
        while self.phase == "player":
            hand = self.hands[self.active]
            if len(hand.cards) == 1:  # a just-split hand awaiting its second card
                hand.cards.append(self._draw())
                if hand.is_ace_split:
                    hand.done = True
            if not hand.done and hand.total >= 21:
                hand.done = True  # 21 auto-stands; a bust is finished too
            if hand.done:
                nxt = self._next_unfinished(self.active)
                if nxt is None:
                    self.phase = "dealer"
                    return
                self.active = nxt
                continue
            return  # landed on a playable hand

    def _next_unfinished(self, after: int) -> int | None:
        for i in range(after + 1, len(self.hands)):
            if not self.hands[i].done:
                return i
        return None

    def available_actions(self) -> set[str]:
        if self.phase != "player":
            return set()
        hand = self.hands[self.active]
        actions = {"hit", "stand"}
        two_cards = len(hand.cards) == 2
        if two_cards and not hand.is_ace_split:
            actions.add("double")  # DAS allowed; aces can't be doubled
        if (
            two_cards
            and not hand.is_ace_split
            and len(self.hands) < MAX_HANDS
            and card_value(hand.cards[0].rank) == card_value(hand.cards[1].rank)
        ):
            actions.add("split")
        return actions

    @property
    def active_hand(self) -> Hand:
        return self.hands[self.active]

    def hit(self) -> None:
        assert "hit" in self.available_actions()
        self.active_hand.cards.append(self._draw())
        self._normalize()

    def stand(self) -> None:
        assert "stand" in self.available_actions()
        self.active_hand.done = True
        self._normalize()

    def double(self) -> None:
        assert "double" in self.available_actions()
        hand = self.active_hand
        hand.bet *= 2
        hand.doubled = True
        hand.cards.append(self._draw())
        hand.done = True
        self._normalize()

    def split(self) -> None:
        assert "split" in self.available_actions()
        hand = self.active_hand
        moved = hand.cards.pop()  # second card starts the new hand
        is_aces = hand.cards[0].rank == "A"
        new_hand = Hand(cards=[moved], bet=self.base_bet, is_ace_split=is_aces)
        hand.is_ace_split = is_aces
        self.hands.insert(self.active + 1, new_hand)
        # current hand is now a single card; _normalize tops it up and, for aces,
        # marks it done so play moves on to the next hand.
        self._normalize()

    def stand_all(self) -> None:
        """Used on timeout: finish every remaining player hand and move on."""
        if self.phase == "insurance":
            self.decline_insurance()
        if self.phase == "player":
            for hand in self.hands:
                hand.done = True
            self.phase = "dealer"

    # --- dealer turn -------------------------------------------------------
    def play_dealer(self) -> None:
        assert self.phase == "dealer"
        # If every player hand busted, the dealer need not draw — reveal only.
        if any(not h.is_bust for h in self.hands):
            while hand_total(self.dealer)[0] < DEALER_STANDS_ON:
                self.dealer.append(self._draw())
        self.phase = "done"

    # --- settlement --------------------------------------------------------
    def settle(self) -> Settlement:
        """Compute outcomes and bullet returns. Pure: no side effects."""
        assert self.phase == "done"
        dealer_total = hand_total(self.dealer)[0]
        dealer_bj = is_blackjack(self.dealer)
        dealer_bust = dealer_total > 21

        results: list[HandResult] = []
        total_return = 0
        for hand in self.hands:
            bet = hand.bet
            if hand.is_bust:
                outcome, payout = "loss", 0
            elif hand.is_natural_blackjack and not dealer_bj:
                outcome, payout = "blackjack", bet + (bet * 3) // 2
            elif dealer_bj:
                # Player has no natural (handled above): naturals push, rest lose.
                outcome, payout = ("push", bet) if hand.is_natural_blackjack else ("loss", 0)
            elif dealer_bust or hand.total > dealer_total:
                outcome, payout = "win", bet * 2
            elif hand.total == dealer_total:
                outcome, payout = "push", bet
            else:
                outcome, payout = "loss", 0
            total_return += payout
            results.append(HandResult(hand, outcome, payout))

        ins_outcome = None
        ins_return = 0
        if self.insurance_bet > 0:
            if dealer_bj:
                ins_outcome = "win"
                ins_return = self.insurance_bet * 3  # 2:1 winnings + stake back
            else:
                ins_outcome = "loss"
            total_return += ins_return

        return Settlement(results, total_return, ins_outcome, ins_return)


class BlackjackTable:
    """A single round of blackjack for several players sharing one dealer and shoe.

    Each seat is a `BlackjackGame` that shares this table's shoe and dealer hand.
    The table drives the shared parts the seats can't decide alone: it deals the
    dealer, runs one insurance round and a single peek for naturals, walks the
    seats through their turns one at a time, plays the dealer once, and then each
    seat settles against the shared dealer. Bullet accounting lives in the caller.

    Phases: 'init' -> ('insurance' ->) 'player' -> 'dealer' -> 'done'. `turn` is the
    index of the current actor in `seats` during the insurance and player phases.
    """

    def __init__(self, rng: random.Random | None = None,
                 _test_shoe: list[Card] | None = None):
        self.rng = rng or random.Random()
        self.shoe = _test_shoe if _test_shoe is not None else build_shoe(NUM_DECKS, self.rng)
        self.dealer: list[Card] = []
        self.seats: list[BlackjackGame] = []
        self.phase = "init"
        self.turn = 0

    def _draw(self) -> Card:
        if not self.shoe:  # CSM safety net: never run dry mid-round
            self.shoe = build_shoe(NUM_DECKS, self.rng)
        return self.shoe.pop(0)

    def add_seat(self, base_bet: int) -> BlackjackGame:
        """Add a player seat sharing this table's shoe and dealer hand."""
        assert self.phase == "init"
        seat = BlackjackGame(base_bet, rng=self.rng, shoe=self.shoe, dealer=self.dealer)
        self.seats.append(seat)
        return seat

    @property
    def current_seat(self) -> BlackjackGame:
        return self.seats[self.turn]

    # --- dealing -----------------------------------------------------------
    def deal(self) -> None:
        assert self.seats, "a table needs at least one seat"
        self.dealer.append(self._draw())  # up-card
        self.dealer.append(self._draw())  # hole card
        for seat in self.seats:
            seat.deal_seat()
        if self.dealer[0].rank == "A":
            self.phase = "insurance"  # offer insurance seat by seat before peeking
            self.turn = 0
        else:
            self._peek_or_play()

    # --- insurance ---------------------------------------------------------
    def advance_insurance(self) -> None:
        """Move to the next seat's insurance decision, then peek once all decided."""
        assert self.phase == "insurance"
        self.turn += 1
        if self.turn >= len(self.seats):
            self._peek_or_play()

    def _peek_or_play(self) -> None:
        """Dealer peeks for blackjack; otherwise non-natural seats begin play."""
        if is_blackjack(self.dealer):
            for seat in self.seats:
                seat.phase = "done"  # round over; seats settle vs the dealer's BJ
            self.phase = "done"
            return
        for seat in self.seats:
            if seat.hands[0].is_natural_blackjack:
                seat.phase = "done"  # winner already; skip its turn
            else:
                seat.start_turn()
        nxt = self._next_playable(-1)
        if nxt is None:
            self._play_dealer()
        else:
            self.phase = "player"
            self.turn = nxt

    # --- player turns ------------------------------------------------------
    def advance_player(self) -> None:
        """Move past the current seat once it has finished all of its hands."""
        assert self.phase == "player"
        if self.current_seat.phase == "player":
            return  # still acting
        nxt = self._next_playable(self.turn)
        if nxt is None:
            self._play_dealer()
        else:
            self.turn = nxt

    def _next_playable(self, after: int) -> int | None:
        for i in range(after + 1, len(self.seats)):
            if self.seats[i].phase == "player":
                return i
        return None

    # --- dealer turn -------------------------------------------------------
    def _play_dealer(self) -> None:
        self.phase = "dealer"
        # The dealer only draws if some hand can still beat it. A bust loses and a
        # natural already won (the peek ruled out a dealer natural), so neither
        # needs the dealer to play — matching the solo engine.
        live = any(
            not h.is_bust and not h.is_natural_blackjack
            for seat in self.seats for h in seat.hands
        )
        if live:
            while hand_total(self.dealer)[0] < DEALER_STANDS_ON:
                self.dealer.append(self._draw())
        for seat in self.seats:
            seat.phase = "done"
        self.phase = "done"

    # --- settlement --------------------------------------------------------
    def settle(self) -> list[Settlement]:
        """Settle every seat against the shared dealer (caller credits bullets)."""
        assert self.phase == "done"
        return [seat.settle() for seat in self.seats]
