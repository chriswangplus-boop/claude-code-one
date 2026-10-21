import json
import subprocess
import os
import time
from pathlib import Path

import pytest

from relay import integrity
from relay.errors import SecretDetected
from relay.integrity import (
    changed_paths,
    git_commit,
    git_init,
    git_snapshot,
    owner_of,
    ownership_violations,
    prune_screens,
    secret_scan,
    snapshot,
    validate_sentinel,
)
from relay.util import sha256_file


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_owner_of():
    assert owner_of("src/app.py") == "coder"
    assert owner_of(".relay/HANDOFF.md") == "claude"
    assert owner_of(".relay/REVIEW.md") == "claude"
    assert owner_of(".relay/CODER_REPORT.md") == "coder"
    assert owner_of(".relay/STATE.json") == "harness"
    assert owner_of(".relay/MONITOR_NOTES.md") == "monitor"
    assert owner_of(".relay/something-new.md") == "harness"


def test_ownership_is_enforced(tmp_path):
    write(tmp_path, ".relay/HANDOFF.md", "v1")
    write(tmp_path, "src/app.py", "print(1)")
    before = snapshot(tmp_path)
    write(tmp_path, ".relay/HANDOFF.md", "v2")
    after = snapshot(tmp_path)
    assert changed_paths(before, after) == [".relay/HANDOFF.md"]
    violations = ownership_violations(before, after, actor="coder")
    assert violations == [{"path": ".relay/HANDOFF.md", "writer": "coder", "owner": "claude"}]
    assert ownership_violations(before, after, actor="claude") == []


def test_coder_may_write_source_and_report(tmp_path):
    before = snapshot(tmp_path)
    write(tmp_path, "src/main.py", "x = 1")
    write(tmp_path, ".relay/CODER_REPORT.md", "done")
    assert ownership_violations(before, snapshot(tmp_path), actor="coder") == []


def test_unknown_relay_file_is_owned_by_harness(tmp_path):
    before = snapshot(tmp_path)
    write(tmp_path, ".relay/SNEAKY.md", "hi")
    assert ownership_violations(before, snapshot(tmp_path), actor="claude")


def sentinel_for(root: Path, role: str, round_no: int, file_rel: str, **extra):
    target = root / file_rel
    data = {
        "round": round_no,
        "role": role,
        "status": "done",
        "verdict": "APPROVED" if role == "reviewer" else None,
        "file": file_rel,
        "sha256": sha256_file(target),
        "summary": "ok",
        "context_estimate_pct": 40,
        "question": None,
    }
    data.update(extra)
    return data


def test_valid_sentinel(tmp_path):
    write(tmp_path, ".relay/REVIEW.md", "review")
    sentinel = sentinel_for(tmp_path, "reviewer", 3, ".relay/REVIEW.md")
    assert validate_sentinel(
        sentinel, root=tmp_path, expected_round=3, expected_role="reviewer",
        expected_file=".relay/REVIEW.md", round_started_wall=time.time() - 1,
    ) == []


def test_stale_round_and_old_mtime(tmp_path):
    write(tmp_path, ".relay/REVIEW.md", "review")
    sentinel = sentinel_for(tmp_path, "reviewer", 2, ".relay/REVIEW.md")
    issues = validate_sentinel(
        sentinel, root=tmp_path, expected_round=3, expected_role="reviewer",
        expected_file=".relay/REVIEW.md", round_started_wall=time.time() - 1,
    )
    assert any("round" in issue for issue in issues)
    old = time.time() - 3600
    os.utime(tmp_path / ".relay/REVIEW.md", (old, old))
    issues = validate_sentinel(
        sentinel, root=tmp_path, expected_round=2, expected_role="reviewer",
        expected_file=".relay/REVIEW.md", round_started_wall=time.time() - 10,
    )
    assert any("predates" in issue for issue in issues)


