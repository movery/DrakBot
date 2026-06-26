import asyncio
import logging
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

import db
from blackjack_engine import BlackjackGame, BlackjackTable, hand_total, is_blackjack

log = logging.getLogger(__name__)

MIN_BET = 5
GAME_TIMEOUT = 120  # seconds a player has to act before the round auto-resolves
MAX_SEATS = 6  # players who can sit at a multiplayer table
LOBBY_SECONDS = 15  # window to sit down and wager before a table deals
TURN_TIMEOUT = 30  # seconds a multiplayer player has before being auto-stood

RESULT_LABELS = {
    "blackjack": "BLACKJACK ✅ (3:2)",
    "win": "WIN ✅",
    "push": "PUSH ➖",
    "loss": "LOSS ❌",
}


def _fmt(cards) -> str:
    return " ".join(str(c) for c in cards)


def _total_tag(cards, natural: bool | None = None) -> str:
    # A split hand can reach 21 in two cards but is not a natural blackjack, so
    # callers pass `natural` explicitly; the dealer falls back to a hand check.
    if natural is None:
        natural = is_blackjack(cards)
    if natural:
        return "(Blackjack!)"
    total, soft = hand_total(cards)
    if total > 21:
        return f"({total} • BUST)"
    if soft and total < 21:
        return f"(soft {total})"
    return f"({total})"


def _format_leaderboard(entries: list[tuple[str, int, int, int, int]]) -> str:
    """Render a monospace table from ranked (name, net, wins, losses, pushes)."""
    lines = []
    for rank, (name, net, wins, losses, pushes) in enumerate(entries, start=1):
        lines.append(f"{rank:>2}. {name[:15]:<15} {net:>+6}  ({wins}W-{losses}L-{pushes}P)")
    return "\n".join(lines)


