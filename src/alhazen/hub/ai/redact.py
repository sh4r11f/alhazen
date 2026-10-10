"""Scrubbing a rig's run log before it is sent to an AI provider.

Hides: the patterns treated as secrets. A run log is the person's own text
(a traceback, the session log), but logs routinely carry credentials:
signed or token-bearing URLs, Authorization headers, API keys printed by a
library, ``password=`` settings. Each match is replaced by a marker naming
its kind, and the number of replacements is recorded with the job, so the
disclosure says what was removed without repeating it.

Deliberately not removed: SHA-256 digests, paths and ordinary numbers,
which a repair needs (a provenance digest is not a secret).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Order matters: whole token-bearing URLs first, then headers, then bare keys.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # user:password@ in any URL
    ("url-credentials", re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@")),
    # a URL whose query carries a credential-like parameter
    (
        "url-token",
        re.compile(
            r"(?i)\bhttps?://[^\s\"'<>]*[?&](?:[a-z0-9_.-]*(?:token|key|sig|signature|secret|"
            r"auth|password|credential|session)[a-z0-9_.-]*)=[^\s\"'<>]*"
        ),
    ),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("basic-auth", re.compile(r"(?i)\bauthorization:\s*basic\s+[A-Za-z0-9+/=]{8,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("api-key", re.compile(r"\bsk-(?:ant-|or-|proj-)?[A-Za-z0-9_-]{16,}")),
    ("api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    (
        "api-key",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"),
    ),
    ("api-key", re.compile(r"\bhf_[A-Za-z0-9]{20,}")),
    ("api-key", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("api-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "private-key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    ),
    # name = value / name: value settings that name a secret
    (
        "setting",
        re.compile(
            r"(?i)\b([a-z0-9_.-]*(?:api[_-]?key|token|secret|password|passwd|"
            r"authorization|credential)[a-z0-9_.-]*)(\s*[:=]\s*)(['\"]?)"
            r"(?!\[redacted)(?![0-9.]+(?:[\s'\",;]|$))[^\s'\",;]{4,}\3"
        ),
    ),
)


def clean_log(text: str) -> str:
    """Line endings to ``\\n``, terminal colour codes and control characters
    (other than tab and newline) removed."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL.sub("", _ANSI.sub("", text))


def redact(text: str, known: Iterable[str] = ()) -> tuple[str, int]:
    """``text`` with secrets replaced by ``[redacted <kind>]``; returns the
    text and the number of replacements. ``known`` are exact values to
    remove wherever they appear (the requester's own stored keys)."""
    count = 0
    for value in known:
        if value and len(value) >= 8 and value in text:
            count += text.count(value)
            text = text.replace(value, "[redacted stored-key]")

    for kind, pattern in _PATTERNS:

        def mark(match: re.Match[str], kind: str = kind) -> str:
            if kind == "url-credentials":
                return f"{match.group(1)}[redacted credentials]@"
            if kind == "setting":
                return f"{match.group(1)}{match.group(2)}[redacted]"
            return f"[redacted {kind}]"

        text, found = pattern.subn(mark, text)
        count += found
    return text, count
