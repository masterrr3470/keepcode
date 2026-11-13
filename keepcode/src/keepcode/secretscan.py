"""Detect secrets before they reach a remote repository.

Two layers:

* file names that almost always hold secrets (.env, private keys, keystores);
* content rules for well-known token formats.

A line that contains the marker `keepcode:allow` is never reported.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ALLOW_MARKER = "keepcode:allow"
MAX_SCAN_BYTES = 1_000_000

ENV_EXAMPLES = {
    ".env.example",
    ".env.sample",
    ".env.template",
    ".env.dist",
    ".env.defaults",
    ".env.schema",
}

SECRET_NAMES = {
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    ".netrc",
    "credentials.json",
    "secrets.json",
    "service-account.json",
    "serviceaccountkey.json",
}

SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx", ".ppk")

SKIP_CONTENT = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "pipfile.lock",
    "cargo.lock",
    "composer.lock",
    "go.sum",
    "bun.lockb",
}


def is_secret_filename(path: str) -> bool:
    name = PurePosixPath(path.replace("\\", "/")).name.lower()
    if name in ENV_EXAMPLES or any(word in name for word in ("example", "sample", "template")):
        return False
    if name == ".env" or name.startswith(".env.") or name.endswith(".env"):
        return True
    if name in SECRET_NAMES:
        return True
    if name.startswith("client_secret") and name.endswith(".json"):
        return True
    if "firebase-adminsdk" in name and name.endswith(".json"):
        return True
    return name.endswith(SECRET_SUFFIXES)


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]


RULES = [
    Rule("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    Rule("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    Rule("GitHub token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    Rule("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    Rule("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    Rule("Stripe live key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{20,}\b")),
    Rule("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    Rule("OpenAI or Anthropic key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{32,}\b")),
    Rule("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    Rule("npm auth token", re.compile(r"_authToken\s*=\s*[^\s$][^\s]{8,}")),
    Rule("PyPI token", re.compile(r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{50,}\b")),
    Rule("SendGrid key", re.compile(r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b")),
    Rule("Telegram bot token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{33}\b")),
    Rule(
        "Discord bot token",
        re.compile(r"\b[MNO][A-Za-z0-9_-]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,38}\b"),
    ),
    Rule(
        "Private key",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----"),
    ),
]

JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.(eyJ[A-Za-z0-9_-]{8,})\.[A-Za-z0-9_-]{8,}\b")


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    rule: str
    preview: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}  {self.rule}  ({self.preview})"


def mask(value: str) -> str:
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}****{value[-2:]}"


def _jwt_role(payload_b64: str) -> str | None:
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return None
    return payload.get("role") if isinstance(payload, dict) else None


def scan_text(text: str, file: str = "") -> list[Finding]:
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        for rule in RULES:
            match = rule.pattern.search(line)
            if match:
                findings.append(Finding(file, number, rule.name, mask(match.group(0))))
                break
        else:
            # Supabase service_role keys are JWTs: only that role is secret,
            # the public anon key looks the same and is safe to commit.
            for match in JWT.finditer(line):
                if _jwt_role(match.group(1)) == "service_role":
                    findings.append(Finding(file, number, "Supabase service_role key", mask(match.group(0))))
                    break
    return findings


def scan_file(root: Path, relative: str) -> list[Finding]:
    name = PurePosixPath(relative.replace("\\", "/")).name.lower()
    if name in SKIP_CONTENT or name.endswith((".min.js", ".map")):
        return []
    path = root / relative
    try:
        if not path.is_file() or path.stat().st_size > MAX_SCAN_BYTES:
            return []
        data = path.read_bytes()
    except OSError:
        return []
    if b"\0" in data[:8192]:
        return []  # binary file
    return scan_text(data.decode("utf-8", errors="ignore"), relative)


def scan_files(root: Path, files: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for relative in files:
        findings.extend(scan_file(root, relative))
    return findings
