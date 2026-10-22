import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from relay.context import open_project  # noqa: E402
from relay.paths import Paths  # noqa: E402


class ManualClock:
    def __init__(self, start: datetime | None = None):
        self.dt = start or datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.dt

    def epoch(self) -> float:
        return self.dt.timestamp()

    def sleep(self, seconds: float) -> None:
        self.dt += timedelta(seconds=seconds)

    def advance(self, **kwargs) -> None:
        self.dt += timedelta(**kwargs)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def ctx(tmp_path: Path, clock: ManualClock):
    return open_project(tmp_path, env={}, clock=clock)


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths.for_project(tmp_path)
