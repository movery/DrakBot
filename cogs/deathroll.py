import asyncio
import random
from dataclasses import dataclass, field
import discord
from discord import app_commands
from discord.ext import commands
import db

ROLL_TIMEOUT = 30
WARN_AT = 20  # warn at 20s (10s remaining)


@dataclass
class DeathrollGame:
    guild_id: int
    challenger: discord.Member
    challengee: discord.Member
    stake: int
    current_max: int
    current_turn_id: int
    history: list = field(default_factory=list)
    id: int = 0  # deathroll_games row id; 0 until persisted on accept


def _header(game: DeathrollGame) -> str:
    return (
        f"**Deathroll** — {game.challenger.mention} vs {game.challengee.mention} "
        f"for **{game.stake}** bullet(s)"
    )


def _player(game: DeathrollGame, player_id: int) -> discord.Member:
    """The player whose id matches player_id."""
    return game.challenger if player_id == game.challenger.id else game.challengee


def _opponent(game: DeathrollGame, player_id: int) -> discord.Member:
    """The player who is NOT player_id."""
    return game.challengee if player_id == game.challenger.id else game.challenger


def _build_message(game: DeathrollGame, footer: str) -> str:
    return "\n".join([_header(game)] + game.history + [footer])


def _final_message(game: DeathrollGame) -> str:
    return "\n".join([_header(game)] + game.history)


def _turn_footer(game: DeathrollGame) -> str:
    turn_mention = _player(game, game.current_turn_id).mention
    return f"{turn_mention} rolls next (1–**{game.current_max}**)"


class RollView(discord.ui.View):
    def __init__(self, game: DeathrollGame, cog: "DeathrollCog"):
        super().__init__(timeout=ROLL_TIMEOUT)
        self.game = game
        self.cog = cog
        self.message: discord.Message | None = None
        self._warn_task: asyncio.Task | None = None
        self._settled = False  # guards against a roll/timeout double-payout race
        self._update_button_label()

    def _update_button_label(self):
        self.roll_button.label = f"Roll (1–{self.game.current_max})"

    def _cancel_warn_task(self):
        if self._warn_task and not self._warn_task.done():
            self._warn_task.cancel()

    def _reset_warn_task(self):
        self._cancel_warn_task()
        self._warn_task = asyncio.create_task(self._send_warning())

    async def _send_warning(self):
        await asyncio.sleep(WARN_AT)
        turn_mention = _player(self.game, self.game.current_turn_id).mention
        if self.message:
            try:
                await self.message.channel.send(
                    f"⏰ {turn_mention}, you have {ROLL_TIMEOUT - WARN_AT} seconds left to roll or you forfeit!",
                    delete_after=ROLL_TIMEOUT - WARN_AT,
                )
            except discord.HTTPException:
                pass  # channel/message gone — the game still resolves on timeout

    def _finish(self, winner: discord.Member, line: str, outcome: str):
        """Pay out the winner, settle the persisted game, and stop the view."""
        self._settled = True
        self._cancel_warn_task()
        db.add_bullets(self.game.guild_id, winner.id, self.game.stake * 2, winner.name)
        db.finish_deathroll_game(self.game.id, winner.id, outcome)
        self.cog._end_game(self.game)
        self.game.history.append(line)
        self.roll_button.disabled = True
        self.stop()

    async def on_timeout(self):
        if self._settled or not self.message:
            self._cancel_warn_task()
            return
        loser = _player(self.game, self.game.current_turn_id)
        winner = _opponent(self.game, self.game.current_turn_id)
        self._finish(winner, f"{loser.mention} ran out of time — {winner.mention} wins **{self.game.stake}** bullet(s)!", "timeout")
        await self.message.edit(content=_final_message(self.game), view=self)

    @discord.ui.button(style=discord.ButtonStyle.primary)
    async def roll_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.game
        if self._settled:
            await interaction.response.send_message("This game has already ended.", ephemeral=True)
            return
        if interaction.user.id != game.current_turn_id:
            await interaction.response.send_message("It's not your turn.", ephemeral=True)
            return

        roll = random.randint(1, game.current_max)
        roller = interaction.user

        if roll == 1:
            winner = _opponent(game, roller.id)
            self._finish(winner, f"{roller.mention} rolled **1** — LOSER! {winner.mention} wins **{game.stake}** bullet(s)!", "rolled_one")
            await interaction.response.edit_message(content=_final_message(game), view=self)
            return

        game.history.append(f"{roller.mention} rolled **{roll}**")
        game.current_max = roll
        game.current_turn_id = _opponent(game, roller.id).id
        self._update_button_label()
        self._reset_warn_task()
        await interaction.response.edit_message(
            content=_build_message(game, _turn_footer(game)),
            view=self,
        )


