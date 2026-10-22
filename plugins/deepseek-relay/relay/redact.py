"""Outbound redaction. Every DeepSeek payload passes through here; only counts are journaled."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}")
HOME_POSIX = re.compile(r"/Users/[^/\s\"']+")
HOME_WIN = re.compile(r"[A-Za-z]:\\Users\\[^\\\s\"']+")
TOKENS: list[tuple[str, re.Pattern[str]]] = [
    ("api_key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
]
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")

KEYED = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|authorization)\b"
    r"(\s*[:=]\s*)([\"']?)([^\s\"'#,]{6,})"
)
ENV_LINE = re.compile(
    r"(?m)^([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_KEY|PRIVATE_KEY)[A-Z0-9_]*)(\s*=\s*)(\S+)"
)
ENTROPY = re.compile(r"[A-Za-z0-9+/=_-]{32,}")

SENSITIVE_KEYS = {
    "api_key", "apikey", "token", "password", "secret", "authorization",
    "x-api-key", "access_token", "refresh_token", "private_key", "client_secret",
}
PLACEHOLDER = re.compile(r"(?i)(?:your[_-]?|example|placeholder|changeme|redacted|\[secret\]|<[^>]*>|\$\{|os\.environ|none|null)")


def _entropy(value: str) -> float:
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    total = len(value)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def _is_probably_not_secret(value: str) -> bool:
    if PLACEHOLDER.search(value):
        return True
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value):
        return True  # git / sha256 digests are referenced constantly and are not credentials
    if value.startswith(("/", "./", "../", "http://", "https://")):
        return True
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return True
    return False


@dataclass
class Redaction:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.counts)

    def bump(self, rule: str, n: int = 1) -> None:
        self.counts[rule] = self.counts.get(rule, 0) + n


def redact(text: str, extra_secrets: list[str] | tuple[str, ...] = ()) -> Redaction:
    if not text:
        return Redaction(text="")
    result = Redaction(text=text)

    for secret in extra_secrets:
        if secret and len(secret) >= 6 and secret in result.text:
            result.text = result.text.replace(secret, "[secret]")
            result.bump("known_secret")

    new, n = PRIVATE_KEY.subn("[private-key]", result.text)
    if n:
        result.text = new
        result.bump("private_key", n)

    for rule, pattern in TOKENS:
        new, n = pattern.subn("[secret]", result.text)
        if n:
            result.text = new
            result.bump(rule, n)

    new, n = JWT.subn("[jwt]", result.text)
    if n:
        result.text = new
        result.bump("jwt", n)

    def bearer(match: re.Match[str]) -> str:
        return f"{match.group(1)} [secret]"

    new, n = BEARER.subn(bearer, result.text)
    if n:
        result.text = new
        result.bump("bearer", n)

    def keyed(match: re.Match[str]) -> str:
        key, sep, quote, value = match.group(1), match.group(2), match.group(3), match.group(4)
        if _is_probably_not_secret(value):
            return match.group(0)
        result.bump("keyed_secret")
        return f"{key}{sep}{quote}[secret]"

    result.text = KEYED.sub(keyed, result.text)

    def env(match: re.Match[str]) -> str:
        if _is_probably_not_secret(match.group(3)):
            return match.group(0)
        result.bump("env_secret")
        return f"{match.group(1)}{match.group(2)}[secret]"

    result.text = ENV_LINE.sub(env, result.text)

    new, n = EMAIL.subn("[email]", result.text)
    if n:
        result.text = new
        result.bump("email", n)

    def home(match: re.Match[str]) -> str:
        result.bump("home_path")
        return "/Users/<user>" if match.group(0).startswith("/") else match.group(0).split("\\")[0] + "\\Users\\<user>"

    result.text = HOME_POSIX.sub(home, result.text)
    result.text = HOME_WIN.sub(home, result.text)

    def entropy(match: re.Match[str]) -> str:
        value = match.group(0)
        if _is_probably_not_secret(value):
            return value
        classes = sum(bool(re.search(pattern, value)) for pattern in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[+/=_-]"))
        if classes >= 3 and _entropy(value) >= 4.2:
            result.bump("high_entropy")
            return "[secret]"
        return value

    result.text = ENTROPY.sub(entropy, result.text)
    return result


def redact_obj(obj: Any, extra_secrets: list[str] | tuple[str, ...] = ()) -> tuple[Any, dict[str, int]]:
    """Recursively redact strings and sensitive dict values. Returns (clean_obj, counts)."""
    counts: dict[str, int] = {}

    def add(report: Redaction) -> None:
        for key, value in report.counts.items():
            counts[key] = counts.get(key, 0) + value

    def walk(value: Any, key: str | None = None) -> Any:
        if isinstance(value, str):
            if key and key.lower() in SENSITIVE_KEYS:
                counts["sensitive_field"] = counts.get("sensitive_field", 0) + 1
                return "[secret]"
            report = redact(value, extra_secrets)
            add(report)
            return report.text
        if isinstance(value, dict):
            return {str(k): walk(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [walk(item) for item in value]
        return value

    return walk(obj), counts
