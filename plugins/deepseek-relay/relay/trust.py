"""Trust material that lives OUTSIDE the project folder.

Review #2 N1: the decision-code hash used to sit in `STATE.json`, inside the agent-writable
project, and the journal was an unkeyed hash chain, so an agent could rewrite both. This module
keeps the HMAC key, the decision-code hashes and the journal anchor in
`~/Library/Application Support/Relay/<project-id>/` (mode 0700), outside every turn snapshot.

Residual risk, documented rather than hidden: an agent with unrestricted shell access as the same
macOS user can read this folder. The key is never written into the project, the codes are stored
only as SHA-256 hashes, and Claude Code/Zcode ask before touching files outside the project.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Mapping

from .errors import StateError
from .state import atomic_write_json, read_json

KEY_NAME = "journal.key"
CODES_NAME = "decision-codes.json"
ANCHOR_NAME = "journal-anchor.json"


def project_id(project_dir: str | Path) -> str:
    resolved = str(Path(project_dir).expanduser().resolve()).encode("utf-8")
    return hashlib.sha256(resolved).hexdigest()[:16]


def default_base(env: Mapping[str, str] | None = None) -> Path:
    override = str((env or {}).get("RELAY_TRUST_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    if "PYTEST_CURRENT_TEST" in os.environ:
        # Tests must never write into the developer's home.
        return Path(tempfile.gettempdir()) / "relay-trust-tests"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Relay"
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / "relay"


class TrustStore:
    """The HMAC key, decision-code hashes and journal anchor for one project."""

    def __init__(self, project_dir: str | Path, env: Mapping[str, str] | None = None):
        self.project_dir = Path(project_dir).expanduser().resolve()
        self.root = default_base(env) / project_id(self.project_dir)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            os.chmod(self.root, 0o700)
        except OSError as exc:
            raise StateError(f"cannot create the Relay trust folder {self.root}: {exc}") from exc

    # --- journal key ------------------------------------------------------
    @property
    def key_path(self) -> Path:
        return self.root / KEY_NAME

    def key(self) -> bytes:
        path = self.key_path
        if path.exists():
            data = path.read_bytes()
            if len(data) >= 32:
                return data
        key = secrets.token_bytes(32)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return path.read_bytes()
        try:
            os.write(fd, key)
            os.fsync(fd)
        finally:
            os.close(fd)
        return key

    def sign(self, payload: str) -> str:
        return hmac.new(self.key(), str(payload).encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, payload: str, signature: str) -> bool:
        if not signature:
            return False
        return hmac.compare_digest(self.sign(payload), str(signature))

    # --- decision codes ---------------------------------------------------
    @property
    def codes_path(self) -> Path:
        return self.root / CODES_NAME

    def _codes(self) -> dict:
        data = read_json(self.codes_path, default={})
        return data if isinstance(data, dict) else {}

    def store_code(self, decision_id: str, code_hash: str, context: dict | None = None) -> None:
        codes = self._codes()
        codes[str(decision_id)] = {"hash": str(code_hash), **(context or {})}
        atomic_write_json(self.codes_path, codes, mode=0o600)

    def code_hash(self, decision_id: str) -> str | None:
        entry = self._codes().get(str(decision_id)) or {}
        value = str(entry.get("hash") or "")
        return value or None

    def clear_code(self, decision_id: str) -> None:
        codes = self._codes()
        if str(decision_id) not in codes:
            return
        codes.pop(str(decision_id), None)
        atomic_write_json(self.codes_path, codes, mode=0o600)

    # --- journal anchor ---------------------------------------------------
    @property
    def anchor_path(self) -> Path:
        return self.root / ANCHOR_NAME

    def anchor(self) -> dict | None:
        data = read_json(self.anchor_path, default=None)
        if not isinstance(data, dict):
            return None
        if not data.get("event_id") or not data.get("hash"):
            return None
        return data

    def set_anchor(self, event_id: str, event_hash: str) -> None:
        atomic_write_json(
            self.anchor_path, {"event_id": str(event_id), "hash": str(event_hash)}, mode=0o600
        )

    def describe(self) -> dict:
        return {
            "root": str(self.root),
            "has_key": self.key_path.exists(),
            "anchor": self.anchor(),
            "codes": sorted(self._codes()),
        }