class ChallengeView(discord.ui.View):
    def __init__(
        self,
        challenger: discord.Member,
        challengee: discord.Member | None,
        stake: int,
        cog: "DeathrollCog",
    ):
        super().__init__(timeout=30)
        self.challenger = challenger
        self.challengee = challengee  # None = open challenge
        self.stake = stake
        self.cog = cog
        self.message: discord.Message | None = None
        if challengee is None:
            self.cancel_or_decline.label = "Cancel"

    async def on_timeout(self):
        challengee_id = self.challengee.id if self.challengee else 0
        guild_id = self.message.guild.id if self.message else 0
        self.cog._clear_pending(guild_id, self.challenger.id, challengee_id)
        if self.message:
            await self.message.edit(
                content=(
                    f"{self.challenger.mention} "
                    + (f"challenged {self.challengee.mention} to" if self.challengee else "posted an open")
                    + f" deathroll for **{self.stake}** bullet(s) — challenge expired."
                ),
                view=None,
            )

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild_id
        cog = self.cog

        if self.challengee is not None:
            if interaction.user.id != self.challengee.id:
                await interaction.response.send_message("This challenge isn't for you.", ephemeral=True)
                return
            acceptor = self.challengee
        else:
            if interaction.user.id == self.challenger.id:
                await interaction.response.send_message("You can't accept your own challenge.", ephemeral=True)
                return
            if (guild_id, interaction.user.id) in cog._players or (guild_id, interaction.user.id) in cog._pending:
                await interaction.response.send_message(
                    "You already have an active or pending deathroll.", ephemeral=True
                )
                return
            if db.get_bullets(guild_id, interaction.user.id) < self.stake:
                await interaction.response.send_message("You don't have enough bullets.", ephemeral=True)
                return
            acceptor = interaction.user

        if (guild_id, self.challenger.id) in cog._players:
            await interaction.response.send_message(
                "One of the players is already in a game.", ephemeral=True
            )
            self.stop()
            return

        if not db.deduct_bullets(guild_id, self.challenger.id, self.stake, self.challenger.name):
            await interaction.response.send_message(
                f"{self.challenger.mention} no longer has enough bullets.", ephemeral=True
            )
            return

        if not db.deduct_bullets(guild_id, acceptor.id, self.stake, acceptor.name):
            db.add_bullets(guild_id, self.challenger.id, self.stake, self.challenger.name)
            await interaction.response.send_message(
                "You don't have enough bullets.", ephemeral=True
            )
            return

        game_id = db.create_deathroll_game(
            guild_id, self.challenger.id, acceptor.id, self.stake,
            self.challenger.name, acceptor.name,
        )
        game = DeathrollGame(
            guild_id=guild_id,
            challenger=self.challenger,
            challengee=acceptor,
            stake=self.stake,
            current_max=self.stake,
            current_turn_id=self.challenger.id,
            id=game_id,
        )
        cog._clear_pending(guild_id, self.challenger.id, acceptor.id)
        cog._register_game(game)
        self.stop()

        roll_view = RollView(game, cog)
        await interaction.response.edit_message(
            content=_build_message(game, _turn_footer(game)),
            view=roll_view,
        )
        roll_view.message = await interaction.original_response()
        roll_view._reset_warn_task()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def cancel_or_decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild_id

        if self.challengee is not None:
            if interaction.user.id == self.challenger.id:
                result_text = f"{self.challenger.mention}'s deathroll challenge to {self.challengee.mention} for **{self.stake}** bullet(s) was cancelled."
            elif interaction.user.id == self.challengee.id:
                result_text = f"{self.challenger.mention} challenged {self.challengee.mention} to a deathroll for **{self.stake}** bullet(s) — declined."
            else:
                await interaction.response.send_message("This challenge isn't for you.", ephemeral=True)
                return
            self.cog._clear_pending(guild_id, self.challenger.id, self.challengee.id)
        else:
            if interaction.user.id != self.challenger.id:
                await interaction.response.send_message("Only the challenger can cancel this.", ephemeral=True)
                return
            self.cog._clear_pending(guild_id, self.challenger.id, 0)
            result_text = f"{self.challenger.mention}'s open deathroll challenge for **{self.stake}** bullet(s) was cancelled."

        self.stop()
        await interaction.response.edit_message(content=result_text, view=None)


