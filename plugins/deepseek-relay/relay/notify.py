"""Desktop + ntfy notifications. Push only ever carries a short question and a decision ID."""

from __future__ import annotations

import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Callable

from .clock import RealClock
from .config import Config, api_key, ntfy_topic, ntfy_token
from .journal import Journal
from .redact import redact
from .util import ascii_pointer, truncate

DesktopFn = Callable[[str, str, bool], bool]
NtfyFn = Callable[[str, str, str, str, str | None, bool], bool]


class Notifier:
    def __init__(
        self,
        cfg: Config,
        journal: Journal,
        *,
        clock: RealClock | None = None,
        desktop_fn: DesktopFn | None = None,
        ntfy_fn: NtfyFn | None = None,
        secrets: list[str] | None = None,
        actor: str = "orchestrator",
    ):
        self.cfg = cfg
        self.journal = journal
        self.clock = clock or RealClock()
        self.desktop_fn = desktop_fn
        self.ntfy_fn = ntfy_fn
        self.secrets = secrets or [s for s in (api_key(), ntfy_topic(), ntfy_token()) if s]
        self.actor = actor

    def send(
        self,
        title: str,
        body: str,
        *,
        decision_id: str | None = None,
        urgent: bool = False,
        desktop_extra: str | None = None,
        ntfy_body: str | None = None,
    ) -> dict:
        clean_body = truncate(redact(body, self.secrets).text, 300, marker="...")
        desktop_text = truncate(
            redact(body + (f"\n{desktop_extra}" if desktop_extra else ""), self.secrets).text,
            500, marker="..."
        )
        ntfy_text = truncate(redact(ntfy_body or body, self.secrets).text, 300, marker="...")
        clean_title = ascii_pointer(redact(title, self.secrets).text)[:120]
        results: dict[str, bool] = {}
        errors: list[str] = []

        if bool(self.cfg.get("notify.desktop", True)):
            try:
                if self.desktop_fn:
                    results["desktop"] = bool(self.desktop_fn(clean_title, desktop_text, urgent))
                else:
                    results["desktop"] = self._desktop(clean_title, desktop_text, urgent)
            except Exception as exc:  # notifications must never break the loop
                results["desktop"] = False
                errors.append(f"desktop: {type(exc).__name__}")

        if bool(self.cfg.get("notify.ntfy", True)):
            topic = self.cfg.get("ntfy.topic") or ntfy_topic()
            if topic:
                server = str(self.cfg.get("ntfy.server", "https://ntfy.sh"))
                token = self.cfg.get("ntfy.token") or ntfy_token()
                try:
                    if self.ntfy_fn:
                        results["ntfy"] = bool(self.ntfy_fn(server, topic, clean_title, ntfy_text, token, urgent))
                    else:
                        results["ntfy"] = self._ntfy(server, topic, clean_title, ntfy_text, token, urgent)
                except Exception as exc:
                    results["ntfy"] = False
                    errors.append(f"ntfy: {type(exc).__name__}")
            else:
                results["ntfy"] = False
                errors.append("ntfy: no topic configured")

        self.journal.append(
            "notify.sent",
            actor=self.actor,
            detail={
                "channels": results,
                "decision_id": decision_id,
                "errors": errors,
                "chars": len(clean_body),
                "desktop_chars": len(desktop_text),
                "ntfy_chars": len(ntfy_text),
            },
            result="ok" if any(results.values()) else "failed",
        )
        return {"channels": results, "errors": errors}

    def _desktop(self, title: str, body: str, urgent: bool) -> bool:
        if sys.platform != "darwin":
            print(f"[relay] {title}: {body}")
            return False
        osascript = shutil.which("osascript")
        if not osascript:
            return False
        script = (
            'display notification '
            f'"{_escape(body)}" with title "{_escape(title)}"'
            + (' sound name "Glass"' if urgent else "")
        )
        result = subprocess.run(
            [osascript, "-e", script], capture_output=True, text=True, timeout=10, check=False
        )
        return result.returncode == 0

    def _ntfy(
        self, server: str, topic: str, title: str, body: str, token: str | None, urgent: bool
    ) -> bool:
        url = f"{server.rstrip('/')}/{topic}"
        headers = {
            "Title": title,
            "Priority": "high" if urgent else "default",
            "Tags": "warning" if urgent else "robot",
            "Content-Type": "text/plain; charset=utf-8",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                return 200 <= response.status < 300
        except (urllib.error.URLError, TimeoutError, OSError):
            return False


class FakeNotifier:
    def __init__(self, answer_fn=None):
        self.sent: list[dict] = []
        self.answer_fn = answer_fn

    def send(self, title, body, *, decision_id=None, urgent=False, desktop_extra=None, ntfy_body=None):
        self.sent.append({
            "title": title, "body": body, "desktop_body": body + (f"\n{desktop_extra}" if desktop_extra else ""),
            "ntfy_body": ntfy_body or body, "decision_id": decision_id, "urgent": urgent,
        })
        if self.answer_fn and decision_id:
            self.answer_fn(decision_id)
        return {"channels": {"fake": True}, "errors": []}


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")
