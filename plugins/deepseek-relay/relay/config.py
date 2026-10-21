"""Defaults + `.relay/config.json` + RELAY_* env overrides, and OS-keychain secrets.

Precedence: packaged defaults < .relay/config.json < RELAY_* environment < explicit overrides.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULTS_FILE = PACKAGE_ROOT / "config" / "defaults.json"
ENV_PREFIX = "RELAY_"
SECRET_SERVICE = "deepseek-relay"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class Config:
    def __init__(self, data: dict, project_dir: str | Path):
        self.data = data
        self.project_dir = Path(project_dir).expanduser().resolve()

    def get(self, path: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def require(self, path: str) -> Any:
        value = self.get(path, _MISSING)
        if value is _MISSING:
            raise ConfigError(f"missing config value: {path}")
        return value

    def set(self, path: str, value: Any) -> None:
        parts = path.split(".")
        cur = self.data
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value

    def as_dict(self) -> dict:
        return json.loads(json.dumps(self.data))

    @property
    def relay_dir(self) -> Path:
        return self.project_dir / ".relay"


_MISSING = object()


def _deep_merge(base: dict, extra: Mapping[str, Any]) -> dict:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _flatten(data: Mapping[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.extend(_flatten(value, path + "."))
        else:
            out.append((path, value))
    return out


def _parse_env(raw: str, default: Any) -> Any:
    if isinstance(default, bool):
        low = raw.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ConfigError(f"cannot parse {raw!r} as a boolean")
    if isinstance(default, int) and not isinstance(default, bool):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, list):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


def _apply_env(data: dict, env: Mapping[str, str]) -> None:
    for path, default in _flatten(data):
        name = ENV_PREFIX + path.replace(".", "_").upper()
        if name in env:
            try:
                value = _parse_env(env[name], default)
            except (ValueError, ConfigError) as exc:
                raise ConfigError(f"bad value for {name}: {env[name]!r}") from exc
            parts = path.split(".")
            cur = data
            for part in parts[:-1]:
                cur = cur.setdefault(part, {})
            cur[parts[-1]] = value


def load_config(
    project_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Config:
    env = os.environ if env is None else env
    raw_project = project_dir or env.get("RELAY_PROJECT_DIR") or "."
    root = Path(raw_project).expanduser().resolve()

    data = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
    project_file = root / ".relay" / "config.json"
    if project_file.exists():
        try:
            project_data = json.loads(project_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{project_file} is not valid JSON: {exc}") from exc
        if not isinstance(project_data, dict):
            raise ConfigError(f"{project_file} must contain a JSON object")
        _deep_merge(data, project_data)

    _apply_env(data, env)
    if overrides:
        _deep_merge(data, overrides)

    data["project_dir"] = "."
    cfg = Config(data, root)
    validate_config(cfg)
    return cfg


def validate_config(cfg: Config) -> None:
    if not cfg.project_dir.exists():
        raise ConfigError(f"project folder does not exist: {cfg.project_dir}")
    for key, lo, hi in (
        ("rollover_threshold", 0.4, 0.95),
        ("deepseek.context_guard", 0.4, 0.95),
        ("host.context_guard", 0.4, 0.95),
    ):
        value = float(cfg.get(key, 0))
        if not lo <= value <= hi:
            raise ConfigError(f"{key} must be between {lo} and {hi}, got {value}")
    if int(cfg.get("deepseek.context_window", 0)) < 10_000:
        raise ConfigError("deepseek.context_window is implausibly small")
    if not str(cfg.get("deepseek.model", "")).strip():
        raise ConfigError("deepseek.model is empty")
    for key in ("poll_seconds", "screen_check_seconds", "decision_poll_seconds"):
        if float(cfg.get(key, 0)) <= 0:
            raise ConfigError(f"{key} must be positive")
    if not cfg.get("popup_allow_list"):
        raise ConfigError("popup_allow_list must not be empty")
    if bool(cfg.get("ntfy.phone_reply", False)):
        raise ConfigError(
            "phone replies are not implemented. Answers are accepted only locally "
            "(`relay decide` or the ANSWER line in PENDING_DECISION.md)."
        )


# --- cloud-sync folders ---------------------------------------------------

def sync_dir_reason(path: str | Path) -> str | None:
    """Return the name of the sync service if `path` lives inside a synced folder."""
    resolved = Path(path).expanduser().resolve()
    parts = [part.lower() for part in resolved.parts]
    joined = "/".join(parts)
    if "library/mobile documents" in joined:
        return "iCloud Drive"
    for part in parts:
        if part == "dropbox":
            return "Dropbox"
        if part.startswith("onedrive"):
            return "OneDrive"
        if part in ("google drive", "my drive") or part.startswith("googledrive-"):
            return "Google Drive"
    if "library/cloudstorage" in joined:
        return "a cloud-synced folder"
    return None


def ensure_usable_project(cfg: Config, allow_sync: bool = False) -> str | None:
    """Raise for a risky sync folder, except Google Drive when explicitly permitted.

    Returns the detected sync service, if any, so the caller can print one warning.
    """
    reason = sync_dir_reason(cfg.project_dir)
    if not reason:
        return None
    if reason == "Google Drive" and bool(cfg.get("security.allow_google_drive", True)):
        return reason
    if allow_sync:
        return reason
    raise ConfigError(
        f"project folder is inside {reason}: {cfg.project_dir}\n"
        "Cloud sync creates conflict copies and can corrupt .relay state. "
        "Move the project to a local folder, or pass --allow-sync-dir to override."
    )


def sync_warning(reason: str) -> str:
    if reason == "Google Drive":
        return (
            "Google Drive is allowed here (security.allow_google_drive=true). "
            "Pause or quit Google Drive sync while Relay is running; .relay writes are atomic "
            "and every turn is committed, but an active sync client can still create conflict copies."
        )
    return (
        f"{reason} was explicitly allowed. Keep sync paused while Relay runs; "
        "conflict copies can corrupt .relay state."
    )


# --- secrets --------------------------------------------------------------

def secret_env_name(name: str) -> str:
    return ENV_PREFIX + name.upper()


def _keyring():
    try:
        import keyring  # type: ignore

        return keyring
    except Exception:
        return None


def _security(args: list[str]) -> subprocess.CompletedProcess | None:
    exe = shutil.which("security")
    if not exe:
        return None
    try:
        return subprocess.run(
            [exe, *args], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


def get_secret(name: str, env: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    from_env = env.get(secret_env_name(name))
    if from_env:
        return from_env.strip()
    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(SECRET_SERVICE, name)
            if value:
                return value
        except Exception:
            pass
    result = _security(["find-generic-password", "-s", SECRET_SERVICE, "-a", name, "-w"])
    if result and result.returncode == 0:
        return result.stdout.strip()
    return None


def set_secret(name: str, value: str) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(SECRET_SERVICE, name, value)
            return
        except Exception:
            pass
    result = _security(
        ["add-generic-password", "-U", "-s", SECRET_SERVICE, "-a", name, "-w", value]
    )
    if not result or result.returncode != 0:
        raise ConfigError(
            "could not store secret in the OS keychain. "
            f"Set it in the environment as {secret_env_name(name)} for this run instead."
        )


def delete_secret(name: str) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(SECRET_SERVICE, name)
        except Exception:
            pass
    _security(["delete-generic-password", "-s", SECRET_SERVICE, "-a", name])


def api_key(env: Mapping[str, str] | None = None) -> str | None:
    return get_secret("deepseek_api_key", env)


def ntfy_topic(env: Mapping[str, str] | None = None) -> str | None:
    return get_secret("ntfy_topic", env)


def ntfy_token(env: Mapping[str, str] | None = None) -> str | None:
    return get_secret("ntfy_token", env)
