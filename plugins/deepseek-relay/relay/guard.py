"""R3-7: refuse to run a supervisor that could read the one-time answer code.

The code is the only proof that a human answered. A supervisor app with Screen Recording or
Accessibility can read a notification, a window, or click a dialog, so Relay refuses to start
while a configured supervisor bundle id holds either permission. The probe reads the user TCC
database read-only; when macOS will not let it be read the result is "unknown", and the
calibration step (which runs inside the real apps) must check it instead.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable

TCC_SERVICES = ("kTCCServiceAccessibility", "kTCCServiceScreenCapture")
TCC_DB = Path.home() / "Library" / "Application Support" / "com.apple.TCC" / "TCC.db"

GrantsFn = Callable[[], dict[str, set[str]] | None]


def system_tcc_grants(db: Path | None = None) -> dict[str, set[str]] | None:
    """Bundle id -> granted risky services, or None when the database cannot be read."""
    path = Path(db or TCC_DB)
    if not path.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        try:
            rows = connection.execute(
                "select service, client from access where auth_value = 2"
            ).fetchall()
        finally:
            connection.close()
    except (sqlite3.Error, OSError):
        return None
    grants: dict[str, set[str]] = {}
    for service, client in rows:
        if service in TCC_SERVICES and client:
            grants.setdefault(str(client), set()).add(str(service))
    return grants


def supervisor_risks(cfg, probe: GrantsFn | None = None) -> list[str]:
    """Reasons a configured supervisor app could read the code. Empty means safe enough."""
    try:
        bundle_ids = [str(item) for item in (cfg.get("security.supervisor_bundle_ids", []) or [])]
    except Exception:  # a malformed config must not crash the check
        return []
    if not bundle_ids:
        return []
    grants = (probe or system_tcc_grants)()
    if grants is None:
        return []
    risks: list[str] = []
    for bundle in bundle_ids:
        services = grants.get(bundle) or set()
        if services:
            risks.append(f"{bundle} ({', '.join(sorted(services))})")
    return risks


def require_clean_supervisors(cfg, probe: GrantsFn | None = None) -> list[str]:
    return supervisor_risks(cfg, probe)
