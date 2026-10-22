"""Verify the bundled GPT Routing v3.0 policy (REVISION_PLAN_3 Part 4, used by Part 2).

The policy files live in `docs/codex-routing/` with a MANIFEST.json of sha256 hashes. Relay
refuses to run Codex as supervisor when a file is missing or modified, and never substitutes
Claude's routing rules.
"""

from __future__ import annotations

import json
from pathlib import Path

from .errors import ConfigError
from .util import sha256_file

POLICY_DIR = Path(__file__).resolve().parent.parent / "docs" / "codex-routing"


def policy_status(root: Path | None = None) -> dict:
    base = Path(root) if root is not None else POLICY_DIR
    manifest_path = base / "MANIFEST.json"
    if not manifest_path.exists():
        return {"ok": False, "version": None, "model": None, "errors": ["MANIFEST.json is missing"]}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "version": None, "model": None, "errors": [f"MANIFEST.json is unreadable: {exc}"]}
    errors: list[str] = []
    for name, expected in (manifest.get("files") or {}).items():
        path = base / name
        if not path.is_file():
            errors.append(f"{name} is missing")
        elif sha256_file(path) != expected:
            errors.append(f"{name} does not match the manifest hash")
    return {
        "ok": not errors,
        "version": manifest.get("version"),
        "model": manifest.get("model"),
        "efforts": manifest.get("allowed_efforts"),
        "errors": errors,
    }


def require_codex_policy(root: Path | None = None) -> dict:
    status = policy_status(root)
    if not status["ok"]:
        raise ConfigError(
            "the Codex routing policy is missing or modified: "
            + "; ".join(status["errors"])
            + ". Restore docs/codex-routing/ (see its MANIFEST.json); Relay does not substitute "
            "Claude routing."
        )
    return status
