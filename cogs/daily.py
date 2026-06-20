import datetime
import os
import discord
from discord import app_commands
from discord.ext import commands
import db


def _daily_amount() -> int:
    """Parse DAILY_BULLET_AMOUNT, falling back to 5 if it is unset, non-integer,
    or below 1. Done lazily so a bad value can't crash cog loading at import."""
    try:
        amount = int(os.getenv("DAILY_BULLET_AMOUNT", "5"))
    except ValueError:
        return 5
    return amount if amount >= 1 else 5


def _format_remaining(remaining: datetime.timedelta) -> str:
    """Format a positive timedelta as "Xh Ym", rounding up to whole minutes so
    the last <1 minute before reset still reads as "1m" rather than "0h 0m"."""
    total_minutes = max(1, -(-int(remaining.total_seconds()) // 60))
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes}m"


DAILY_BULLET_AMOUNT = _daily_amount()


class DailyCog(commands.Cog):

    @app_commands.command(name="daily", description="Claim your daily bullet allowance")
    @app_commands.guild_only()
    async def daily(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "You can only claim your daily allowance in a server.",
                ephemeral=True,
            )
            return
        claimed, remaining, total = db.claim_daily(
            interaction.guild_id,
            interaction.user.id,
            DAILY_BULLET_AMOUNT,
            interaction.user.name,
        )
        if not claimed:
            await interaction.response.send_message(
                f"You already claimed today's allowance. Resets at midnight EST "
                f"(**{_format_remaining(remaining)}** from now).",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"{interaction.user.mention} claimed their daily allowance of **{DAILY_BULLET_AMOUNT}** bullet(s)! "
            f"They now have **{total}**."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(DailyCog())
