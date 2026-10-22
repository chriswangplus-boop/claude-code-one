"""Injectable time. Tests can pass a clock whose sleep() does not sleep."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable


class RealClock:
    def __init__(self, sleep_fn: Callable[[float], None] = time.sleep):
        self._sleep = sleep_fn

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def epoch(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._sleep(seconds)


def utc_iso(clock: RealClock | None = None) -> str:
    return (clock.now() if clock else datetime.now(timezone.utc)).isoformat(timespec="milliseconds")
