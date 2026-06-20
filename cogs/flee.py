import logging
import os
import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

BULLET_ADMIN_ROLE = os.getenv("BULLET_ADMIN_ROLE", "")


class FleeCog(commands.Cog):
    def __init__(self):
        # Per-guild flee target: guild_id -> user_id others flee from.
        self._flee_targets: dict[int, int] = {}
        # Voice channel ids with a flee batch in flight, to stop rapid channel
        # hopping from stacking overlapping move storms onto the rate limiter.
        self._in_progress: set[int] = set()

    async def _do_flee(self, channel: discord.VoiceChannel, flee_user_id: int):
        if channel.id in self._in_progress:
            return

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

        self._in_progress.add(channel.id)
        moved = 0
        try:
            for m in others:
                try:
                    await m.move_to(destination)
                    moved += 1
                except discord.HTTPException as exc:
                    # forbidden, disconnected mid-loop, or full channel — skip
                    log.warning("could not move %s out of %s: %s", m, channel, exc)
        finally:
            self._in_progress.discard(channel.id)
        log.info("fled %d member(s) from %s to %s", moved, channel, destination)

    @app_commands.command(name="flee", description="Set a user to flee from, or omit to disable")
    @app_commands.describe(user="The user others will flee from (omit to disable flee mode)")
    async def flee(self, interaction: discord.Interaction, user: discord.Member = None):
        if not discord.utils.get(interaction.user.roles, name=BULLET_ADMIN_ROLE):
            await interaction.response.send_message(
                f"You need the **{BULLET_ADMIN_ROLE}** role to use this command.",
                ephemeral=True
            )
            return

        if user:
            self._flee_targets[interaction.guild_id] = user.id
            log.info("flee mode enabled in guild %s — target %s", interaction.guild_id, user)
            await interaction.response.send_message(f"Flee mode enabled — others will flee from {user.mention}.")
            if user.voice and user.voice.channel:
                await self._do_flee(user.voice.channel, user.id)
        else:
            self._flee_targets.pop(interaction.guild_id, None)
            log.info("flee mode disabled in guild %s", interaction.guild_id)
            await interaction.response.send_message("Flee mode disabled.")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        flee_user_id = self._flee_targets.get(member.guild.id)
        if flee_user_id is None:
            return
        if member.id != flee_user_id:
            return
        if after.channel is None or before.channel == after.channel:
            return

        await self._do_flee(after.channel, flee_user_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(FleeCog())