class BlackjackView(discord.ui.View):
    def __init__(self, cog: "BlackjackCog", player: discord.Member, guild_id: int,
                 game: BlackjackGame, game_id: int):
        super().__init__(timeout=GAME_TIMEOUT)
        self.cog = cog
        self.player = player
        self.guild_id = guild_id
        self.game = game
        self.game_id = game_id
        self.escrow = game.base_bet  # bullets already deducted (base bet)
        self.message: discord.Message | None = None
        self.settlement = None
        self.net = 0
        self._settled = False

    # --- bullet helpers ----------------------------------------------------
    def _balance(self) -> int:
        return db.get_bullets(self.guild_id, self.player.id)

    def _commit(self, amount: int) -> bool:
        """Deduct an extra wager (double/split/insurance) and grow the escrow."""
        if not db.deduct_bullets(self.guild_id, self.player.id, amount, self.player.name):
            return False
        self.escrow += amount
        db.set_blackjack_escrow(self.game_id, self.escrow)
        return True

    # --- round lifecycle ---------------------------------------------------
    def _maybe_resolve(self):
        if self.game.phase == "dealer":
            self.game.play_dealer()
        if self.game.phase == "done":
            self._finalize()

    def _finalize(self):
        if self._settled:
            return
        self._settled = True
        settlement = self.game.settle()
        if settlement.total_return > 0:
            db.add_bullets(self.guild_id, self.player.id, settlement.total_return, self.player.name)
        self.net = settlement.total_return - self.escrow
        outcome = "win" if self.net > 0 else "loss" if self.net < 0 else "push"
        # Count each hand separately so a split round records every result.
        wins = sum(1 for r in settlement.hands if r.outcome in ("win", "blackjack"))
        losses = sum(1 for r in settlement.hands if r.outcome == "loss")
        pushes = sum(1 for r in settlement.hands if r.outcome == "push")
        db.finish_blackjack_game(self.game_id, self.net, outcome, wins, losses, pushes)
        self.cog._active.discard((self.guild_id, self.player.id))
        self.settlement = settlement
        log.info(
            "blackjack game %d settled: player %s (%d) wagered %d, net %+d",
            self.game_id, self.player.name, self.player.id, self.escrow, self.net,
        )
        self.stop()

    # --- rendering ---------------------------------------------------------
    def _dealer_line(self) -> str:
        game = self.game
        if game.phase in ("insurance", "player"):
            return f"**Dealer:** {game.dealer[0]} 🂠"
        return f"**Dealer:** {_fmt(game.dealer)}  {_total_tag(game.dealer)}"

    def _hand_line(self, idx: int, hand, result=None) -> str:
        game = self.game
        active = (game.phase == "player" and idx == game.active and not self._settled)
        marker = "▶ " if active else "　"
        prefix = f"Hand {idx + 1}:" if len(game.hands) > 1 else "You:"
        bet_tag = f" • bet {hand.bet}" + (" (doubled)" if hand.doubled else "")
        label = f" — {RESULT_LABELS[result.outcome]}" if result else ""
        tag = _total_tag(hand.cards, natural=hand.is_natural_blackjack)
        return f"{marker}**{prefix}** {_fmt(hand.cards)}  {tag}{bet_tag}{label}"

    def render(self) -> str:
        game = self.game
        lines = [
            f"🃏 **Blackjack** — {self.player.mention} • base bet **{game.base_bet}** bullet(s)",
            self._dealer_line(),
        ]
        results = self.settlement.hands if self.settlement else [None] * len(game.hands)
        for idx, hand in enumerate(game.hands):
            lines.append(self._hand_line(idx, hand, results[idx]))

        if game.phase == "insurance":
            lines.append(
                f"\nDealer shows an **Ace**. Insurance costs **{game.insurance_cost()}** "
                f"bullet(s) and pays 2:1 if the dealer has blackjack."
            )
        elif game.phase == "player":
            lines.append(f"\nYour move, {self.player.mention}.")
        elif self.settlement is not None:
            if self.settlement.insurance_outcome == "win":
                lines.append(f"\n🛡️ Insurance **won** (+{self.settlement.insurance_return - self.game.insurance_bet}).")
            elif self.settlement.insurance_outcome == "loss":
                lines.append(f"\n🛡️ Insurance lost (-{self.game.insurance_bet}).")
            if self.net > 0:
                summary = f"**You won {self.net} bullet(s).**"
            elif self.net < 0:
                summary = f"**You lost {abs(self.net)} bullet(s).**"
            else:
                summary = "**You broke even.**"
            lines.append(f"{summary} Balance: **{self._balance()}**.")
        return "\n".join(lines)

    # --- button state ------------------------------------------------------
    def _sync(self):
        game = self.game
        insurance_phase = game.phase == "insurance"
        player_phase = game.phase == "player"
        actions = game.available_actions()
        bal = self._balance()
        hand = game.hands[game.active] if player_phase else None

        self.hit_button.disabled = not player_phase or "hit" not in actions
        self.stand_button.disabled = not player_phase or "stand" not in actions
        self.double_button.disabled = (
            not player_phase or "double" not in actions or bal < hand.bet
        )
        self.split_button.disabled = (
            not player_phase or "split" not in actions or bal < game.base_bet
        )
        self.insurance_button.disabled = not insurance_phase or bal < game.insurance_cost()
        self.decline_button.disabled = not insurance_phase

        if self.settlement is not None:  # round over — nothing is clickable
            for child in self.children:
                child.disabled = True

    async def _refresh(self, interaction: discord.Interaction):
        self._maybe_resolve()
        self._sync()
        view = None if self.settlement is not None else self
        await interaction.response.edit_message(content=self.render(), view=view)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("This isn't your game.", ephemeral=True)
            return False
        if self._settled:
            await interaction.response.send_message("This round has already ended.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self._settled or not self.message:
            return
        self.game.stand_all()
        self._maybe_resolve()
        self._sync()
        await self.message.edit(content=self.render(), view=None)

    # --- buttons -----------------------------------------------------------
    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary, row=0)
    async def hit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        if "hit" not in self.game.available_actions():
            await interaction.response.send_message("You can't hit right now.", ephemeral=True)
            return
        self.game.hit()
        await self._refresh(interaction)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary, row=0)
    async def stand_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        if "stand" not in self.game.available_actions():
            await interaction.response.send_message("You can't stand right now.", ephemeral=True)
            return
        self.game.stand()
        await self._refresh(interaction)

    @discord.ui.button(label="Double", style=discord.ButtonStyle.success, row=0)
    async def double_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        if "double" not in self.game.available_actions():
            await interaction.response.send_message("You can't double this hand.", ephemeral=True)
            return
        if not self._commit(self.game.active_hand.bet):
            await interaction.response.send_message("You don't have enough bullets to double.", ephemeral=True)
            return
        self.game.double()
        await self._refresh(interaction)

    @discord.ui.button(label="Split", style=discord.ButtonStyle.success, row=0)
    async def split_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        if "split" not in self.game.available_actions():
            await interaction.response.send_message("You can't split this hand.", ephemeral=True)
            return
        if not self._commit(self.game.base_bet):
            await interaction.response.send_message("You don't have enough bullets to split.", ephemeral=True)
            return
        self.game.split()
        await self._refresh(interaction)

    @discord.ui.button(label="Insurance", style=discord.ButtonStyle.primary, row=1)
    async def insurance_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        if self.game.phase != "insurance":
            await interaction.response.send_message("Insurance isn't available.", ephemeral=True)
            return
        if not self._commit(self.game.insurance_cost()):
            await interaction.response.send_message("You don't have enough bullets for insurance.", ephemeral=True)
            return
        self.game.take_insurance()
        await self._refresh(interaction)

    @discord.ui.button(label="No Insurance", style=discord.ButtonStyle.danger, row=1)
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        if self.game.phase != "insurance":
            await interaction.response.send_message("Insurance isn't available.", ephemeral=True)
            return
        self.game.decline_insurance()
        await self._refresh(interaction)


