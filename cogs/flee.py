import os
import discord
from discord import app_commands
from discord.ext import commands

FLEE_USER_ID = int(os.getenv("FLEE_USER_ID", "0"))
BULLET_ADMIN_ROLE = os.getenv("BULLET_ADMIN_ROLE", "")

flee_enabled = False


class FleeCog(commands.Cog):

    @app_commands.command(name="flee", description="Enable or disable flee mode")
    @app_commands.describe(enabled="Whether flee mode should be active")
    async def flee(self, interaction: discord.Interaction, enabled: bool):
        if not discord.utils.get(interaction.user.roles, name=BULLET_ADMIN_ROLE):
            await interaction.response.send_message(
                f"You need the **{BULLET_ADMIN_ROLE}** role to use this command.",
                ephemeral=True
            )
            return

        global flee_enabled
        flee_enabled = enabled
        state = "enabled" if enabled else "disabled"
        await interaction.response.send_message(f"Flee mode {state}.")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if not flee_enabled or FLEE_USER_ID == 0:
            return
        if member.id != FLEE_USER_ID:
            return
        if after.channel is None or before.channel == after.channel:
            return

        channel = after.channel
        others = [m for m in channel.members if m.id != FLEE_USER_ID]
        if not others:
            return

        voice_channels = sorted(
            [c for c in member.guild.channels
             if isinstance(c, discord.VoiceChannel) and c.category_id == channel.category_id],
            key=lambda c: c.position
        )
        current_idx = next((i for i, c in enumerate(voice_channels) if c.id == channel.id), None)
        if current_idx is None:
            return

        if current_idx + 1 < len(voice_channels):
            destination = voice_channels[current_idx + 1]
        elif current_idx > 0:
            destination = voice_channels[current_idx - 1]
        else:
            return

        for m in others:
            try:
                await m.move_to(destination)
            except discord.Forbidden:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(FleeCog())
