"""The single definition of where Relay keeps everything inside the project folder."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROLES = ("planner", "reviewer", "coder")


@dataclass(frozen=True)
class Paths:
    project_dir: Path

    @classmethod
    def for_project(cls, project_dir: str | Path) -> "Paths":
        return cls(Path(project_dir).expanduser().resolve())

    # --- relay root -------------------------------------------------------
    @property
    def relay_dir(self) -> Path:
        return self.project_dir / ".relay"

    @property
    def lock(self) -> Path:
        return self.relay_dir / ".lock"

    @property
    def state(self) -> Path:
        return self.relay_dir / "STATE.json"

    @property
    def baton(self) -> Path:
        return self.relay_dir / "BATON.json"

    @property
    def config(self) -> Path:
        return self.relay_dir / "config.json"

    @property
    def goal(self) -> Path:
        return self.relay_dir / "GOAL.md"

    @property
    def plan(self) -> Path:
        return self.relay_dir / "PLAN.md"

    @property
    def handoff(self) -> Path:
        return self.relay_dir / "HANDOFF.md"

    @property
    def review(self) -> Path:
        return self.relay_dir / "REVIEW.md"

    @property
    def coder_report(self) -> Path:
        return self.relay_dir / "CODER_REPORT.md"

    @property
    def decisions(self) -> Path:
        return self.relay_dir / "DECISIONS.md"

    @property
    def pending_decision(self) -> Path:
        return self.relay_dir / "PENDING_DECISION.md"

    @property
    def monitor_notes(self) -> Path:
        return self.relay_dir / "MONITOR_NOTES.md"

    @property
    def rules_claude(self) -> Path:
        return self.relay_dir / "RULES_CLAUDE.md"

    @property
    def rules_coder(self) -> Path:
        return self.relay_dir / "RULES_CODER.md"

    @property
    def heartbeat(self) -> Path:
        return self.relay_dir / "HEARTBEAT.json"

    @property
    def pause_flag(self) -> Path:
        return self.relay_dir / "PAUSE"

    @property
    def stop_flag(self) -> Path:
        return self.relay_dir / "STOP"

    @property
    def halt_flag(self) -> Path:
        return self.relay_dir / "HALT"

    @property
    def run_pid(self) -> Path:
        return self.relay_dir / ".run.pid"

    @property
    def run_lock(self) -> Path:
        return self.relay_dir / ".run.lock"

    @property
    def monitor_lock(self) -> Path:
        return self.monitor_dir / ".monitor.lock"

    def checkpoint(self, role: str) -> Path:
        return self.relay_dir / f"CHECKPOINT_{role}.md"

    def sentinel(self, role: str) -> Path:
        return self.relay_dir / ("CODER_DONE.json" if role == "coder" else "CLAUDE_DONE.json")

    def rule_for(self, role: str) -> Path:
        return self.rules_coder if role == "coder" else self.rules_claude

    # --- journal ----------------------------------------------------------
    @property
    def journal_dir(self) -> Path:
        return self.relay_dir / "journal"

    @property
    def calls_dir(self) -> Path:
        return self.journal_dir / "calls"

    @property
    def screens_dir(self) -> Path:
        return self.journal_dir / "screens"

    # --- monitor ----------------------------------------------------------
    @property
    def monitor_dir(self) -> Path:
        return self.relay_dir / "monitor"

    @property
    def monitor_heartbeat(self) -> Path:
        return self.monitor_dir / "HEARTBEAT.json"

    @property
    def correction(self) -> Path:
        return self.monitor_dir / "CORRECTION.json"

    @property
    def monitor_events(self) -> Path:
        return self.monitor_dir / "events.jsonl"

    # --- other ------------------------------------------------------------
    @property
    def calibration_dir(self) -> Path:
        return self.relay_dir / "calibration"

    @property
    def answers_dir(self) -> Path:
        return self.relay_dir / "answers"

    def calibration(self, app: str) -> Path:
        return self.calibration_dir / f"{app}.json"

    def calibration_proof(self, app: str) -> Path:
        return self.relay_dir / f"CALIBRATION_{app}.json"

    def all_working_dirs(self) -> list[Path]:
        """Directories Relay may create."""
        return [
            self.relay_dir, self.journal_dir, self.calls_dir, self.screens_dir,
            self.monitor_dir, self.calibration_dir, self.answers_dir,
        ]