class DeathrollCog(commands.Cog):
    def __init__(self):
        self._games: dict[int, DeathrollGame] = {}
        self._players: dict[tuple[int, int], int] = {}
        self._pending: set[tuple[int, int]] = set()

    def _register_game(self, game: DeathrollGame) -> int:
        self._games[game.id] = game
        self._players[(game.guild_id, game.challenger.id)] = game.id
        self._players[(game.guild_id, game.challengee.id)] = game.id
        return game.id

    def _clear_pending(self, guild_id: int, challenger_id: int, challengee_id: int):
        self._pending.discard((guild_id, challenger_id))
        self._pending.discard((guild_id, challengee_id))

    def _end_game(self, game: DeathrollGame):
        self._players.pop((game.guild_id, game.challenger.id), None)
        self._players.pop((game.guild_id, game.challengee.id), None)
        self._games.pop(game.id, None)

    @app_commands.command(name="deathroll", description="Challenge a user (or anyone) to a deathroll for bullets")
    @app_commands.describe(user="The user to challenge (omit for an open challenge)", amount="Number of bullets to wager")
    async def deathroll(self, interaction: discord.Interaction, amount: int, user: discord.Member = None):
        if amount < 5:
            await interaction.response.send_message("Amount must be at least 5.", ephemeral=True)
            return
        if user == interaction.user:
            await interaction.response.send_message("You can't challenge yourself.", ephemeral=True)
            return
        if user is not None and user.bot:
            await interaction.response.send_message("You can't challenge a bot.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        if (guild_id, interaction.user.id) in self._players or (guild_id, interaction.user.id) in self._pending:
            await interaction.response.send_message("You already have an active or pending deathroll.", ephemeral=True)
            return

        if user is not None:
            if (guild_id, user.id) in self._players or (guild_id, user.id) in self._pending:
                await interaction.response.send_message(
                    f"{user.mention} already has an active or pending deathroll.", ephemeral=True
                )
                return

        if db.get_bullets(guild_id, interaction.user.id) < amount:
            await interaction.response.send_message("You don't have enough bullets.", ephemeral=True)
            return

        self._pending.add((guild_id, interaction.user.id))
        if user is not None:
            self._pending.add((guild_id, user.id))

        view = ChallengeView(interaction.user, user, amount, self)
        if user is not None:
            msg = f"{interaction.user.mention} challenges {user.mention} to a deathroll for **{amount}** bullet(s)!"
        else:
            msg = f"{interaction.user.mention} is looking for a deathroll opponent for **{amount}** bullet(s)! Anyone can accept."

        await interaction.response.send_message(msg, view=view)
        view.message = await interaction.original_response()


async def setup(bot: commands.Bot):
    await bot.add_cog(DeathrollCog())
