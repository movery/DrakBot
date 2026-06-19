import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import db

load_dotenv()

intents = discord.Intents.default()
intents.voice_states = True

bot = commands.Bot(command_prefix="/", intents=intents)


async def setup_hook():
    db.init_db()
    await bot.load_extension("cogs.bullets")
    await bot.load_extension("cogs.flee")
    await bot.load_extension("cogs.daily")
    await bot.load_extension("cogs.deathroll")

bot.setup_hook = setup_hook


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Synced slash commands.")


@bot.tree.command(name="ping", description="Check the bot's latency")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency * 1000)}ms")


if __name__ == "__main__":
    bot.run(os.getenv("DISCORD_TOKEN"))
