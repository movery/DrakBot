import os
import discord
from discord import app_commands
from discord.ext import commands

BULLET_ADMIN_ROLE = os.getenv("BULLET_ADMIN_ROLE", "")

flee_user_id = None


class FleeCog(commands.Cog):

    async def _do_flee(self, channel: discord.VoiceChannel):
        others = [m for m in channel.members if m.id != flee_user_id]
        if not others:
            return

        voice_channels = sorted(
            [c for c in channel.guild.channels
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

    @app_commands.command(name="flee", description="Set a user to flee from, or omit to disable")
    @app_commands.describe(user="The user others will flee from (omit to disable flee mode)")
    async def flee(self, interaction: discord.Interaction, user: discord.Member = None):
        if not discord.utils.get(interaction.user.roles, name=BULLET_ADMIN_ROLE):
            await interaction.response.send_message(
                f"You need the **{BULLET_ADMIN_ROLE}** role to use this command.",
                ephemeral=True
            )
            return

        global flee_user_id
        flee_user_id = user.id if user else None

        if user:
            await interaction.response.send_message(f"Flee mode enabled — others will flee from {user.mention}.")
            if user.voice and user.voice.channel:
                await self._do_flee(user.voice.channel)
        else:
            await interaction.response.send_message("Flee mode disabled.")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if flee_user_id is None:
            return
        if member.id != flee_user_id:
            return
        if after.channel is None or before.channel == after.channel:
            return

        await self._do_flee(after.channel)


async def setup(bot: commands.Bot):
    await bot.add_cog(FleeCog())