# ===================== multiplayer table =====================

@dataclass
class Seat:
    """One player's place at a multiplayer table (bullet bookkeeping + their game)."""
    player: discord.Member
    game_id: int          # blackjack_games row id (created at sit time)
    escrow: int           # bullets held so far (wager + double/split/insurance)
    game: BlackjackGame | None = None  # the engine seat, attached when the round deals
    net: int = 0          # filled in at settlement, for the final summary


class WagerModal(discord.ui.Modal, title="Sit at the Blackjack Table"):
    amount = discord.ui.TextInput(
        label=f"Wager — multiple of {MIN_BET} (min {MIN_BET})",
        placeholder=f"e.g. {MIN_BET * 5}",
        required=True,
        max_length=9,
    )

    def __init__(self, lobby: "LobbyView"):
        super().__init__()
        self.lobby = lobby

    async def on_submit(self, interaction: discord.Interaction):
        await self.lobby.handle_sit(interaction, str(self.amount.value))


class LobbyView(discord.ui.View):
    """The 15-second open lobby: anyone can sit and name their own wager."""

    def __init__(self, cog: "BlackjackCog", guild_id: int, channel_id: int, host: discord.Member):
        super().__init__(timeout=LOBBY_SECONDS)
        self.cog = cog
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.host = host
        self.seats: list[Seat] = []
        self.message: discord.Message | None = None
        self._closed = False

    def render(self) -> str:
        lines = [
            f"🃏 **Multiplayer Blackjack** — opened by {self.host.mention}",
            f"Press **Sit at table** and name your wager (a multiple of {MIN_BET}). "
            f"The table deals in ~{LOBBY_SECONDS}s.",
        ]
        if self.seats:
            lines.append("\n**Seated:**")
            for seat in self.seats:
                lines.append(f"• {seat.player.mention} — **{seat.escrow}** bullet(s)")
        else:
            lines.append("\n_No players seated yet._")
        lines.append(f"\nSeats: **{len(self.seats)}/{MAX_SEATS}**")
        return "\n".join(lines)

    @discord.ui.button(label="Sit at table", style=discord.ButtonStyle.success)
    async def sit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._closed:
            await interaction.response.send_message("The lobby has already closed.", ephemeral=True)
            return
        if any(s.player.id == interaction.user.id for s in self.seats):
            await interaction.response.send_message("You're already seated.", ephemeral=True)
            return
        if len(self.seats) >= MAX_SEATS:
            await interaction.response.send_message("The table is full.", ephemeral=True)
            return
        if (self.guild_id, interaction.user.id) in self.cog._active:
            await interaction.response.send_message("You're already in a blackjack game.", ephemeral=True)
            return
        await interaction.response.send_modal(WagerModal(self))

    async def handle_sit(self, interaction: discord.Interaction, raw: str):
        try:
            amount = int(raw.strip())
        except ValueError:
            await interaction.response.send_message("Enter a whole number of bullets.", ephemeral=True)
            return
        if amount < MIN_BET or amount % MIN_BET != 0:
            await interaction.response.send_message(
                f"Your wager must be a multiple of {MIN_BET} (minimum {MIN_BET}).", ephemeral=True
            )
            return
        # Re-check everything: state may have changed while the modal was open.
        if self._closed:
            await interaction.response.send_message("The lobby has already closed.", ephemeral=True)
            return
        if any(s.player.id == interaction.user.id for s in self.seats):
            await interaction.response.send_message("You're already seated.", ephemeral=True)
            return
        if len(self.seats) >= MAX_SEATS:
            await interaction.response.send_message("The table is full.", ephemeral=True)
            return
        if (self.guild_id, interaction.user.id) in self.cog._active:
            await interaction.response.send_message("You're already in a blackjack game.", ephemeral=True)
            return
        if db.get_bullets(self.guild_id, interaction.user.id) < amount:
            await interaction.response.send_message("You don't have enough bullets.", ephemeral=True)
            return
        if not db.deduct_bullets(self.guild_id, interaction.user.id, amount, interaction.user.name):
            await interaction.response.send_message("You don't have enough bullets.", ephemeral=True)
            return

        # Create the row now so a crash during the lobby is refunded on restart.
        game_id = db.create_blackjack_game(self.guild_id, interaction.user.id, amount, interaction.user.name)
        self.cog._active.add((self.guild_id, interaction.user.id))
        self.seats.append(Seat(player=interaction.user, game_id=game_id, escrow=amount))
        await interaction.response.send_message(
            f"You're seated for **{amount}** bullet(s). Good luck!", ephemeral=True
        )
        if self.message:
            await self.message.edit(content=self.render(), view=self)

    async def on_timeout(self):
        if self._closed:
            return
        self._closed = True
        self.cog._tables.pop(self.channel_id, None)
        if not self.seats:
            if self.message:
                await self.message.edit(
                    content="🃏 Multiplayer blackjack — nobody sat down. Table closed.", view=None
                )
            return
        table = BlackjackTable()
        for seat in self.seats:
            seat.game = table.add_seat(seat.escrow)
        table.deal()
        view = TableView(self.cog, self.guild_id, self.channel_id, table, self.seats)
        self.cog._tables[self.channel_id] = view
        if self.message:
            await view.start(self.message)


