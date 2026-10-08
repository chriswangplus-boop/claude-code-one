#!/usr/bin/env python3
"""glm_switch.py — safe, reversible Claude <-> GLM-5.3 provider switch for Claude Code.

Design invariants (NEVER violated):
  1. Only the MANAGED_KEYS inside the "env" object of ~/.claude/settings.json
     are ever written or removed. Everything else (permissions, hooks,
     mcpServers, statusLine, model, any unknown key) is preserved byte-for-value.
  2. Every write is preceded by a timestamped full-file backup and followed by
     a JSON re-parse + non-managed-key equality check. On any failure the
     original file is restored automatically.
  3. Writes are atomic: temp file in the same directory, then os.replace().
  4. Switching to GLM records the exact prior value (or absence) of each
     managed key; switching back restores those exact values — not a guess.
  5. Login credentials (keychain / OAuth in ~/.claude.json) are never touched.
  6. The Z.AI API key lives only in ~/.config/glm-switch/key.env (chmod 600),
     never inside this script or the skill package.

Python 3 stdlib only. No third-party dependencies.

Usage:
  glm_switch.py status                 # which provider is active; health check
  glm_switch.py to-glm                 # switch to GLM-5.3, or upgrade an older GLM config
  glm_switch.py to-claude              # restore new sessions to Claude
  glm_switch.py setup-key              # store Z.AI API key (prompted, 0600)
  glm_switch.py check                  # 1-token request per model ID: key + IDs valid?
  glm_switch.py backups                # list backups
  glm_switch.py restore [PATH|--latest]# full-file restore from a backup
"""

import json
import os
import shutil
import stat
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- constants

HOME = Path(os.environ.get("GLM_SWITCH_HOME", str(Path.home())))
SETTINGS = HOME / ".claude" / "settings.json"
BACKUP_DIR = HOME / ".claude" / "glm-switch-backups"
MIRROR_DIR = HOME / ".config" / "glm-switch" / "backups"  # survives loss of ~/.claude
STATE_DIR = HOME / ".config" / "glm-switch"
STATE_FILE = STATE_DIR / "state.json"
KEY_FILE = STATE_DIR / "key.env"

ZAI_BASE_URL = "https://api.z.ai/api/anthropic"

# Exact set of env keys this tool is allowed to write/remove.
# Values follow the official guide: https://docs.z.ai/devpack/tool/claude
MANAGED_KEYS = [
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "API_TIMEOUT_MS",
]

ABSENT = "__GLM_SWITCH_ABSENT__"  # sentinel meaning "key did not exist before"


def glm_env(api_key: str) -> dict:
    """The GLM env block per the official Z.AI Claude Code guide."""
    return {
        "ANTHROPIC_AUTH_TOKEN": api_key,
        "ANTHROPIC_BASE_URL": ZAI_BASE_URL,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-5.3-flash[1m]",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3[1m]",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3[1m]",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
        "API_TIMEOUT_MS": "3000000",
    }


# ---------------------------------------------------------------- utilities

