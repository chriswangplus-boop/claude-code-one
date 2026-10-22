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
import os
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Mapping

from .errors import StateError
from .state import atomic_write_json, read_json
from .util import canonical_json

KEY_NAME = "journal.key"
CODES_NAME = "decision-codes.json"
DECISIONS_NAME = "decisions.json"
WAIT_NAME = "wait-fingerprint.json"
ATTEMPTS_NAME = "attempts.json"
ACTIONS_NAME = "action-codes.json"
GIT_CONTROL_NAME = "git-control.json"
ANCHOR_NAME = "journal-anchor.json"
TIP_NAME = "journal-tip.json"


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

    # --- signed decision records (R3-2) -----------------------------------
    # The question's meaning must not be editable while the loop is stopped. The record is
    # written when the question is asked and is the ONLY source an answer acts on; STATE.json
    # and PENDING_DECISION.md are display copies.
    @property
    def decisions_path(self) -> Path:
        return self.root / DECISIONS_NAME

    def _decisions(self) -> dict:
        data = read_json(self.decisions_path, default={})
        return data if isinstance(data, dict) else {}

    def store_decision(self, record: dict) -> dict:
        body = {key: value for key, value in record.items() if key != "sig"}
        body["sig"] = self.sign(canonical_json(body))
        records = self._decisions()
        records[str(body.get("id"))] = body
        atomic_write_json(self.decisions_path, records, mode=0o600)
        return body

    def decision(self, decision_id: str) -> dict | None:
        record = self._decisions().get(str(decision_id))
        if not isinstance(record, dict):
            return None
        body = {key: value for key, value in record.items() if key != "sig"}
        signature = str(record.get("sig") or "")
        if not signature or not self.verify(canonical_json(body), signature):
            return None
        return body

    def clear_decision(self, decision_id: str) -> None:
        records = self._decisions()
        if str(decision_id) not in records:
            return
        records.pop(str(decision_id), None)
        atomic_write_json(self.decisions_path, records, mode=0o600)

    # --- persisted WAIT_HUMAN fingerprint (R3-2) --------------------------
    @property
    def wait_path(self) -> Path:
        return self.root / WAIT_NAME

    def set_wait_fingerprint(self, decision_id: str, state_digest: str, pending_digest: str) -> None:
        body = {
            "id": str(decision_id), "state": str(state_digest), "pending": str(pending_digest),
        }
        body["sig"] = self.sign(canonical_json(body))
        atomic_write_json(self.wait_path, body, mode=0o600)

    def wait_fingerprint(self, decision_id: str) -> tuple[str, str] | None:
        data = read_json(self.wait_path, default=None)
        if not isinstance(data, dict) or str(data.get("id") or "") != str(decision_id):
            return None
        body = {key: value for key, value in data.items() if key != "sig"}
        if not self.verify(canonical_json(body), str(data.get("sig") or "")):
            return None
        return str(data.get("state") or ""), str(data.get("pending") or "")

    def clear_wait_fingerprint(self) -> None:
        try:
            self.wait_path.unlink()
        except FileNotFoundError:
            pass

    # --- signed turn baselines (R3-4) -------------------------------------
    # File hashes and git-control hashes are captured before the pointer is typed. Persisting
    # them outside the project means a crash cannot turn what the agent wrote into the new
    # baseline on resume.
    @property
    def attempts_path(self) -> Path:
        return self.root / ATTEMPTS_NAME

    def _attempts(self) -> dict:
        data = read_json(self.attempts_path, default={})
        return data if isinstance(data, dict) else {}

    def store_attempt(self, attempt_id: str, record: dict) -> dict:
        body = {key: value for key, value in record.items() if key != "sig"}
        body["attempt_id"] = str(attempt_id)
        body["sig"] = self.sign(canonical_json(body))
        attempts = self._attempts()
        attempts[str(attempt_id)] = body
        atomic_write_json(self.attempts_path, attempts, mode=0o600)
        return body

    def attempt(self, attempt_id: str) -> dict | None:
        record = self._attempts().get(str(attempt_id))
        if not isinstance(record, dict):
            return None
        body = {key: value for key, value in record.items() if key != "sig"}
        signature = str(record.get("sig") or "")
        if not signature or not self.verify(canonical_json(body), signature):
            return None
        return body

    def clear_attempt(self, attempt_id: str) -> None:
        attempts = self._attempts()
        if str(attempt_id) not in attempts:
            return
        attempts.pop(str(attempt_id), None)
        atomic_write_json(self.attempts_path, attempts, mode=0o600)

    # --- one-time codes for CLI actions (R4-1) ----------------------------
    # `relay resume --clear-halt` and `--adopt-baseline` are human-only actions; a signature
    # alone only proves Relay wrote the event, not that a human asked for it.
    @property
    def actions_path(self) -> Path:
        return self.root / ACTIONS_NAME

    def _actions(self) -> dict:
        data = read_json(self.actions_path, default={})
        return data if isinstance(data, dict) else {}

    def store_action_code(self, action: str, code_hash: str, *, ttl: float, now: float) -> None:
        actions = self._actions()
        actions[str(action)] = {
            "hash": str(code_hash), "expires": float(now) + float(ttl), "issued": float(now),
        }
        atomic_write_json(self.actions_path, actions, mode=0o600)

    def action_code(self, action: str) -> dict | None:
        entry = self._actions().get(str(action))
        if not isinstance(entry, dict):
            return None
        return entry

    def clear_action_code(self, action: str) -> None:
        actions = self._actions()
        if str(action) not in actions:
            return
        actions.pop(str(action), None)
        atomic_write_json(self.actions_path, actions, mode=0o600)

    # --- last known good git control files (R4-2) -------------------------
    @property
    def git_control_path(self) -> Path:
        return self.root / GIT_CONTROL_NAME

    def git_control(self) -> dict | None:
        data = read_json(self.git_control_path, default=None)
        if not isinstance(data, dict):
            return None
        body = {key: value for key, value in data.items() if key != "sig"}
        if not body.get("hashes"):
            return None
        if not self.verify(canonical_json(body), str(data.get("sig") or "")):
            return None
        return body["hashes"]

    def set_git_control(self, hashes: dict) -> None:
        body = {"hashes": {str(k): str(v) for k, v in hashes.items()}}
        body["sig"] = self.sign(canonical_json(body))
        atomic_write_json(self.git_control_path, body, mode=0o600)

    def prune_attempts(self, keep: set[str]) -> list[str]:
        attempts = self._attempts()
        dropped = [key for key in attempts if key not in keep]
        if dropped:
            for key in dropped:
                attempts.pop(key, None)
            atomic_write_json(self.attempts_path, attempts, mode=0o600)
        return dropped

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

    # --- journal tip ------------------------------------------------------
    # R3-1: the anchor is the fixed start of trust (the migration point); the tip moves with
    # every append so deleting the tail is still detected. Conflating the two made "events after
    # the anchor" empty, so the signature regime covered nothing.
    @property
    def tip_path(self) -> Path:
        return self.root / TIP_NAME

    def tip(self) -> dict | None:
        data = read_json(self.tip_path, default=None)
        if not isinstance(data, dict):
            return None
        if not data.get("event_id") or not data.get("hash"):
            return None
        return data

    def set_tip(self, event_id: str, event_hash: str) -> None:
        atomic_write_json(
            self.tip_path, {"event_id": str(event_id), "hash": str(event_hash)}, mode=0o600
        )

    def describe(self) -> dict:
        return {
            "root": str(self.root),
            "has_key": self.key_path.exists(),
            "anchor": self.anchor(),
            "tip": self.tip(),
            "codes": sorted(self._codes()),
            "attempts": sorted(self._attempts()),
        }
