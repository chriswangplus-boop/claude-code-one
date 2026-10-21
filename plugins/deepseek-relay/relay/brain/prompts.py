"""Prompt loading and rendering. Prompts are files so they can be reviewed and versioned."""

from __future__ import annotations

from pathlib import Path
from string import Template
from typing import Mapping

from ..errors import BrainError

PROMPT_VERSION = "1"
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
JUDGMENTS_DIR = PACKAGE_ROOT / "prompts" / "judgments"


def load_prompt(name: str) -> tuple[str, str]:
    path = JUDGMENTS_DIR / name
    if not path.exists():
        raise BrainError(f"missing prompt template: {path}")
    text = path.read_text(encoding="utf-8")
    marker = "\n--- user ---\n"
    if marker not in text:
        raise BrainError(f"prompt {name} has no '--- user ---' section")
    system_part, user_part = text.split(marker, 1)
    system_part = system_part.removeprefix("--- system ---\n")
    return system_part.strip(), user_part.strip()


def render(template: str, values: Mapping[str, object]) -> str:
    try:
        return Template(template).substitute({key: str(value) for key, value in values.items()})
    except KeyError as exc:
        raise BrainError(f"prompt variable not supplied: {exc.args[0]}") from exc


def fence(text: str, label: str = "agent-output") -> str:
    """Wrap untrusted text so a forged closing tag cannot escape the block."""
    cleaned = str(text).replace("</untrusted>", "[/untrusted]").replace("<untrusted", "[untrusted")
    return f'<untrusted source="{label}">\n{cleaned}\n</untrusted>'
