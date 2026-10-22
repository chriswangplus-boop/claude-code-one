"""`relay` command line: init, calibrate, start/resume/pause/stop, decide, status, trace/why/replay."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .brain import Brain
from .config import (
    api_key, ensure_usable_project, ntfy_token, ntfy_topic,
    set_secret, sync_warning,
)
from .context import Context, open_project
from .decisions import accept_decision, confirm_human, record_instruction
from .errors import ConfigError, JournalError, RelayError, StateError
from .integrity import prune_screens
from .model_policy import opus_usage
from .monitor import Monitor
from .notify import Notifier
from .orchestrator import Orchestrator
from .setup import init_project
from .state import FileLock, atomic_write_json
from .tools import replay as replay_call
from .tools import trace as trace_events
from .tools import why as explain_event

RELAY_ROOT = Path(__file__).resolve().parent.parent


# --- shared helpers -------------------------------------------------------

def _ctx(args) -> Context:
    ctx = open_project(args.project)
    allow = bool(getattr(args, "allow_sync_dir", False)) or os.environ.get(
        "RELAY_ALLOW_SYNC_DIR", ""
    ).lower() in ("1", "true", "yes")
    reason = ensure_usable_project(ctx.cfg, allow_sync=allow)
    if reason:
        print(f"warning: {sync_warning(reason)}", file=sys.stderr)
    return ctx


def _initialized(ctx: Context) -> None:
    if not ctx.paths.state.exists():
        raise ConfigError("this project is not initialized; run `relay init` first")


def _secrets() -> list[str]:
    return [item for item in (api_key(), ntfy_topic(), ntfy_token()) if item]


def _notifier(ctx: Context, actor: str = "orchestrator") -> Notifier:
    return Notifier(ctx.cfg, ctx.journal, clock=ctx.clock, secrets=_secrets(), actor=actor)


def _brain(ctx: Context, *, model: str | None = None, actor: str = "orchestrator") -> Brain:
    key = api_key()
    if not key:
        raise ConfigError(
            "no DeepSeek API key. Set RELAY_DEEPSEEK_API_KEY or store one with "
            "`relay init --set-api-key`."
        )
    return Brain(
        ctx.cfg, ctx.journal, api_key=key, model=model, clock=ctx.clock,
        extra_secrets=_secrets(), actor=actor,
    )


def _monitor_alive(ctx: Context) -> bool:
    age = ctx.store.heartbeat_age(ctx.paths.monitor_heartbeat)
    return age is not None and age < 60


def _spawn_monitor(ctx: Context, *, allow_sync: bool) -> bool:
    if not bool(ctx.cfg.get("monitor.enabled", True)):
        return False
    if _monitor_alive(ctx):
        return True
    ctx.paths.monitor_dir.mkdir(parents=True, exist_ok=True)
    log_path = ctx.paths.monitor_dir / "monitor.log"
    log = open(log_path, "a", encoding="utf-8")
    try:
        log_path.chmod(0o600)
    except OSError:
        pass
    cmd = [sys.executable, "-m", "relay.cli", "monitor", "--project", str(ctx.cfg.project_dir)]
    if allow_sync:
        cmd.append("--allow-sync-dir")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(RELAY_ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        process = subprocess.Popen(
            cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=env,
            cwd=str(ctx.cfg.project_dir),
        )
    except OSError as exc:
        log.close()
        raise ConfigError(f"could not start the monitor: {exc}") from exc
    finally:
        if not log.closed:
            log.close()
    ctx.journal.append("monitor.spawned", detail={"pid": process.pid, "log": str(log_path)})
    print(f"monitor started (pid {process.pid}, log {log_path})")
    return True


def _refuse_unsafe_supervisors(ctx: Context, probe=None) -> None:
    """R3-7: the answer code must not be readable by the app Relay supervises."""
    from .guard import supervisor_risks

    risks = supervisor_risks(ctx.cfg, probe)
    if not risks:
        return
    try:
        ctx.journal.append(
            "security.refused", detail={"risks": risks, "kind": "supervisor_permissions"},
            result="blocked",
        )
    except JournalError:
        pass
    raise ConfigError(
        "a supervisor app holds screen or accessibility permission, so it could read the "
        "one-time answer code: " + ", ".join(risks) + ". Revoke Screen Recording and "
        "Accessibility for it in System Settings > Privacy & Security, turn off its computer "
        "use, then run `relay start` again."
    )


def _runtime(ctx: Context, args) -> tuple[dict, Brain | None, bool]:
    if bool(getattr(args, "fake", False)):
        from .demo import DemoApps

        return DemoApps(ctx.cfg.project_dir).drivers(), None, False

    if not bool(ctx.cfg.get("gui.enabled", True)):
        raise ConfigError("GUI control is disabled (gui.enabled=false)")
    for app in ("claude", "zcode"):
        if not ctx.paths.calibration(app).exists():
            raise ConfigError(
                f"{app} is not calibrated. Run `relay calibrate --app {app}` before starting."
            )
    from .gui.calibrate import shared_folder_status
    from .gui.screen import backend_for

    status = shared_folder_status(ctx.paths)
    if not all(status["proofs"].values()):
        raise ConfigError(
            "calibration self-test is incomplete: both .relay/CALIBRATION_claude.json and "
            ".relay/CALIBRATION_zcode.json are required. Run `relay calibrate` for each app."
        )
    if not status["same_folder"]:
        raise ConfigError(
            "the calibration proofs disagree about the working folder: "
            f"{status['proofs']}. Both apps must open the same project folder."
        )
    backend = backend_for(ctx.cfg, ctx.journal)
    permissions = backend.permissions()
    missing = [name for name, ok in permissions.items() if not ok]
    if missing:
        raise ConfigError(
            "macOS permissions missing: " + ", ".join(missing) + ". Open System Settings > "
            "Privacy & Security and grant Accessibility, Screen Recording, and Automation "
            "(System Events) to the app that launches Relay. Then QUIT and RELAUNCH that app: "
            "macOS caches Screen Recording at process launch, so the running process cannot pick "
            "up a newly granted permission. Then run `relay start` again."
        )
    brain = None
    vision = None
    if not bool(getattr(args, "no_brain", False)):
        brain = _brain(ctx)
        if bool(ctx.cfg.get("deepseek.vision", True)) and not bool(ctx.cfg.get("deepseek.text_only_mode", False)):
            vision = brain.vision
    # R3-7: a supervisor that can read the screen or the accessibility tree could read the
    # one-time answer code and answer its own question, so refuse to start.
    _refuse_unsafe_supervisors(ctx)
    from .apps import make_claude_driver, make_zcode_driver

    drivers = {
        "claude": make_claude_driver(ctx.cfg, ctx.paths, ctx.journal, backend, vision),
        "coder": make_zcode_driver(ctx.cfg, ctx.paths, ctx.journal, backend, vision),
    }
    monitor_required = False
    if bool(ctx.cfg.get("monitor.enabled", True)) and not bool(getattr(args, "no_monitor", False)):
        monitor_required = _spawn_monitor(ctx, allow_sync=bool(getattr(args, "allow_sync_dir", False)))
    return drivers, brain, monitor_required


def _print_status(ctx: Context) -> None:
    if not ctx.paths.state.exists():
        print("not initialized")
        return
    state = ctx.store.load()
    baton = ctx.store.baton()
    age = ctx.store.heartbeat_age(ctx.paths.monitor_heartbeat)
    pending = state.get("pending_decision") or {}
    print(f"Relay {__version__}  {ctx.cfg.project_dir}")
    print(f"phase: {state.get('phase')}   round: {state.get('round')}   milestone: {state.get('milestone')}")
    print(f"baton: {baton.get('holder')} (round {baton.get('round')})   role: {state.get('role')}")
    print(f"spend today: ${ctx.journal.spend_for_day():.4f} / ${float(ctx.cfg.get('daily_budget_usd', 0)):.2f}")
    print(f"trust: {ctx.trust.root}")
    print(f"monitor: {'alive' if age is not None and age < 60 else 'stale/missing'}"
          + (f" ({age:.0f}s)" if age is not None else ""))
    print(f"session rounds: {state.get('session_rounds')}")
    opus = opus_usage(state, ctx.clock.now().astimezone().date().isoformat(), ctx.cfg)
    print(f"Opus today: {opus['today']}/{opus['today_limit']}, "
          f"this milestone: {opus['milestone']}/{opus['milestone_limit']}")
    if state.get("paused_reason"):
        print(f"paused: {state.get('paused_reason')}")
    halt = ctx.store.read_flag("halt")
    if halt:
        print(f"HALT: {halt.get('reason')}")
    pause = ctx.store.read_flag("pause")
    if pause:
        print(f"pause requested: {pause.get('reason')}")
    if ctx.store.read_flag("stop"):
        print("stop requested")
    last_instruction = state.get("last_instruction") or {}
    if last_instruction:
        print(f"last instruction: {last_instruction.get('id')}  {last_instruction.get('text')}")
    if pending:
        print(f"pending decision: {pending.get('id')}  {pending.get('question')}")
        print('  the one-time answer code is in the desktop notification; answer with `relay decide <id> --code <code> "..."`')
    print("recent:")
    for event in ctx.journal.all_events()[-6:]:
        print(f"  {event.get('id')}  {event.get('type')}")


# --- commands -------------------------------------------------------------

def cmd_init(args) -> int:
    Path(args.project).expanduser().resolve().mkdir(parents=True, exist_ok=True)
    ctx = open_project(args.project)
    allow = args.allow_sync_dir or os.environ.get("RELAY_ALLOW_SYNC_DIR", "").lower() in ("1", "true", "yes")
    reason = ensure_usable_project(ctx.cfg, allow_sync=allow)
    if reason:
        print(f"warning: {sync_warning(reason)}", file=sys.stderr)
    result = init_project(ctx.cfg, ctx.paths, ctx.store, ctx.journal, goal=args.goal, force=args.force)

    if args.set_api_key:
        key = getpass.getpass("DeepSeek API key (hidden): ").strip()
        if key:
            try:
                set_secret("deepseek_api_key", key)
                print("API key stored in the OS keychain")
            except ConfigError as exc:
                print(f"could not store the key: {exc}")
    if args.ntfy_topic:
        set_secret("ntfy_topic", args.ntfy_topic)
    elif not args.no_ntfy and not ntfy_topic():
        topic = "relay-" + secrets.token_urlsafe(24).replace("-", "").replace("_", "")[:32]
        try:
            set_secret("ntfy_topic", topic)
            print(f"ntfy topic generated and stored in the keychain (…{topic[-4:]})")
        except ConfigError as exc:
            print(f"could not store the ntfy topic: {exc}")

    if result.get("already_initialized"):
        print("already initialized (use --force to rewrite rules)")
        if result.get("gitignore_updated"):
            print("  .gitignore updated")
        return 0
    print(f"initialized {ctx.cfg.project_dir}")
    print(f"  trust material (kept outside the project): {ctx.trust.root}")
    print(f"  created: {', '.join(result.get('created') or []) or 'nothing'}")
    if result.get("git_commit"):
        print(f"  baseline commit: {result['git_commit'][:12]}")
    print("next: relay calibrate --app claude && relay calibrate --app zcode")
    return 0


def cmd_calibrate(args) -> int:
    ctx = _ctx(args)
    _initialized(ctx)
    from .gui.calibrate import calibrate_app, shared_folder_status

    opener = None
    if sys.platform == "darwin":
        opener = lambda path: subprocess.run(["open", str(path)], check=False)  # noqa: E731
    result = calibrate_app(
        ctx, args.app, timeout_seconds=args.timeout, opener=opener, skip_self_test=args.skip_self_test
    )
    print(json.dumps(result, indent=2))
    status = shared_folder_status(ctx.paths)
    if all(status["proofs"].values()):
        print("shared folder: " + ("verified" if status["same_folder"] else "MISMATCH"))
    return 0


def cmd_start(args) -> int:
    ctx = _ctx(args)
    _initialized(ctx)
    _refuse_unsafe_supervisors(ctx)
    state = ctx.store.load()
    if state.get("phase") == "PAUSED":
        raise ConfigError("Relay is paused. Run `relay resume` to continue.")
    if args.detach:
        return _spawn_detached(ctx, args)
    return _run_loop(ctx, args, resume=False)


def cmd_resume(args) -> int:
    ctx = _ctx(args)
    _initialized(ctx)
    state = ctx.store.load()
    if state.get("phase") == "DONE":
        print("the project is already DONE")
        return 0
    if ctx.paths.halt_flag.exists() and not args.clear_halt:
        raise ConfigError(
            "the monitor left a HALT flag. Review .relay/MONITOR_NOTES.md, fix the cause, then "
            "run `relay resume --clear-halt`."
        )
    if args.clear_halt:
        ok, errors = ctx.journal.verify()
        if not ok:
            raise ConfigError(
                "journal integrity check failed, so the halt cannot be cleared: "
                + "; ".join(errors[:3])
            )
        previous = ctx.store.read_flag("halt") or {}
        ctx.store.clear_flag("halt")
        ctx.journal.append(
            "human.halt_cleared", actor="human",
            detail={"previous_reason": previous.get("reason")},
        )
    if getattr(args, "adopt_baseline", False):
        _adopt_baseline(ctx)
    return _run_loop(ctx, args, resume=True)


def _adopt_baseline(ctx: Context) -> None:
    """R3-4: the human explicitly accepts the current files as a pending turn's baseline."""
    import uuid

    from .integrity import git_control_snapshot, snapshot

    state = ctx.store.load()
    pending = state.get("pending")
    if not isinstance(pending, dict):
        print("no pending turn to adopt a baseline for")
        return
    attempt_id = str(pending.get("attempt_id") or uuid.uuid4().hex[:12])
    # Save the attempt id in STATE first: its own write must not land after the baseline.
    ctx.store.mutate(lambda current: current["pending"].__setitem__("attempt_id", attempt_id))
    ctx.trust.store_attempt(attempt_id, {
        "attempt_id": attempt_id,
        "phase": pending.get("phase"),
        "role": pending.get("role"),
        "round": pending.get("round"),
        "files": snapshot(ctx.cfg.project_dir),
        "git": git_control_snapshot(ctx.cfg.project_dir),
        "started_wall": pending.get("started_wall"),
        "kind": "adopted",
    })
    ctx.journal.append(
        "baseline.adopted", detail={"attempt_id": attempt_id, "role": pending.get("role")},
        result="warn",
    )
    print(f"adopted the current files as the baseline for attempt {attempt_id}")


