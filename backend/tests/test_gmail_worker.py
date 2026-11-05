import asyncio
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import worker
from app.worker import seconds_until_gmail_sync


@pytest.mark.parametrize(
    ("now", "expected_seconds"),
    [
        (datetime(2026, 1, 15, 5, 30, tzinfo=ZoneInfo("America/New_York")), 1800),
        (
            datetime(2026, 3, 7, 7, 0, tzinfo=ZoneInfo("America/New_York")),
            22 * 3600,
        ),
        (
            datetime(2026, 10, 31, 7, 0, tzinfo=ZoneInfo("America/New_York")),
            24 * 3600,
        ),
    ],
)
def test_daily_gmail_schedule_tracks_eastern_dst(now, expected_seconds):
    assert seconds_until_gmail_sync(now) == expected_seconds


def test_gmail_loop_offloads_sync_from_event_loop(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    released_while_blocked = []

    class Token:
        checks = 0

        def exists(self):
            self.checks += 1
            return self.checks == 1

    def blocking_sync():
        started.set()
        released_while_blocked.append(release.wait(2))
        completed.set()
        return {"added": 0, "mode": "incremental"}

    token = Token()
    monkeypatch.setattr("app.worker.seconds_until_gmail_sync", lambda: 0)
    monkeypatch.setattr("app.worker.token_path", lambda: token)
    monkeypatch.setattr("app.worker.run_gmail_sync", blocking_sync)

    async def exercise():
        task = asyncio.create_task(worker.gmail_loop())
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        release.set()
        for _ in range(100):
            if completed.is_set():
                break
            await asyncio.sleep(0.01)
        assert completed.is_set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(exercise())
    assert released_while_blocked == [True]