class TableView(discord.ui.View):
    """Sequential multiplayer round: only the current player's buttons act."""

    def __init__(self, cog: "BlackjackCog", guild_id: int, channel_id: int,
                 table: BlackjackTable, seats: list[Seat]):
        super().__init__(timeout=None)  # the turn clock drives timeouts, not the View
        self.cog = cog
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.table = table
        self.seats = seats  # parallel to table.seats
        self.message: discord.Message | None = None
        self.settlements = None
        self._settled = False
        self._turn_task: asyncio.Task | None = None
        self._turn_gen = 0  # bumped on every turn change to invalidate a stale timer

    @property
    def current(self) -> Seat | None:
        if self.table.phase in ("insurance", "player"):
            return self.seats[self.table.turn]
        return None

    async def start(self, message: discord.Message):
        self.message = message
        self._maybe_finish()  # a dealt natural / dealer blackjack can end it at once
        self._sync()
        view = None if self._settled else self
        await message.edit(content=self.render(), view=view)
        if not self._settled:
            self._reset_turn_timer()

    # --- bullet helpers ----------------------------------------------------
    def _commit(self, seat: Seat, amount: int) -> bool:
        if not db.deduct_bullets(self.guild_id, seat.player.id, amount, seat.player.name):
            return False
        seat.escrow += amount
        db.set_blackjack_escrow(seat.game_id, seat.escrow)
        return True

    # --- round lifecycle ---------------------------------------------------
    def _maybe_finish(self):
        if self.table.phase == "done" and not self._settled:
            self._finalize()

    def _finalize(self):
        if self._settled:
            return
        self._settled = True
        self._cancel_turn_timer()
        self.settlements = self.table.settle()
        for seat, settlement in zip(self.seats, self.settlements):
            if settlement.total_return > 0:
                db.add_bullets(self.guild_id, seat.player.id, settlement.total_return, seat.player.name)
            seat.net = settlement.total_return - seat.escrow
            outcome = "win" if seat.net > 0 else "loss" if seat.net < 0 else "push"
            wins = sum(1 for r in settlement.hands if r.outcome in ("win", "blackjack"))
            losses = sum(1 for r in settlement.hands if r.outcome == "loss")
            pushes = sum(1 for r in settlement.hands if r.outcome == "push")
            db.finish_blackjack_game(seat.game_id, seat.net, outcome, wins, losses, pushes)
            self.cog._active.discard((self.guild_id, seat.player.id))
        self.cog._tables.pop(self.channel_id, None)
        log.info(
            "multiplayer blackjack settled in guild %d: %d seat(s)",
            self.guild_id, len(self.seats),
        )
        self.stop()

    def _advance(self):
        if self.table.phase == "insurance":
            self.table.advance_insurance()
        elif self.table.phase == "player":
            self.table.advance_player()
        self._maybe_finish()

    # --- turn clock --------------------------------------------------------
    def _reset_turn_timer(self):
        self._cancel_turn_timer()
        self._turn_gen += 1
        if self._settled or self.current is None:
            return
        self._turn_task = asyncio.create_task(self._turn_timer(self._turn_gen))

    def _cancel_turn_timer(self):
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        self._turn_task = None

    async def _turn_timer(self, gen: int):
        try:
            await asyncio.sleep(TURN_TIMEOUT)
        except asyncio.CancelledError:
            return
        # A button press (or a prior timeout) advanced the turn while we slept.
        if gen != self._turn_gen or self._settled:
            return
        seat = self.current
        if seat is None:
            return
        if seat.game.phase == "insurance":
            seat.game.decline_insurance()
        elif seat.game.phase == "player":
            seat.game.stand_all()
        else:
            return
        self._advance()
        self._sync()
        if self.message:
            view = None if self._settled else self
            try:
                await self.message.edit(content=self.render(), view=view)
            except discord.HTTPException:
                pass
        self._reset_turn_timer()

    # --- rendering ---------------------------------------------------------
    def _dealer_line(self) -> str:
        if self.table.phase in ("insurance", "player"):
            return f"**Dealer:** {self.table.dealer[0]} 🂠"
        return f"**Dealer:** {_fmt(self.table.dealer)}  {_total_tag(self.table.dealer)}"

    def _seat_lines(self, idx: int, seat: Seat) -> list[str]:
        game = seat.game
        results = self.settlements[idx].hands if self.settlements else [None] * len(game.hands)
        is_current = self.current is seat
        out = []
        for hidx, hand in enumerate(game.hands):
            active = is_current and game.phase == "player" and hidx == game.active and not self._settled
            marker = "▶" if active else "　"
            label_name = seat.player.display_name
            prefix = label_name if len(game.hands) == 1 else f"{label_name} #{hidx + 1}"
            bet_tag = f" • bet {hand.bet}" + (" (doubled)" if hand.doubled else "")
            result = results[hidx]
            res_label = f" — {RESULT_LABELS[result.outcome]}" if result else ""
            tag = _total_tag(hand.cards, natural=hand.is_natural_blackjack)
            out.append(f"{marker} **{prefix}:** {_fmt(hand.cards)}  {tag}{bet_tag}{res_label}")
        return out

    def render(self) -> str:
        lines = ["🃏 **Multiplayer Blackjack**", self._dealer_line(), ""]
        for idx, seat in enumerate(self.seats):
            lines.extend(self._seat_lines(idx, seat))

        if self._settled:
            lines.append("\n**Results:**")
            for seat in self.seats:
                if seat.net > 0:
                    verb = f"won **{seat.net}**"
                elif seat.net < 0:
                    verb = f"lost **{abs(seat.net)}**"
                else:
                    verb = "broke even"
                lines.append(f"• {seat.player.mention} {verb} bullet(s).")
        elif self.table.phase == "insurance":
            seat = self.current
            lines.append(
                f"\nDealer shows an **Ace**. {seat.player.mention}, take insurance for "
                f"**{seat.game.insurance_cost()}** bullet(s)? (pays 2:1 on dealer blackjack)"
            )
        elif self.table.phase == "player":
            seat = self.current
            lines.append(f"\n▶ {seat.player.mention}, your move.")
        return "\n".join(lines)

    # --- button state ------------------------------------------------------
    def _sync(self):
        if self._settled or self.current is None:
            for child in self.children:
                child.disabled = True
            return
        game = self.current.game
        insurance_phase = game.phase == "insurance"
        player_phase = game.phase == "player"
        actions = game.available_actions()
        bal = db.get_bullets(self.guild_id, self.current.player.id)
        hand = game.active_hand if player_phase else None

        self.hit_button.disabled = not player_phase or "hit" not in actions
        self.stand_button.disabled = not player_phase or "stand" not in actions
        self.double_button.disabled = (
            not player_phase or "double" not in actions or bal < (hand.bet if hand else 0)
        )
        self.split_button.disabled = (
            not player_phase or "split" not in actions or bal < game.base_bet
        )
        self.insurance_button.disabled = not insurance_phase or bal < game.insurance_cost()
        self.decline_button.disabled = not insurance_phase

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if self._settled:
            await interaction.response.send_message("This round has already ended.", ephemeral=True)
            return False
        seat = self.current
        if seat is None or interaction.user.id != seat.player.id:
            await interaction.response.send_message("It's not your turn.", ephemeral=True)
            return False
        return True

    async def _act(self, interaction: discord.Interaction):
        self._advance()
        self._sync()
        if not self._settled:
            self._reset_turn_timer()
        else:
            self._cancel_turn_timer()
        view = None if self._settled else self
        await interaction.response.edit_message(content=self.render(), view=view)

    # --- buttons -----------------------------------------------------------
    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary, row=0)
    async def hit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        if "hit" not in self.current.game.available_actions():
            await interaction.response.send_message("You can't hit right now.", ephemeral=True)
            return
        self.current.game.hit()
        await self._act(interaction)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary, row=0)
    async def stand_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        if "stand" not in self.current.game.available_actions():
            await interaction.response.send_message("You can't stand right now.", ephemeral=True)
            return
        self.current.game.stand()
        await self._act(interaction)

    @discord.ui.button(label="Double", style=discord.ButtonStyle.success, row=0)
    async def double_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        seat = self.current
        if "double" not in seat.game.available_actions():
            await interaction.response.send_message("You can't double this hand.", ephemeral=True)
            return
        if not self._commit(seat, seat.game.active_hand.bet):
            await interaction.response.send_message("You don't have enough bullets to double.", ephemeral=True)
            return
        seat.game.double()
        await self._act(interaction)

    @discord.ui.button(label="Split", style=discord.ButtonStyle.success, row=0)
    async def split_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        seat = self.current
        if "split" not in seat.game.available_actions():
            await interaction.response.send_message("You can't split this hand.", ephemeral=True)
            return
        if not self._commit(seat, seat.game.base_bet):
            await interaction.response.send_message("You don't have enough bullets to split.", ephemeral=True)
            return
        seat.game.split()
        await self._act(interaction)

    @discord.ui.button(label="Insurance", style=discord.ButtonStyle.primary, row=1)
    async def insurance_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        seat = self.current
        if seat.game.phase != "insurance":
            await interaction.response.send_message("Insurance isn't available.", ephemeral=True)
            return
        if not self._commit(seat, seat.game.insurance_cost()):
            await interaction.response.send_message("You don't have enough bullets for insurance.", ephemeral=True)
            return
        seat.game.take_insurance()
        await self._act(interaction)

    @discord.ui.button(label="No Insurance", style=discord.ButtonStyle.danger, row=1)
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        seat = self.current
        if seat.game.phase != "insurance":
            await interaction.response.send_message("Insurance isn't available.", ephemeral=True)
            return
        seat.game.decline_insurance()
        await self._act(interaction)