def _spawn_detached(ctx: Context, args) -> int:
    log_path = ctx.paths.relay_dir / "relay.log"
    log = open(log_path, "a", encoding="utf-8")
    try:
        log_path.chmod(0o600)
    except OSError:
        pass
    cmd = [sys.executable, "-m", "relay.cli", "_run", "--project", str(ctx.cfg.project_dir)]
    if args.fake:
        cmd.append("--fake")
    if args.no_monitor:
        cmd.append("--no-monitor")
    if args.no_brain:
        cmd.append("--no-brain")
    if args.max_steps:
        cmd += ["--max-steps", str(args.max_steps)]
    if args.allow_sync_dir:
        cmd.append("--allow-sync-dir")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(RELAY_ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        process = subprocess.Popen(
            cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=env,
            cwd=str(ctx.cfg.project_dir),
        )
    except OSError as exc:
        log.close()
        raise ConfigError(f"could not start the monitor: {exc}") from exc
    finally:
        if not log.closed:
            log.close()
    atomic_write_json(ctx.paths.run_pid, {"pid": process.pid})
    time.sleep(0.3)
    code = process.poll()
    if code not in (None, 0):
        raise ConfigError(f"the detached relay exited immediately (code {code}); see {log_path}")
    print(f"relay started detached (pid {process.pid}); log: {log_path}")
    return 0


def _run_loop(ctx: Context, args, *, resume: bool) -> int:
    lock = FileLock(ctx.paths.run_lock, timeout=0.8)
    try:
        lock.acquire()
    except StateError as exc:
        raise ConfigError("another Relay loop is already running for this project") from exc
    try:
        atomic_write_json(ctx.paths.run_pid, {"pid": os.getpid()})
        state = ctx.store.load()
        ctx.store.heartbeat(phase=state.get("phase"), round=state.get("round", 0))
        prune_screens(ctx.paths.screens_dir, int(ctx.cfg.get("screenshot_retention_days", 14)), ctx.clock.epoch())
        drivers, brain, monitor_required = _runtime(ctx, args)
        if brain is None:
            print("warning: routing is deterministic (no DeepSeek brain)")
        orchestrator = Orchestrator(
            ctx, drivers, brain=brain, notifier=_notifier(ctx), monitor_required=monitor_required,
        )
        wait_human = not bool(getattr(args, "no_wait_human", False))
        if resume:
            state = orchestrator.resume(max_steps=args.max_steps, wait_human=wait_human)
        else:
            state = orchestrator.run(max_steps=args.max_steps, wait_human=wait_human)
        print(f"phase: {state.get('phase')}  round: {state.get('round')}")
        if state.get("pending_decision"):
            print(f"waiting on {state['pending_decision'].get('id')}: {state['pending_decision'].get('question')}")
        if state.get("paused_reason"):
            print(f"paused: {state.get('paused_reason')}")
        return 0
    except RelayError:
        raise
    except Exception as exc:
        ctx.journal.append("error", detail={"error": f"{type(exc).__name__}: {exc}"}, result="error")
        print(f"relay: unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            ctx.paths.run_pid.unlink()
        except FileNotFoundError:
            pass
        lock.release()


def cmd_pause(args) -> int:
    ctx = _ctx(args)
    _initialized(ctx)
    ctx.store.set_flag("pause", {"reason": args.reason, "by": "human"})
    ctx.journal.append("human.pause", actor="human", detail={"reason": args.reason})
    print("pause requested; the loop stops at its next safe point")
    return 0


def cmd_stop(args) -> int:
    ctx = _ctx(args)
    _initialized(ctx)
    ctx.store.set_flag("stop", {"reason": args.reason, "by": "human"})
    ctx.journal.append("human.stop", actor="human", detail={"reason": args.reason})
    print("stop requested; the loop and monitor exit at their next check")
    return 0


def cmd_status(args) -> int:
    ctx = _ctx(args)
    if args.json:
        if not ctx.paths.state.exists():
            print(json.dumps({"initialized": False}))
            return 0
        state = ctx.store.load()
        print(json.dumps({
            "initialized": True,
            "phase": state.get("phase"),
            "round": state.get("round"),
            "milestone": state.get("milestone"),
            "baton": ctx.store.baton(),
            "spent_today_usd": ctx.journal.spend_for_day(),
            "trust_dir": str(ctx.trust.root),
            "opus": opus_usage(state, ctx.clock.now().astimezone().date().isoformat(), ctx.cfg),
            "monitor_heartbeat_age": ctx.store.heartbeat_age(ctx.paths.monitor_heartbeat),
            "pending_decision": state.get("pending_decision"),
            "paused_reason": state.get("paused_reason"),
        }, indent=2, default=str))
        return 0
    _print_status(ctx)
    return 0


def cmd_instruct(args) -> int:
    ctx = _ctx(args)
    _initialized(ctx)
    state = ctx.store.load()
    text = " ".join(args.text)
    required = bool(ctx.cfg.get("security.require_instruction_confirmation", True))
    if required and not confirm_human(f"Record this Relay instruction?\n\n{text[:300]}"):
        raise ConfigError("instruction was not confirmed")
    result = record_instruction(ctx, text, confirmed=True, opus=bool(getattr(args, "opus", False)))
    pending = state.get("pending_decision")
    print(f"{result['id']} recorded as a human instruction; both agents see it in DECISIONS.md on their next turn")
    if not required:
        print("warning: instruction confirmation is disabled in config; any process that can run relay can record an instruction")
    if result.get("opus"):
        print("also armed one Opus turn (it counts toward the model_policy limits)")
    if pending:
        print(f"note: decision {pending.get('id')} is still pending; answer it with `relay decide {pending.get('id')} \"...\"`")
    return 0


def cmd_decide(args) -> int:
    ctx = _ctx(args)
    _initialized(ctx)
    state = ctx.store.load()
    pending = state.get("pending_decision")
    if not isinstance(pending, dict):
        raise ConfigError("there is no pending decision")
    if args.decision_id.upper() != str(pending.get("id")).upper():
        raise ConfigError(f"pending decision is {pending.get('id')}, not {args.decision_id}")
    answer = " ".join(args.answer).strip()
    result = accept_decision(ctx, answer, args.code)
    print(f"{result['id']} answered; Relay will continue at {result['phase']}")
    print("if the loop is not running, start it with `relay resume`")
    return 0


def cmd_journal_repair(args) -> int:
    ctx = _ctx(args)
    _initialized(ctx)
    result = ctx.journal.repair()
    print(json.dumps(result, indent=2, default=str))
    ok, errors = ctx.journal.verify()
    print(f"journal chain: {'OK' if ok else 'BROKEN'}")
    for error in errors[:10]:
        print(f"  ! {error}")
    return 0 if ok else 1


def cmd_trace(args) -> int:
    ctx = _ctx(args)
    print(trace_events(
        ctx, round_no=args.round, verify=args.verify, limit=args.limit, event_type=args.type,
    ))
    if args.verify:
        ok, _ = ctx.journal.verify()
        return 0 if ok else 1
    return 0


def cmd_why(args) -> int:
    ctx = _ctx(args)
    print(explain_event(ctx, args.event_id))
    return 0


def cmd_replay(args) -> int:
    ctx = _ctx(args)
    result = replay_call(ctx, args.call_id)
    print(json.dumps(result, indent=2)[:6000])
    print("same response" if result["same"] else "response changed")
    return 0


def cmd_monitor(args) -> int:
    ctx = _ctx(args)
    _initialized(ctx)
    if args.interval_minutes is not None:
        ctx.cfg.set("monitor.interval_minutes", args.interval_minutes)
    if args.no_layer2:
        ctx.cfg.set("monitor.layer2", False)
    lock = FileLock(ctx.paths.monitor_lock, timeout=0.5)
    try:
        lock.acquire()
    except StateError as exc:
        raise ConfigError("a monitor is already running for this project") from exc
    try:
        brain = None
        key = api_key()
        if key and bool(ctx.cfg.get("monitor.layer2", True)):
            model = ctx.cfg.get("monitor.model")
            brain = _brain(ctx, model=model, actor="monitor")
        monitor = Monitor(ctx, brain=brain, notifier=_notifier(ctx, actor="monitor"))
        if args.once:
            print(json.dumps(monitor.check_once(), indent=2, default=str))
            return 0
        print("monitor running; Ctrl-C to stop (or `relay stop`)")
        monitor.run_forever(poll_seconds=args.poll_seconds)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        lock.release()


def cmd_host_guard(args) -> int:
    ctx = _ctx(args)
    _initialized(ctx)
    pct = args.pct
    turns = args.turns
    if pct is None:
        raw = os.environ.get("RELAY_HOST_CONTEXT_PCT")
        pct = float(raw) if raw else None
    if turns is None:
        raw = os.environ.get("RELAY_HOST_TURNS")
        turns = int(raw) if raw else None
    if pct is None and turns is None:
        print(
            "the host app exposes no usage reading; pass --pct or --turns, or set "
            "RELAY_HOST_CONTEXT_PCT / RELAY_HOST_TURNS"
        )
        return 0
    guard = float(ctx.cfg.get("host.context_guard", 0.7)) * 100
    max_turns = int(ctx.cfg.get("host.max_turns", 40))
    tripped = (pct is not None and pct >= guard) or (turns is not None and turns >= max_turns)
    if tripped:
        reason = f"host DeepSeek session at {pct}% / {turns} turns"
        ctx.store.set_flag("pause", {"reason": reason, "by": "host-guard"})
        ctx.journal.append(
            "guard.tripped", detail={"kind": "host_context", "pct": pct, "turns": turns, "limit": guard},
            result="blocked",
        )
        _notifier(ctx).send(
            "Relay paused: host context guard",
            f"{reason}. Start a new host session, then run `relay resume`.",
            urgent=True,
        )
        print(f"paused: {reason}")
        return 0
    print(f"host context ok ({pct if pct is not None else '?'}% of {guard:.0f}%)")
    return 0


def cmd_version(args) -> int:
    print(__version__)
    return 0


# --- parser ---------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default=".", help="project folder (default: current directory)")
    common.add_argument("--allow-sync-dir", action="store_true", help="allow a cloud-synced project folder")

    parser = argparse.ArgumentParser(prog="relay", description="GUI-level relay driven by a stateless DeepSeek brain")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(
        dest="command", required=True,
        metavar="{init,calibrate,start,resume,pause,stop,status,instruct,decide,trace,why,replay,monitor,host-guard,version}",
    )

    init = sub.add_parser("init", parents=[common], help="create .relay/, rules, and the git baseline")
    init.add_argument("--goal", default="", help="project goal written to .relay/GOAL.md")
    init.add_argument("--force", action="store_true", help="rewrite rules and gitignore")
    init.add_argument("--set-api-key", action="store_true", help="prompt for the DeepSeek API key")
    init.add_argument("--ntfy-topic", default=None)
    init.add_argument("--no-ntfy", action="store_true")
    init.set_defaults(func=cmd_init)

    calibrate = sub.add_parser("calibrate", parents=[common], help="interactively calibrate one app")
    calibrate.add_argument("--app", required=True, choices=["claude", "zcode"])
    calibrate.add_argument("--timeout", type=int, default=240)
    calibrate.add_argument("--skip-self-test", action="store_true")
    calibrate.set_defaults(func=cmd_calibrate)

    start = sub.add_parser("start", parents=[common], help="start the relay loop")
    start.add_argument("--detach", action="store_true", help="run in the background")
    start.add_argument("--fake", action="store_true", help="use deterministic fake apps (offline demo)")
    start.add_argument("--no-monitor", action="store_true", help="do not start the independent monitor")
    start.add_argument("--no-brain", action="store_true", help="deterministic routing only (no DeepSeek)")
    start.add_argument("--max-steps", type=int, default=None, help="stop after N orchestrator steps")
    start.add_argument("--no-wait-human", action="store_true", help="exit at a human decision instead of blocking")
    start.set_defaults(func=cmd_start)

    run = sub.add_parser("_run", parents=[common], help=argparse.SUPPRESS)
    run.add_argument("--fake", action="store_true")
    run.add_argument("--no-monitor", action="store_true")
    run.add_argument("--no-brain", action="store_true")
    run.add_argument("--max-steps", type=int, default=None)
    run.add_argument("--no-wait-human", action="store_true")
    run.set_defaults(func=cmd_start, detach=False)

    resume = sub.add_parser("resume", parents=[common], help="resume a paused or waiting loop")
    resume.add_argument("--clear-halt", action="store_true", help="clear a monitor HALT after review")
    resume.add_argument(
        "--adopt-baseline", action="store_true",
        help="adopt the current files as the signed baseline of a pending turn that predates R3-4",
    )
    resume.add_argument("--fake", action="store_true")
    resume.add_argument("--no-monitor", action="store_true")
    resume.add_argument("--no-brain", action="store_true")
    resume.add_argument("--max-steps", type=int, default=None)
    resume.add_argument("--no-wait-human", action="store_true")
    resume.set_defaults(func=cmd_resume)

    pause = sub.add_parser("pause", parents=[common], help="pause at the next safe point")
    pause.add_argument("--reason", default="paused by user")
    pause.set_defaults(func=cmd_pause)

    stop = sub.add_parser("stop", parents=[common], help="stop the loop and monitor")
    stop.add_argument("--reason", default="stopped by user")
    stop.set_defaults(func=cmd_stop)

    status = sub.add_parser("status", parents=[common], help="show the current state")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    instruct = sub.add_parser("instruct", parents=[common], help="record a new human instruction for both agents")
    instruct.add_argument("text", nargs="+")
    instruct.add_argument("--opus", action="store_true", help="also run one Opus turn, by rule B2")
    instruct.set_defaults(func=cmd_instruct)

    decide = sub.add_parser("decide", parents=[common], help="answer a pending decision locally")
    decide.add_argument("decision_id")
    decide.add_argument("--code", required=True, help="one-time code from the desktop notification")
    decide.add_argument("answer", nargs="+")
    decide.set_defaults(func=cmd_decide)

    journal = sub.add_parser("journal", parents=[common], help="journal maintenance")
    journal_sub = journal.add_subparsers(dest="journal_command", required=True)
    journal_repair = journal_sub.add_parser("repair", help="quarantine a corrupt tail and repair the chain")
    journal_repair.set_defaults(func=cmd_journal_repair)

    trace = sub.add_parser("trace", parents=[common], help="show the journal timeline")
    trace.add_argument("--round", type=int, default=None)
    trace.add_argument("--verify", action="store_true", help="verify the hash chain")
    trace.add_argument("--limit", type=int, default=300)
    trace.add_argument("--type", default=None)
    trace.set_defaults(func=cmd_trace)

    why = sub.add_parser("why", parents=[common], help="explain one event and the judgment behind it")
    why.add_argument("event_id")
    why.set_defaults(func=cmd_why)

    replay = sub.add_parser("replay", parents=[common], help="re-run one DeepSeek judgment")
    replay.add_argument("call_id")
    replay.set_defaults(func=cmd_replay)

    monitor = sub.add_parser("monitor", parents=[common], help="run the independent monitor")
    monitor.add_argument("--once", action="store_true")
    monitor.add_argument("--interval-minutes", type=float, default=None)
    monitor.add_argument("--no-layer2", action="store_true")
    monitor.add_argument("--poll-seconds", type=float, default=10.0)
    monitor.set_defaults(func=cmd_monitor)

    host = sub.add_parser("host-guard", parents=[common], help="pause if the hosting session is near its context limit")
    host.add_argument("--pct", type=float, default=None)
    host.add_argument("--turns", type=int, default=None)
    host.set_defaults(func=cmd_host_guard)

    version = sub.add_parser("version", parents=[common], help="print the version")
    version.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            action._choices_actions = [
                choice for choice in action._choices_actions if choice.dest != "_run"
            ]
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except RelayError as exc:
        print(f"relay: {exc}", file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"relay: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
