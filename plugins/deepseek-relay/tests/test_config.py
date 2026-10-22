import json

import pytest

from relay.config import (
    ensure_usable_project,
    get_secret,
    load_config,
    secret_env_name,
    sync_dir_reason,
)
from relay.errors import ConfigError


def test_defaults_are_loaded(tmp_path):
    cfg = load_config(tmp_path, env={})
    assert cfg.get("deepseek.model") == "deepseek-flash"
    assert cfg.get("deepseek.context_window") == 1_000_000
    assert cfg.get("deepseek.context_guard") == 0.7
    assert cfg.get("max_total_rounds") == 60
    assert cfg.project_dir == tmp_path.resolve()


def test_env_overrides_types(tmp_path):
    env = {
        "RELAY_DEEPSEEK_MODEL": "deepseek-v4-pro",
        "RELAY_ROLLOVER_THRESHOLD": "0.65",
        "RELAY_DEEPSEEK_VISION": "false",
        "RELAY_MAX_TOTAL_ROUNDS": "12",
        "RELAY_POPUP_ALLOW_LIST": "read file, run tests",
    }
    cfg = load_config(tmp_path, env=env)
    assert cfg.get("deepseek.model") == "deepseek-v4-pro"
    assert cfg.get("rollover_threshold") == 0.65
    assert cfg.get("deepseek.vision") is False
    assert cfg.get("max_total_rounds") == 12
    assert cfg.get("popup_allow_list") == ["read file", "run tests"]


def test_bad_env_value_raises(tmp_path):
    with pytest.raises(ConfigError, match="bad value"):
        load_config(tmp_path, env={"RELAY_DEEPSEEK_VISION": "maybe"})


def test_project_config_overrides_defaults_and_env_wins(tmp_path):
    relay = tmp_path / ".relay"
    relay.mkdir()
    (relay / "config.json").write_text(json.dumps({"zcode_app": {"timeout_minutes": 99}}))
    cfg = load_config(tmp_path, env={"RELAY_ZCODE_APP_TIMEOUT_MINUTES": "42"})
    assert cfg.get("zcode_app.timeout_minutes") == 42
    cfg2 = load_config(tmp_path, env={})
    assert cfg2.get("zcode_app.timeout_minutes") == 99


def test_project_dir_is_never_taken_from_config_file(tmp_path):
    relay = tmp_path / ".relay"
    relay.mkdir()
    (relay / "config.json").write_text(json.dumps({"project_dir": "/tmp"}))
    cfg = load_config(tmp_path, env={})
    assert cfg.project_dir == tmp_path.resolve()


def test_sync_dir_detection(tmp_path):
    dropbox = tmp_path / "Dropbox" / "proj"
    dropbox.mkdir(parents=True)
    assert sync_dir_reason(dropbox) == "Dropbox"
    gdrive = tmp_path / "My Drive" / "proj"
    gdrive.mkdir(parents=True)
    assert sync_dir_reason(gdrive) == "Google Drive"
    assert ensure_usable_project(load_config(gdrive, env={})) == "Google Drive"

    cloud = tmp_path / "Library" / "CloudStorage" / "GoogleDrive-user" / "My Drive" / "proj"
    cloud.mkdir(parents=True)
    assert sync_dir_reason(cloud) == "Google Drive"
    assert ensure_usable_project(load_config(cloud, env={})) == "Google Drive"

    blocked = load_config(gdrive, env={})
    blocked.set("security.allow_google_drive", False)
    with pytest.raises(ConfigError, match="Google Drive"):
        ensure_usable_project(blocked)

    assert sync_dir_reason(tmp_path / "proj") is None
    with pytest.raises(ConfigError, match="Dropbox"):
        ensure_usable_project(load_config(dropbox, env={}))
    ensure_usable_project(load_config(dropbox, env={}), allow_sync=True)


def test_secret_prefers_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(secret_env_name("deepseek_api_key"), "sk-from-env")
    assert get_secret("deepseek_api_key") == "sk-from-env"


def test_validation_rejects_absurd_values(tmp_path):
    with pytest.raises(ConfigError, match="context_window"):
        load_config(tmp_path, env={"RELAY_DEEPSEEK_CONTEXT_WINDOW": "100"})
    with pytest.raises(ConfigError, match="rollover_threshold"):
        load_config(tmp_path, env={"RELAY_ROLLOVER_THRESHOLD": "1.5"})


def test_phone_replies_are_rejected(tmp_path):
    with pytest.raises(ConfigError, match="phone replies"):
        load_config(tmp_path, env={"RELAY_NTFY_PHONE_REPLY": "true"})


def test_google_drive_can_be_disabled_by_env(tmp_path):
    gdrive = tmp_path / "My Drive" / "proj"
    gdrive.mkdir(parents=True)
    with pytest.raises(ConfigError, match="Google Drive"):
        ensure_usable_project(load_config(gdrive, env={"RELAY_SECURITY_ALLOW_GOOGLE_DRIVE": "false"}))
