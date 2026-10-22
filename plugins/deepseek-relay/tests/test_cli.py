import json

from relay.cli import main
from relay.context import open_project
from relay.decisions import hash_code, new_code, write_pending
from relay.util import sha256_text


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
    code = new_code()
    state["pending_decision"] = {
        "id": "D-001", "question": "JSON or SQLite?", "return_to": "REVIEW", "round": 2,
        "options": [],
    }
    ctx.store.save(state)
    ctx.trust.store_decision({
        "id": "D-001", "question_sha256": sha256_text("JSON or SQLite?"),
        "options": [{"label": "JSON", "consequence": "simple"}], "kind": None,
        "data": {}, "return_to": "REVIEW", "round": 2, "code_hash": hash_code(code),
        "asked_at": "2026-10-21T00:00:00+00:00",
    })
    write_pending(
        ctx.paths, decision_id="D-001", question="JSON or SQLite?",
        context_lines=["storage choice"], options=[{"label": "JSON", "consequence": "simple"}],
        suggestion=None, round_no=2, reason="needs a decision", clock=ctx.clock,
    )
    assert run("decide", "--project", project, "D-001", "--code", code, "JSON,", "keep", "it", "simple") == 0
    assert "D-001 answered" in capsys.readouterr().out
    assert "JSON, keep it simple" in ctx.paths.decisions.read_text()
    assert ctx.store.load()["phase"] == "REVIEW"

    # wrong id / wrong code are clean errors
    assert run("decide", "--project", project, "D-009", "--code", code, "no") == 2
    assert run("decide", "--project", project, "D-001", "--code", "wrongcode", "no") == 2


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


def test_instruct_records_human_instruction(tmp_path, capsys, monkeypatch):
    project = tmp_path / "proj"
    assert run("init", "--project", project, "--no-ntfy") == 0
    capsys.readouterr()
    monkeypatch.setattr("relay.cli.confirm_human", lambda prompt: True)
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


def test_instruct_requires_human_confirmation(tmp_path, monkeypatch, capsys):
    project = tmp_path / "proj"
    assert run("init", "--project", project, "--no-ntfy") == 0
    capsys.readouterr()
    monkeypatch.setattr("relay.cli.confirm_human", lambda prompt: False)
    assert run("instruct", "--project", project, "Do", "not", "record") == 2
    assert not open_project(project).paths.decisions.read_text().count("Host instruction")
    monkeypatch.setattr("relay.cli.confirm_human", lambda prompt: True)
    assert run("instruct", "--project", project, "Record", "this") == 0
    assert "Record this" in open_project(project).paths.decisions.read_text()


def test_instruction_confirmation_can_be_disabled_explicitly(tmp_path, capsys):
    project = tmp_path / "proj"
    assert run("init", "--project", project, "--no-ntfy") == 0
    (project / ".relay" / "config.json").write_text(
        json.dumps({"security": {"require_instruction_confirmation": False}})
    )
    capsys.readouterr()
    assert run("instruct", "--project", project, "Auto", "test", "instruction") == 0
    out = capsys.readouterr().out
    assert "confirmation is disabled" in out
    assert "Auto test instruction" in open_project(project).paths.decisions.read_text()


def test_resume_clear_halt_refuses_a_tampered_journal(tmp_path, capsys):
    project = tmp_path / "proj"
    assert run("init", "--project", project, "--no-ntfy") == 0
    ctx = open_project(project)
    ctx.store.set_flag("halt", {"reason": "test"})
    ctx.journal.append("monitor.halt", round_no=1, detail={"reason": "test"})

    journal_file = sorted(ctx.paths.journal_dir.glob("*.jsonl"))[-1]
    lines = journal_file.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[-1])
    event["detail"]["reason"] = "rewritten"
    lines[-1] = json.dumps(event, sort_keys=True)
    journal_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    capsys.readouterr()

    assert run("resume", "--project", project, "--clear-halt") == 2
    assert ctx.paths.halt_flag.exists(), "the halt must not be cleared from a tampered journal"
    assert "journal integrity check failed" in capsys.readouterr().err


def test_start_refuses_a_supervisor_with_accessibility(tmp_path, capsys, monkeypatch):
    project = tmp_path / "proj"
    assert run("init", "--project", project, "--no-ntfy") == 0
    config = project / ".relay/config.json"
    config.write_text(json.dumps({"security": {"supervisor_bundle_ids": ["com.example.claude"]}}))
    monkeypatch.setattr(
        "relay.guard.system_tcc_grants",
        lambda: {"com.example.claude": {"kTCCServiceAccessibility"}},
    )
    capsys.readouterr()

    assert run("start", "--project", project, "--fake", "--max-steps", "1") == 2
    err = capsys.readouterr().err
    assert "could read the one-time answer code" in err
