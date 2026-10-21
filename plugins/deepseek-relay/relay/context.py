"""Wire the shared lock, clock, store, and journal together for one project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .clock import RealClock
from .config import Config, load_config
from .journal import Journal
from .paths import Paths
from .state import FileLock, StateStore
from .trust import TrustStore


@dataclass
class Context:
    cfg: Config
    paths: Paths
    lock: FileLock
    clock: RealClock
    store: StateStore
    journal: Journal
    trust: TrustStore


def open_project(
    project_dir: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
    clock: RealClock | None = None,
    lock_timeout: float = 30.0,
) -> Context:
    cfg = load_config(project_dir, env=env, overrides=overrides)
    paths = Paths.for_project(cfg.project_dir)
    clock = clock or RealClock()
    lock = FileLock(paths.lock, timeout=lock_timeout)
    store = StateStore(paths, clock=clock, lock=lock)
    trust = TrustStore(cfg.project_dir, env=env)
    journal = Journal(paths, lock=lock, clock=clock, trust=trust)
    return Context(
        cfg=cfg, paths=paths, lock=lock, clock=clock, store=store, journal=journal, trust=trust
    )
