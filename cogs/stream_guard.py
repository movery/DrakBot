import os
import logging
import datetime
import discord
from discord.ext import commands

log = logging.getLogger(__name__)

STREAM_GUARD_ENABLED = os.getenv("STREAM_GUARD_ENABLED", "false").lower() == "true"
GUARD_WINDOW = 5  # seconds after joining during which streaming triggers a disconnect


def _started_streaming(before: discord.VoiceState, after: discord.VoiceState) -> bool:
    """True when the user just turned on a stream or camera this update."""
    return (after.self_stream and not before.self_stream) or \
           (after.self_video and not before.self_video)


def _within_window(join_time: datetime.datetime, now: datetime.datetime) -> bool:
    """True when ``now`` is within GUARD_WINDOW seconds of ``join_time``."""
    return (now - join_time).total_seconds() <= GUARD_WINDOW


class StreamGuardCog(commands.Cog):
    def __init__(self):
        # Keyed by (guild_id, member_id) — member ids are only unique within a
        # guild, so a bare member id would let one guild's join leak into another.
        self._join_times: dict[tuple[int, int], datetime.datetime] = {}

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if not STREAM_GUARD_ENABLED:
            return

        key = (member.guild.id, member.id)

        # Record join time when a user enters or switches to a channel; drop the
        # entry when they leave voice so the dict cannot grow unbounded.
        if after.channel is not None and before.channel != after.channel:
            self._join_times[key] = datetime.datetime.now(datetime.timezone.utc)
        elif after.channel is None:
            self._join_times.pop(key, None)
            return

        # If the user just started streaming or turned on their camera
        if not _started_streaming(before, after):
            return

        join_time = self._join_times.get(key)
        if join_time is None:
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        if _within_window(join_time, now):
            try:
                elapsed = (now - join_time).total_seconds()
                log.info(
                    "disconnecting %s in %s (channel %s) for streaming %.1fs after joining",
                    member, member.guild, after.channel, elapsed,
                )
                await member.move_to(None)
            except discord.HTTPException as exc:
                log.warning("failed to disconnect %s: %s", member, exc)  # forbidden, or disconnected between the cached read and the move
            finally:
                # Whether or not the move landed, don't act on this join again.
                self._join_times.pop(key, None)


async def setup(bot: commands.Bot):
    await bot.add_cog(StreamGuardCog())