def test_sha_mismatch_and_missing_file(tmp_path):
    write(tmp_path, ".relay/REVIEW.md", "review")
    sentinel = sentinel_for(tmp_path, "reviewer", 1, ".relay/REVIEW.md")
    write(tmp_path, ".relay/REVIEW.md", "changed after sentinel")
    issues = validate_sentinel(
        sentinel, root=tmp_path, expected_round=1, expected_role="reviewer",
        expected_file=".relay/REVIEW.md", round_started_wall=time.time() - 1,
    )
    assert any("sha256" in issue for issue in issues)
    sentinel["file"] = ".relay/NOPE.md"
    issues = validate_sentinel(
        sentinel, root=tmp_path, expected_round=1, expected_role="reviewer",
        expected_file=".relay/NOPE.md", round_started_wall=time.time() - 1,
    )
    assert any("does not exist" in issue for issue in issues)


def test_reviewer_needs_verdict_and_decision_needs_question(tmp_path):
    write(tmp_path, ".relay/REVIEW.md", "review")
    sentinel = sentinel_for(tmp_path, "reviewer", 1, ".relay/REVIEW.md", verdict=None)
    issues = validate_sentinel(
        sentinel, root=tmp_path, expected_round=1, expected_role="reviewer",
        expected_file=".relay/REVIEW.md", round_started_wall=time.time() - 1,
    )
    assert any("verdict" in issue for issue in issues)
    sentinel = sentinel_for(
        tmp_path, "coder", 1, ".relay/REVIEW.md",
        status="needs_decision", question="",
    )
    issues = validate_sentinel(
        sentinel, root=tmp_path, expected_round=1, expected_role="coder",
        expected_file=".relay/REVIEW.md", round_started_wall=time.time() - 1,
    )
    assert any("question" in issue for issue in issues)


def test_path_escape_is_rejected(tmp_path):
    write(tmp_path, ".relay/REVIEW.md", "review")
    sentinel = sentinel_for(tmp_path, "reviewer", 1, ".relay/REVIEW.md", file="../outside.md")
    issues = validate_sentinel(
        sentinel, root=tmp_path, expected_round=1, expected_role="reviewer",
        expected_file="../outside.md", round_started_wall=time.time() - 1,
    )
    assert any("escapes" in issue for issue in issues)


def test_secret_scan_finds_key_and_skips_ignored_dirs(tmp_path):
    write(tmp_path, "src/settings.py", 'API_KEY = "sk-abcdefghijklmnopqrstuvwx"\n')
    hits = secret_scan(tmp_path)
    assert any(hit["rule"] == "api_key" and hit["path"] == "src/settings.py" for hit in hits)
    write(tmp_path, ".relay/journal/calls/call-aaaaaaaaaaaa.json", '{"k": "sk-abcdefghijklmnopqrstuvwx"}')
    assert [h for h in secret_scan(tmp_path) if h["path"].startswith(".relay/journal")] == []
    with pytest.raises(SecretDetected, match="secret scan"):
        integrity.assert_no_secrets(tmp_path)


def test_secret_scan_ignores_placeholders_and_hashes(tmp_path):
    digest = "a" * 64
    write(tmp_path, "NOTES.md", f'sha256 {digest}\nAPI_KEY = "<your-key>"\npassword = "${{DB_PASSWORD}}"\n')
    assert secret_scan(tmp_path) == []


def test_git_commit_never_pushes_and_is_idempotent(tmp_path):
    git_init(tmp_path)
    write(tmp_path, ".gitignore", "ignored.txt\n")
    write(tmp_path, "a.txt", "one")
    commit = git_commit(tmp_path, "relay: round 1 test")
    assert commit and len(commit) == 40
    assert git_commit(tmp_path, "nothing") is None
    assert git_snapshot(tmp_path) == []
    assert not hasattr(integrity, "git_push")


def test_prune_screens(tmp_path):
    old = tmp_path / "old.png"
    new = tmp_path / "new.png"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    os.utime(old, (time.time() - 20 * 86400,) * 2)
    removed = prune_screens(tmp_path, retention_days=14)
    assert old in removed and new.exists()


def test_git_commit_refuses_staged_secret(tmp_path):
    from relay.errors import SecretDetected

    git_init(tmp_path)
    write(tmp_path, "leak.py", 'API_KEY = "sk-abcdefghijklmnopqrstuvwx"\n')
    with pytest.raises(SecretDetected, match="secret scan"):
        git_commit(tmp_path, "should not happen")
    log = subprocess.run(["git", "-C", str(tmp_path), "log", "--oneline"], capture_output=True, text=True)
    assert log.returncode != 0 or not log.stdout.strip()
