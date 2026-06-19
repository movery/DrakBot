import os
import datetime
import discord
from discord.ext import commands

STREAM_GUARD_ENABLED = os.getenv("STREAM_GUARD_ENABLED", "false").lower() == "true"
GUARD_WINDOW = 5  # seconds after joining during which streaming triggers a disconnect


class StreamGuardCog(commands.Cog):
    def __init__(self):
        self._join_times: dict[int, datetime.datetime] = {}

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if not STREAM_GUARD_ENABLED:
            return

        # Record join time when a user enters or switches to a channel
        if after.channel is not None and before.channel != after.channel:
            self._join_times[member.id] = datetime.datetime.now(datetime.timezone.utc)
        elif after.channel is None:
            self._join_times.pop(member.id, None)
            return

        # If the user just started streaming or turned on their camera
        started_streaming = (after.self_stream and not before.self_stream) or \
                            (after.self_video and not before.self_video)
        if not started_streaming:
            return

        join_time = self._join_times.get(member.id)
        if join_time is None:
            return

        elapsed = (datetime.datetime.now(datetime.timezone.utc) - join_time).total_seconds()
        if elapsed <= GUARD_WINDOW:
            try:
                await member.move_to(None)
            except discord.Forbidden:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(StreamGuardCog())