class BlackjackCog(commands.Cog):
    def __init__(self):
        self._active: set[tuple[int, int]] = set()
        self._tables: dict[int, object] = {}  # channel_id -> open LobbyView/TableView

    @app_commands.command(name="blackjack", description="Play a hand of blackjack, wagering your bullets")
    @app_commands.describe(amount="Bullets to wager (minimum 5)")
    @app_commands.guild_only()
    async def blackjack(self, interaction: discord.Interaction, amount: int):
        guild_id = interaction.guild_id
        player = interaction.user

        if amount < MIN_BET:
            await interaction.response.send_message(f"Minimum buy-in is {MIN_BET} bullets.", ephemeral=True)
            return
        if (guild_id, player.id) in self._active:
            await interaction.response.send_message("You already have a blackjack round in progress.", ephemeral=True)
            return
        if db.get_bullets(guild_id, player.id) < amount:
            await interaction.response.send_message("You don't have enough bullets.", ephemeral=True)
            return

        if not db.deduct_bullets(guild_id, player.id, amount, player.name):
            await interaction.response.send_message("You don't have enough bullets.", ephemeral=True)
            return

        self._active.add((guild_id, player.id))
        game = BlackjackGame(amount)
        game.deal_initial()
        game_id = db.create_blackjack_game(guild_id, player.id, amount, player.name)
        log.info(
            "blackjack game %d started in guild %d: %s (%d) for %d bullet(s)",
            game_id, guild_id, player.name, player.id, amount,
        )

        view = BlackjackView(self, player, guild_id, game, game_id)
        view._maybe_resolve()  # resolves immediately on a natural blackjack
        view._sync()
        interactive = view.settlement is None
        await interaction.response.send_message(
            content=view.render(), view=view if interactive else None
        )
        view.message = await interaction.original_response()

    @app_commands.command(
        name="blackjack-multiplayer",
        description="Open a multiplayer blackjack table; players sit and wager during a 15s lobby",
    )
    @app_commands.guild_only()
    async def blackjack_multiplayer(self, interaction: discord.Interaction):
        channel_id = interaction.channel_id
        if channel_id in self._tables:
            await interaction.response.send_message(
                "There's already a blackjack table open in this channel.", ephemeral=True
            )
            return
        lobby = LobbyView(self, interaction.guild_id, channel_id, interaction.user)
        self._tables[channel_id] = lobby
        await interaction.response.send_message(content=lobby.render(), view=lobby)
        lobby.message = await interaction.original_response()
        log.info(
            "multiplayer blackjack lobby opened in guild %d channel %d by %s (%d)",
            interaction.guild_id, channel_id, interaction.user.name, interaction.user.id,
        )

    @app_commands.command(
        name="blackjack-leaderboard",
        description="Show the blackjack leaderboard (net bullets won/lost vs the house)",
    )
    @app_commands.guild_only()
    async def blackjack_leaderboard(self, interaction: discord.Interaction):
        rows = db.blackjack_leaderboard(interaction.guild_id)
        if not rows:
            await interaction.response.send_message("No blackjack rounds have been completed yet.")
            return

        entries = []
        for row in rows[:10]:
            member = interaction.guild.get_member(row["user_id"])
            name = member.display_name if member else (row["name"] or f"User {row['user_id']}")
            entries.append((name, row["net"], row["wins"], row["losses"], row["pushes"]))

        table = _format_leaderboard(entries)
        await interaction.response.send_message(
            f"🃏 **Blackjack Leaderboard** — net bullets won/lost\n```\n{table}\n```"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(BlackjackCog())
