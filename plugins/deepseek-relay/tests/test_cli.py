import json
from pathlib import Path

from relay.cli import main
from relay.context import open_project
from relay.decisions import write_pending


def run(*args) -> int:
    return main([str(arg) for arg in args])


def test_init_start_status_trace_why(tmp_path, capsys):
    project = tmp_path / "proj"
    assert run("init", "--project", project, "--goal", "tiny todo", "--no-ntfy") == 0
    assert (project / ".relay" / "RULES_CLAUDE.md").exists()
    assert (project / ".git").exists()

    assert run("start", "--project", project, "--fake", "--max-steps", "20") == 0
    out = capsys.readouterr().out
    assert "phase: DONE" in out

    assert run("status", "--project", project, "--json") == 0
    status = json.loads(capsys.readouterr().out)
    assert status["phase"] == "DONE"
    assert status["round"] == 6
    assert status["milestone"] == 2
    assert status["baton"]["holder"] == "none"

    assert run("trace", "--project", project, "--verify") == 0
    trace = capsys.readouterr().out
    assert "journal chain: OK" in trace
    assert "route.decided" in trace

    ctx = open_project(project)
    init_event = ctx.journal.of_type("init")[0]
    assert run("why", "--project", project, init_event["id"]) == 0
    why = capsys.readouterr().out
    assert f"EVENT {init_event['id']}" in why
    assert "NEARBY" in why


def test_init_is_idempotent(tmp_path, capsys):
    project = tmp_path / "proj"
    assert run("init", "--project", project, "--no-ntfy") == 0
    assert run("init", "--project", project, "--no-ntfy") == 0
    assert "already initialized" in capsys.readouterr().out


def test_decide_answers_locally(tmp_path, capsys):
    project = tmp_path / "proj"
    assert run("init", "--project", project, "--no-ntfy") == 0
    ctx = open_project(project)
    state = ctx.store.load()
    state["phase"] = "WAIT_HUMAN"
    state["return_to"] = "REVIEW"
    state["round"] = 2
    state["pending_decision"] = {
        "id": "D-001", "question": "JSON or SQLite?", "return_to": "REVIEW", "round": 2,
        "options": [],
    }
    ctx.store.save(state)
    write_pending(
        ctx.paths, decision_id="D-001", question="JSON or SQLite?",
        context_lines=["storage choice"], options=[{"label": "JSON", "consequence": "simple"}],
        suggestion=None, round_no=2, reason="needs a decision", clock=ctx.clock,
    )
    assert run("decide", "--project", project, "D-001", "JSON,", "keep", "it", "simple") == 0
    assert "D-001 answered" in capsys.readouterr().out
    assert "JSON, keep it simple" in ctx.paths.decisions.read_text()
    assert ctx.store.load()["phase"] == "REVIEW"

    # wrong id is a clean error
    assert run("decide", "--project", project, "D-009", "no") == 2


def test_pause_stop_and_host_guard(tmp_path):
    project = tmp_path / "proj"
    assert run("init", "--project", project, "--no-ntfy") == 0
    ctx = open_project(project)
    assert run("pause", "--project", project, "--reason", "break") == 0
    assert ctx.store.read_flag("pause")["reason"] == "break"
    assert run("stop", "--project", project) == 0
    assert ctx.paths.stop_flag.exists()
    ctx.store.clear_flag("pause")
    ctx.store.clear_flag("stop")
    assert run("host-guard", "--project", project, "--pct", "90") == 0
    flag = ctx.store.read_flag("pause")
    assert flag and "host" in flag["reason"]


def test_monitor_once_via_cli(tmp_path, capsys):
    project = tmp_path / "proj"
    assert run("init", "--project", project, "--no-ntfy") == 0
    capsys.readouterr()
    ctx = open_project(project)
    ctx.store.heartbeat()
    state = ctx.store.load()
    state["phase"] = "CODE"
    state["round"] = 2
    state["pending"] = {"phase": "CODE", "role": "coder", "round": 2}
    ctx.store.save(state)
    ctx.store.set_baton("coder", "coder", 2)
    ctx.journal.append("turn.start", state="CODE", round_no=2, detail={"role": "coder"})
    assert run("monitor", "--project", project, "--once", "--no-layer2") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["halted"] is False
    assert ctx.paths.monitor_heartbeat.exists()


def test_resume_after_max_steps(tmp_path, capsys):
    project = tmp_path / "proj"
    assert run("init", "--project", project, "--no-ntfy") == 0
    capsys.readouterr()
    assert run("start", "--project", project, "--fake", "--max-steps", "1") == 0
    capsys.readouterr()
    assert run("status", "--project", project, "--json") == 0
    assert json.loads(capsys.readouterr().out)["phase"] == "CODE"
    assert run("resume", "--project", project, "--fake", "--max-steps", "20") == 0
    capsys.readouterr()
    assert run("status", "--project", project, "--json") == 0
    assert json.loads(capsys.readouterr().out)["phase"] == "DONE"


def test_trace_verify_detects_tampering(tmp_path, capsys):
    project = tmp_path / "proj"
    assert run("init", "--project", project, "--no-ntfy") == 0
    ctx = open_project(project)
    journal = sorted(ctx.paths.journal_dir.glob("*.jsonl"))[0]
    lines = journal.read_text().splitlines()
    lines[-1] = lines[-1].replace('"init"', '"innit"')
    journal.write_text("\n".join(lines) + "\n")
    assert run("trace", "--project", project, "--verify") == 1
    assert "BROKEN" in capsys.readouterr().out


def test_google_drive_project_runs_without_override(tmp_path, capsys):
    project = tmp_path / "My Drive" / "relay-proj"
    assert run("init", "--project", project, "--goal", "drive", "--no-ntfy") == 0
    err = capsys.readouterr().err
    assert "Google Drive is allowed" in err
    assert run("start", "--project", project, "--fake", "--max-steps", "20") == 0
    capsys.readouterr()
    assert run("status", "--project", project, "--json") == 0
    assert json.loads(capsys.readouterr().out)["phase"] == "DONE"


def test_instruct_records_human_instruction(tmp_path, capsys):
    project = tmp_path / "proj"
    assert run("init", "--project", project, "--no-ntfy") == 0
    capsys.readouterr()
    assert run("instruct", "--project", project, "Prefer", "JSON", "and", "keep", "dependencies", "minimal") == 0
    out = capsys.readouterr().out
    assert "D-001 recorded" in out
    ctx = open_project(project)
    decisions = ctx.paths.decisions.read_text()
    assert "Host instruction" in decisions
    assert "Prefer JSON" in decisions
    assert "decision.answered" in decisions
    event = ctx.journal.latest("decision.answered")
    assert event["actor"] == "human"
    assert event["detail"]["source"] == "host-instruction"
    assert ctx.store.load()["decisions"] == 1
