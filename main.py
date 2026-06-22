import logging
import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import db

load_dotenv()

log = logging.getLogger("drakbot")

intents = discord.Intents.default()
intents.voice_states = True

bot = commands.Bot(command_prefix="/", intents=intents)


async def setup_hook():
    db.init_db()
    refunded = db.recover_deathroll_games()
    if refunded:
        log.info("Refunded %d interrupted deathroll game(s).", len(refunded))
    refunded_bj = db.recover_blackjack_games()
    if refunded_bj:
        log.info("Refunded %d interrupted blackjack game(s).", len(refunded_bj))
    await bot.load_extension("cogs.bullets")
    await bot.load_extension("cogs.flee")
    await bot.load_extension("cogs.daily")
    await bot.load_extension("cogs.deathroll")
    await bot.load_extension("cogs.blackjack")
    await bot.load_extension("cogs.stream_guard")

bot.setup_hook = setup_hook


@bot.event
async def on_ready():
    await bot.tree.sync()
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    log.info("Synced slash commands.")


@bot.tree.command(name="ping", description="Check the bot's latency")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency * 1000)}ms")


def run(token: str | None = None):
    """Start the bot. If logging is already configured (e.g. by launcher.py),
    reuse it; otherwise let discord.py set up logging so `python main.py`
    still produces output on its own."""
    token = token or os.getenv("DISCORD_TOKEN")
    if logging.getLogger().hasHandlers():
        bot.run(token, log_handler=None)
    else:
        bot.run(token, root_logger=True)


if __name__ == "__main__":
    run()
