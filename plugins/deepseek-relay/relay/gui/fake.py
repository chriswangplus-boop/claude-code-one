"""In-memory driver used by the offline end-to-end tests and `relay start --fake`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from ..errors import AppMismatch
from ..util import sha256_file
from .base import AppDriver, Popup


def write_sentinel(
    project_dir: Path,
    role: str,
    round_no: int,
    file_rel: str,
    *,
    status: str = "done",
    verdict: str | None = None,
    summary: str = "fake sentinel",
    context_pct: int = 40,
    question: str | None = None,
    options: list[dict] | None = None,
    blocking_issue_ids: list[str] | None = None,
) -> Path:
    target = project_dir / file_rel
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"fake output for {role} round {round_no}\n")
    payload = {
        "round": round_no,
        "role": role,
        "status": status,
        "verdict": verdict,
        "file": file_rel,
        "sha256": sha256_file(target),
        "summary": summary,
        "context_estimate_pct": context_pct,
        "question": question,
        "options": options,
        "blocking_issue_ids": blocking_issue_ids,
    }
    sentinel = project_dir / ".relay" / ("CODER_DONE.json" if role == "coder" else "CLAUDE_DONE.json")
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(json.dumps(payload, indent=2))
    return sentinel


class FakeAppDriver(AppDriver):
    """A driver whose `ring` calls a callback instead of typing.

    The callback is the fake app's "agent": it writes files and a sentinel.
    """

    def __init__(
        self,
        name: str,
        project_dir: str | Path,
        *,
        on_ring: Callable[["FakeAppDriver", str], None] | None = None,
        context_pct: float | None = None,
        busy: bool | None = False,
        popup: Popup | None = None,
        sent: bool | None = True,
    ):
        self.name = name
        self.project_dir = Path(project_dir)
        self.on_ring = on_ring
        self.context_pct = context_pct
        self.busy = busy
        self.popup = popup
        self.sent = sent
        self.rings: list[str] = []
        self.focus_calls = 0
        self.focus_failures = 0
        self.new_session_calls = 0
        self.models: list[tuple[str, str]] = []
        self.model_roles: list[str | None] = []
        self.model_fails = False
        self.effort_verified: bool | str = True
        self.verified: list[bool | None] = []
        self.idle_waits = 0

    def focus(self) -> None:
        self.focus_calls += 1
        if self.focus_failures:
            self.focus_failures -= 1
            raise AppMismatch("window is not frontmost")

    def new_session(self) -> None:
        self.new_session_calls += 1

    def select_model(self, name: str, effort: str, role: str | None = None) -> dict:
        from ..errors import HumanRequired

        self.models.append((name, effort))
        self.model_roles.append(role)
        if self.model_fails:
            raise HumanRequired(
                f"could not verify the model label in {self.name}", reason="model verification failed"
            )
        return {"model_verified": True, "effort_verified": self.effort_verified}

    def ring(self, pointer_line: str) -> None:
        self.rings.append(pointer_line)
        if self.on_ring:
            self.on_ring(self, pointer_line)

    def verify_sent(self) -> bool | None:
        self.verified.append(self.sent)
        return self.sent

    def read_context_pct(self) -> float | None:
        return self.context_pct

    def is_busy(self) -> bool | None:
        return self.busy

    def read_popup(self) -> Popup | None:
        return self.popup

    def click_popup(self, popup: Popup, button: str) -> None:
        self.popup = None
        self.clicked = (popup.text, button)

    def wait_for_idle(self, seconds: int) -> None:
        self.idle_waits += 1

    def is_user_idle(self, seconds: int) -> bool:
        return True


def pointer_role(pointer: str) -> str | None:
    for token in pointer.split():
        if token.startswith("role="):
            return token.split("=", 1)[1].strip(":")
    return None


def pointer_round(pointer: str) -> int | None:
    parts = pointer.split()
    for index, token in enumerate(parts):
        if token == "round" and index + 1 < len(parts):
            try:
                return int(parts[index + 1].strip(":"))
            except ValueError:
                return None
        if token.startswith("round="):
            try:
                return int(token.split("=", 1)[1].strip(":"))
            except ValueError:
                return None
    return None
