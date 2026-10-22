"""Small shared helpers: canonical JSON, hashing, ASCII pointer text."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def short_hash(value: str, n: int = 12) -> str:
    return value[:n]


_ASCII_FOLD = {
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u00a0": " ",
}


def ascii_pointer(text: str) -> str:
    """Fold typographic punctuation, drop every non-ASCII char, collapse whitespace.

    The pointer is typed as keystrokes, so it must survive any keyboard layout.
    """
    for src, dst in _ASCII_FOLD.items():
        text = text.replace(src, dst)
    text = text.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[ \t]+", " ", text).strip()


def truncate(text: str, limit: int, marker: str = "\n[... truncated ...]") -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(marker))] + marker


def parse_json_lenient(text: str) -> Any:
    """Parse JSON that may be wrapped in a markdown fence or surrounded by prose."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise json.JSONDecodeError("no JSON object found", text, 0)
