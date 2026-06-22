import logging

import discord
from discord import app_commands
from discord.ext import commands

import db
from blackjack_engine import BlackjackGame, hand_total, is_blackjack

log = logging.getLogger(__name__)

MIN_BET = 5
GAME_TIMEOUT = 120  # seconds a player has to act before the round auto-resolves

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


class BlackjackCog(commands.Cog):
    def __init__(self):
        self._active: set[tuple[int, int]] = set()

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