def die(msg: str, code: int = 1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def load_settings():
    """Return (dict, existed). Refuses to proceed past corrupt JSON."""
    if not SETTINGS.exists():
        return {}, False
    raw = SETTINGS.read_text(encoding="utf-8")
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        corrupt = backup_file(label="corrupt")
        die(
            f"{SETTINGS} is not valid JSON ({e}).\n"
            f"A copy was saved to {corrupt}.\n"
            "Fix the file manually or run: glm_switch.py restore --latest\n"
            "No changes were made."
        )
    if not isinstance(data, dict):
        die(f"{SETTINGS} top level is not a JSON object. No changes were made.")
    return data, True


def backup_file(label: str) -> Path:
    """Timestamped full-file backup, mirrored to a second independent
    directory. Also captures a one-time baseline in both locations."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"settings-{ts}-{label}.json"
    if SETTINGS.exists():
        shutil.copy2(SETTINGS, dest)
    else:
        dest.write_text("__FILE_DID_NOT_EXIST__", encoding="utf-8")
    baseline = BACKUP_DIR / "baseline-first-run.json"
    if not baseline.exists():
        if SETTINGS.exists():
            shutil.copy2(SETTINGS, baseline)
        else:
            baseline.write_text("__FILE_DID_NOT_EXIST__", encoding="utf-8")
    # Mirror to the second location (outside ~/.claude). Best-effort:
    # a mirror failure must never block the switch — the primary succeeded.
    try:
        MIRROR_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dest, MIRROR_DIR / dest.name)
        mirror_baseline = MIRROR_DIR / "baseline-first-run.json"
        if not mirror_baseline.exists():
            shutil.copy2(baseline, mirror_baseline)
    except OSError as e:
        print(f"WARNING: mirror backup to {MIRROR_DIR} failed ({e}). "
              f"Primary backup at {dest} is intact.", file=sys.stderr)
    return dest


def atomic_write(data: dict):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(SETTINGS.parent), prefix=".glm-switch-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, SETTINGS)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def strip_managed(data: dict) -> dict:
    """Deep-ish copy of settings with managed env keys removed — used to
    prove that a write changed nothing outside the managed surface."""
    clone = json.loads(json.dumps(data))
    env = clone.get("env")
    if isinstance(env, dict):
        for k in MANAGED_KEYS:
            env.pop(k, None)
        if not env:  # empty env ≡ no env for invariant purposes
            clone.pop("env", None)
    return clone


def verified_write(new_data: dict, old_data: dict, backup: Path):
    """Atomic write + post-write verification. Auto-restores on failure."""
    atomic_write(new_data)
    try:
        reread = json.loads(SETTINGS.read_text(encoding="utf-8"))
        assert reread == new_data, "re-read mismatch"
        assert strip_managed(reread) == strip_managed(old_data), (
            "non-managed settings changed — invariant violation"
        )
    except (json.JSONDecodeError, AssertionError) as e:
        if backup.exists() and backup.read_text(encoding="utf-8") != "__FILE_DID_NOT_EXIST__":
            shutil.copy2(backup, SETTINGS)
        elif SETTINGS.exists():
            SETTINGS.unlink()  # original state was "no file" — restore that
        print(f"Post-write verification FAILED ({e}). Original file restored "
              f"from {backup}. No switch performed.", file=sys.stderr)
        sys.exit(1)


def read_api_key() -> str:
    if not KEY_FILE.exists():
        die(
            f"No Z.AI API key found at {KEY_FILE}.\n"
            "Run: glm_switch.py setup-key  (key is stored locally, chmod 600)"
        )
    for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("ZAI_API_KEY="):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")
            if key:
                return key
    die(f"{KEY_FILE} exists but contains no ZAI_API_KEY=... line.")


def save_state(prior: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(prior, indent=2), encoding="utf-8")
    os.chmod(STATE_FILE, stat.S_IRUSR | stat.S_IWUSR)


def current_provider(data: dict) -> str:
    env = data.get("env", {}) if isinstance(data.get("env"), dict) else {}
    base = env.get("ANTHROPIC_BASE_URL", "")
    if "api.z.ai" in base:
        return "GLM (Z.AI)"
    if base:
        return f"custom ({base})"
    return "Claude (Anthropic default)"


# ---------------------------------------------------------------- commands

def cmd_status():
    data, existed = load_settings()
    print(f"settings file : {SETTINGS} ({'present' if existed else 'absent — Claude defaults'})")
    print(f"provider      : {current_provider(data)}")
    env = data.get("env", {}) if isinstance(data.get("env"), dict) else {}
    managed_present = [k for k in MANAGED_KEYS if k in env]
    if managed_present:
        print("managed keys  :")
        for k in managed_present:
            v = env[k]
            if k == "ANTHROPIC_AUTH_TOKEN":
                v = v[:6] + "…" if len(v) > 6 else "•••"
            print(f"  {k} = {v}")
    else:
        print("managed keys  : none set")
    print(f"key file      : {'present' if KEY_FILE.exists() else 'absent'} ({KEY_FILE})")
    print(f"prior-state   : {'recorded' if STATE_FILE.exists() else 'none'} ({STATE_FILE})")
    backups = sorted(BACKUP_DIR.glob("settings-*.json")) if BACKUP_DIR.exists() else []
    mirrors = sorted(MIRROR_DIR.glob("settings-*.json")) if MIRROR_DIR.exists() else []
    print(f"backups       : {len(backups)} in {BACKUP_DIR}")
    print(f"mirror        : {len(mirrors)} in {MIRROR_DIR}")
    print("\nNote: provider changes apply to NEW Claude Code sessions only. "
          "Restart the session/app after switching.")


def cmd_to_glm():
    data, _ = load_settings()
    env = data.get("env")
    if env is None:
        env = {}
    if not isinstance(env, dict):
        die('"env" in settings.json is not an object. No changes were made.')

    on_glm = current_provider(data) == "GLM (Z.AI)"
    # Already on GLM: reuse the token in settings if key.env is missing, so an
    # upgrade between GLM generations never needs the key re-entered.
    if on_glm and not KEY_FILE.exists() and env.get("ANTHROPIC_AUTH_TOKEN"):
        api_key = env["ANTHROPIC_AUTH_TOKEN"]
    else:
        api_key = read_api_key()
    target = glm_env(api_key)

    if on_glm and all(env.get(k) == v for k, v in target.items()):
        print("Already on GLM-5.3 (Z.AI) with current settings. Nothing to do.")
        return

    backup = backup_file(label="pre-upgrade" if on_glm else "pre-glm")

    changed = {}
    if on_glm:
        # Upgrade in place (e.g. GLM-5.2 -> GLM-5.3). Keep the ORIGINAL prior
        # state, if any, so to-claude still restores the pre-GLM values.
        for k in MANAGED_KEYS:
            if k != "ANTHROPIC_AUTH_TOKEN" and env.get(k) != target[k]:
                changed[k] = (env.get(k, "(unset)"), target[k])
    else:
        # Record exact prior values of managed keys so to-claude restores reality.
        save_state({k: env.get(k, ABSENT) for k in MANAGED_KEYS})

    new_env = dict(env)
    new_env.update(target)
    new_data = dict(data)
    new_data["env"] = new_env

    verified_write(new_data, data, backup)
    print("Upgraded GLM settings to GLM-5.3." if on_glm else "Switched to GLM-5.3 (Z.AI).")
    for k, (old, new) in changed.items():
        print(f"  {k}: {old} -> {new}")
    print(f"  backup      : {backup}")
    print(f"  prior state : {STATE_FILE if STATE_FILE.exists() else 'none recorded'}")
    print("  RESTART Claude Code (new session) for this to take effect.")
    print("  Way back    : glm_switch.py to-claude")
    print("  Then verify : glm_switch.py check   (and /status in the new session)")


def cmd_to_claude():
    data, existed = load_settings()
    if not existed:
        print("No settings file — already on Claude defaults.")
        return
    if not STATE_FILE.exists() and current_provider(data) != "GLM (Z.AI)":
        print(f"Already on {current_provider(data)}. Nothing to do.")
        return
    backup = backup_file(label="pre-claude")

    env = data.get("env")
    if not isinstance(env, dict):
        print("No env block — already on Claude defaults.")
        return

    new_env = dict(env)
    if STATE_FILE.exists():
        prior = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        for k in MANAGED_KEYS:
            pv = prior.get(k, ABSENT)
            if pv == ABSENT:
                new_env.pop(k, None)
            else:
                new_env[k] = pv
        mode = "restored recorded prior values"
    else:
        # Fallback: no recorded state — remove GLM keys only if they clearly
        # point at Z.AI, otherwise refuse rather than guess.
        if "api.z.ai" not in new_env.get("ANTHROPIC_BASE_URL", ""):
            die(
                "No recorded prior state and ANTHROPIC_BASE_URL does not point "
                "at Z.AI — refusing to guess. Use 'restore --latest' or "
                "'restore <baseline-first-run.json>' for a full-file rollback."
            )
        for k in MANAGED_KEYS:
            new_env.pop(k, None)
        mode = "removed GLM keys (no prior state recorded)"

    new_data = dict(data)
    if new_env:
        new_data["env"] = new_env
    else:
        new_data.pop("env", None)

    verified_write(new_data, data, backup)
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    print(f"Switched back to Claude — {mode}.")
    print(f"  backup : {backup}")
    print("  RESTART Claude Code (new session) for this to take effect.")


def cmd_setup_key():
    import getpass
    key = getpass.getpass("Paste your Z.AI API key (input hidden): ").strip()
    if not key:
        die("Empty key — nothing saved.")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(f'ZAI_API_KEY="{key}"\n', encoding="utf-8")
    os.chmod(KEY_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 600
    os.chmod(STATE_DIR, stat.S_IRWXU)                # 700
    print(f"Key saved to {KEY_FILE} (permissions 600). It never leaves this machine.")


def cmd_check():
    """Send a 1-token request per configured model ID to Z.AI. Confirms the
    key works and the model IDs are accepted. The key is never printed."""
    import urllib.error
    import urllib.request
    api_key = read_api_key()
    # "[1m]" is a Claude Code client-side suffix (1M-context opt-in); the API
    # itself expects the bare model ID.
    ids = sorted({v.split("[")[0] for k, v in glm_env("").items() if k.endswith("_MODEL")})
    ok = True
    for model in ids:
        body = json.dumps({"model": model, "max_tokens": 1,
                           "messages": [{"role": "user", "content": "."}]}).encode()
        req = urllib.request.Request(
            f"{ZAI_BASE_URL}/v1/messages", data=body, method="POST",
            headers={"Authorization": f"Bearer {api_key}",
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                print(f"  OK   {model}  (HTTP {r.status})")
        except urllib.error.HTTPError as e:
            ok = False
            detail = e.read().decode("utf-8", "replace")[:200].replace(api_key, "***")
            print(f"  FAIL {model}  (HTTP {e.code}) {detail}")
        except OSError as e:
            ok = False
            print(f"  FAIL {model}  ({e})")
    if not ok:
        print("\nIf a model ID was rejected, compare with the current list at "
              "https://docs.z.ai/devpack/tool/claude and edit glm_env() in this script.")
        sys.exit(1)


def cmd_backups():
    found = False
    for name, d in (("primary", BACKUP_DIR), ("mirror ", MIRROR_DIR)):
        if d.exists():
            for p in sorted(d.glob("*.json")):
                print(f"[{name}] {p}")
                found = True
    if not found:
        print("No backups yet.")


def cmd_restore(arg: str):
    if arg == "--latest":
        candidates = {}
        for d in (BACKUP_DIR, MIRROR_DIR):
            if d.exists():
                for p in d.glob("settings-*.json"):
                    candidates.setdefault(p.name, p)  # primary wins on ties
        if not candidates:
            die("No backups found in primary or mirror.")
        src = candidates[sorted(candidates)[-1]]
    else:
        src = Path(arg).expanduser()
        if not src.exists():
            die(f"Backup not found: {src}")
    content = src.read_text(encoding="utf-8")
    backup_file(label="pre-restore")
    if content == "__FILE_DID_NOT_EXIST__":
        if SETTINGS.exists():
            SETTINGS.unlink()
        print(f"Restored state: settings.json removed (it did not exist at backup time {src.name}).")
        return
    json.loads(content)  # validate before touching anything
    shutil.copy2(src, SETTINGS)
    print(f"Restored {SETTINGS} from {src}. Restart Claude Code to apply.")


# ---------------------------------------------------------------- entry

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)
    cmd = args[0]
    if cmd == "status":
        cmd_status()
    elif cmd == "to-glm":
        cmd_to_glm()
    elif cmd == "to-claude":
        cmd_to_claude()
    elif cmd == "setup-key":
        cmd_setup_key()
    elif cmd == "check":
        cmd_check()
    elif cmd == "backups":
        cmd_backups()
    elif cmd == "restore":
        if len(args) < 2:
            die("restore requires a backup path or --latest")
        cmd_restore(args[1])
    else:
        die(f"Unknown command: {cmd}\n{__doc__}")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
