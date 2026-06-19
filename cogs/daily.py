import os
import discord
from discord import app_commands
from discord.ext import commands
import db

DAILY_BULLET_AMOUNT = int(os.getenv("DAILY_BULLET_AMOUNT", "5"))


class DailyCog(commands.Cog):

    @app_commands.command(name="daily", description="Claim your daily bullet allowance")
    async def daily(self, interaction: discord.Interaction):
        claimed, remaining, total = db.claim_daily(
            interaction.guild_id,
            interaction.user.id,
            DAILY_BULLET_AMOUNT,
            interaction.user.name,
        )
        if not claimed:
            hours, remainder = divmod(int(remaining.total_seconds()), 3600)
            minutes = remainder // 60
            await interaction.response.send_message(
                f"You already claimed today's allowance. Resets at midnight EST (**{hours}h {minutes}m** from now).",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"{interaction.user.mention} claimed their daily allowance of **{DAILY_BULLET_AMOUNT}** bullet(s)! "
            f"They now have **{total}**."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(DailyCog())
