"""Tests for the pure helpers in cogs/daily.py — env-var parsing and the
cooldown duration formatter. Discord interaction handlers are not unit-tested.
"""
import datetime
import importlib
import os
import unittest
from unittest import mock

import cogs.daily as daily
from cogs.daily import _format_remaining


def _amount_with_env(value):
    """Reload the module with DAILY_BULLET_AMOUNT set (or unset for None)."""
    env = {k: v for k, v in os.environ.items() if k != "DAILY_BULLET_AMOUNT"}
    if value is not None:
        env["DAILY_BULLET_AMOUNT"] = value
    with mock.patch.dict(os.environ, env, clear=True):
        return importlib.reload(daily).DAILY_BULLET_AMOUNT


class DailyAmountTests(unittest.TestCase):
    def tearDown(self):
        importlib.reload(daily)  # restore module-level default for other tests

    def test_default_when_unset(self):
        self.assertEqual(_amount_with_env(None), 5)

    def test_valid_integer(self):
        self.assertEqual(_amount_with_env("10"), 10)

    def test_non_integer_falls_back(self):
        self.assertEqual(_amount_with_env("abc"), 5)

    def test_empty_string_falls_back(self):
        self.assertEqual(_amount_with_env(""), 5)

    def test_zero_falls_back(self):
        self.assertEqual(_amount_with_env("0"), 5)

    def test_negative_falls_back(self):
        self.assertEqual(_amount_with_env("-3"), 5)


class FormatRemainingTests(unittest.TestCase):
    def test_hours_and_minutes(self):
        self.assertEqual(_format_remaining(datetime.timedelta(hours=3, minutes=20)), "3h 20m")

    def test_rounds_up_partial_minute(self):
        self.assertEqual(_format_remaining(datetime.timedelta(seconds=30)), "0h 1m")

    def test_minimum_one_minute(self):
        self.assertEqual(_format_remaining(datetime.timedelta(seconds=0)), "0h 1m")

    def test_rounds_up_seconds_within_hour(self):
        # 1h 0m 30s -> 1h 1m (ceil of minutes), never drops to 1h 0m mid-hour.
        self.assertEqual(_format_remaining(datetime.timedelta(hours=1, seconds=30)), "1h 1m")


if __name__ == "__main__":
    unittest.main()
