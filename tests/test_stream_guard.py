"""Characterization tests for cogs/stream_guard.py — covers the pure stream
detection and timing helpers plus the in-memory join-time keying on the cog.

Discord VoiceState objects are replaced with lightweight stand-ins exposing
just the attributes the code reads (.channel, .self_stream, .self_video).
The async on_voice_state_update handler is not tested (it needs voice mocks).
"""
import datetime
import unittest
from types import SimpleNamespace

from cogs.stream_guard import (
    StreamGuardCog,
    GUARD_WINDOW,
    _started_streaming,
    _within_window,
)


def state(channel=None, self_stream=False, self_video=False):
    return SimpleNamespace(channel=channel, self_stream=self_stream, self_video=self_video)


class StartedStreamingTests(unittest.TestCase):
    def test_stream_turned_on(self):
        self.assertTrue(_started_streaming(state(), state(self_stream=True)))

    def test_camera_turned_on(self):
        self.assertTrue(_started_streaming(state(), state(self_video=True)))

    def test_already_streaming_is_not_a_new_start(self):
        self.assertFalse(
            _started_streaming(state(self_stream=True), state(self_stream=True))
        )

    def test_stream_turned_off_is_not_a_start(self):
        self.assertFalse(_started_streaming(state(self_stream=True), state()))

    def test_no_change(self):
        self.assertFalse(_started_streaming(state(), state()))


class WithinWindowTests(unittest.TestCase):
    def setUp(self):
        self.join = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)

    def test_inside_window(self):
        now = self.join + datetime.timedelta(seconds=GUARD_WINDOW - 1)
        self.assertTrue(_within_window(self.join, now))

    def test_exactly_at_boundary_is_inside(self):
        now = self.join + datetime.timedelta(seconds=GUARD_WINDOW)
        self.assertTrue(_within_window(self.join, now))

    def test_outside_window(self):
        now = self.join + datetime.timedelta(seconds=GUARD_WINDOW + 1)
        self.assertFalse(_within_window(self.join, now))


class JoinTimeKeyingTests(unittest.TestCase):
    def test_same_member_id_in_two_guilds_does_not_collide(self):
        cog = StreamGuardCog()
        t = datetime.datetime.now(datetime.timezone.utc)
        cog._join_times[(1, 100)] = t
        cog._join_times[(2, 100)] = t
        self.assertEqual(len(cog._join_times), 2)


if __name__ == "__main__":
    unittest.main()
