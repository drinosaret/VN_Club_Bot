"""Healthcheck heartbeat: the connection watchdog bumps the log file's mtime
while connected so the Docker healthcheck (which reads the mtime age) stays
green during quiet periods, instead of false-flapping to `unhealthy`.
"""
import os
import time

import pytest

from lib.bot import VNClubBot


@pytest.fixture
def bot(tmp_path):
    # Construction opens no gateway connection (no token needed); it only sets
    # up internal state, so this is safe to instantiate directly in tests.
    return VNClubBot(path_to_db=str(tmp_path / "db.sqlite3"))


def _make_stale(path, age_seconds=4000):
    """Create a file with an mtime well past the health threshold."""
    path.write_text("x")
    old = time.time() - age_seconds
    os.utime(path, (old, old))
    return old


def test_touch_health_refreshes_mtime(bot, tmp_path):
    health = tmp_path / "hikaru_bot.log"
    _make_stale(health)
    bot._health_file = str(health)

    bot._touch_health()

    assert time.time() - os.path.getmtime(health) < 5


def test_touch_health_swallows_missing_file(bot, tmp_path):
    bot._health_file = str(tmp_path / "does-not-exist.log")
    # Must not raise even though the file is absent.
    bot._touch_health()


async def test_watchdog_touches_when_ready(bot, tmp_path, monkeypatch):
    health = tmp_path / "hikaru_bot.log"
    _make_stale(health)
    bot._health_file = str(health)
    monkeypatch.setattr(bot, "is_ready", lambda: True)

    await bot._connection_watchdog.coro(bot)

    assert time.time() - os.path.getmtime(health) < 5


async def test_watchdog_skips_touch_when_not_ready(bot, tmp_path, monkeypatch):
    health = tmp_path / "hikaru_bot.log"
    old = _make_stale(health)
    bot._health_file = str(health)
    monkeypatch.setattr(bot, "is_ready", lambda: False)
    # Recent heartbeat so the disconnect branch doesn't try to exit the process.
    bot._last_heartbeat = time.time()

    await bot._connection_watchdog.coro(bot)

    # Disconnected: health is intentionally left stale. The watchdog exits the
    # process after a sustained disconnect, well before the health window ends.
    assert os.path.getmtime(health) == pytest.approx(old, abs=1)
