"""A deterministic offline pair of fake apps for `relay start --fake` and demos."""

from __future__ import annotations

from collections import deque
from pathlib import Path

from .gui import FakeAppDriver, write_sentinel


class DemoApps:
    def __init__(self, project_dir: str | Path, verdicts=("MILESTONE_DONE", "PROJECT_DONE")):
        self.project = Path(project_dir)
        self.verdicts = deque(verdicts)
        self.claude = FakeAppDriver("claude", self.project, on_ring=self._claude)
        self.coder = FakeAppDriver("coder", self.project, on_ring=self._coder)

    def drivers(self) -> dict:
        return {"claude": self.claude, "coder": self.coder}

    def _write(self, rel: str, text: str) -> None:
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def _claude(self, driver: FakeAppDriver, pointer: str) -> None:
        if pointer.startswith("RELAY seed") or pointer.startswith("RELAY nudge"):
            return
        role = _token(pointer, "role")
        round_no = _round(pointer)
        if pointer.startswith("RELAY checkpoint"):
            self._write(f".relay/CHECKPOINT_{role}.md", f"checkpoint {role} {round_no}\n")
            write_sentinel(self.project, role, round_no, f".relay/CHECKPOINT_{role}.md", context_pct=60)
            return
        if role in ("planner", "strategist"):
            if role == "strategist":
                self._write(".relay/STRATEGY.md", "# Strategy\n\nKeep the CLI small and test it.\n")
            self._write(".relay/PLAN.md", "# Plan\n\n1. todo CLI with add/list/done\n2. tests\n")
            self._write(".relay/HANDOFF.md", f"Implement milestone for round {round_no}. Run pytest.\n")
            write_sentinel(self.project, role, round_no, ".relay/HANDOFF.md", context_pct=30)
        elif role == "fixer":
            self._write(".relay/FIX_REPORT.md", f"# Fix report {round_no}\n\nRoot cause fixed, tests pass.\n")
            write_sentinel(self.project, "fixer", round_no, ".relay/FIX_REPORT.md", context_pct=35)
        elif role == "reviewer":
            verdict = self.verdicts.popleft() if self.verdicts else "PROJECT_DONE"
            self._write(".relay/REVIEW.md", f"Review round {round_no}: {verdict}\n")
            write_sentinel(self.project, "reviewer", round_no, ".relay/REVIEW.md", verdict=verdict, context_pct=44)

    def _coder(self, driver: FakeAppDriver, pointer: str) -> None:
        if pointer.startswith("RELAY seed") or pointer.startswith("RELAY nudge"):
            return
        round_no = _round(pointer)
        if pointer.startswith("RELAY checkpoint"):
            self._write(".relay/CHECKPOINT_coder.md", f"checkpoint coder {round_no}\n")
            write_sentinel(self.project, "coder", round_no, ".relay/CHECKPOINT_coder.md", context_pct=60)
            return
        self._write("src/todo.py", f"ITEMS = []\nROUND = {round_no}\n")
        self._write("tests/test_todo.py", "def test_smoke():\n    assert True\n")
        self._write(
            ".relay/CODER_REPORT.md",
            f"# Coder report {round_no}\n\n- wrote src/todo.py\n- ran pytest: 1 passed\n",
        )
        write_sentinel(self.project, "coder", round_no, ".relay/CODER_REPORT.md", context_pct=48)


def _token(pointer: str, key: str) -> str | None:
    for token in pointer.split():
        if token.startswith(f"{key}="):
            return token.split("=", 1)[1].strip(":")
    return None


def _round(pointer: str) -> int:
    parts = pointer.split()
    for index, token in enumerate(parts):
        if token == "round" and index + 1 < len(parts):
            return int(parts[index + 1].strip(":"))
    return 0
