import base64
import json
from pathlib import Path

import pytest

from keepcode import gitignore
from keepcode.secretscan import is_secret_filename, mask, scan_file, scan_text


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.local",
        "config/.env.production",
        "prod.env",
        "server.pem",
        "id_rsa",
        "app.keystore",
        "client_secret_123.json",
        "my-firebase-adminsdk-abc.json",
    ],
)
def test_secret_file_names(name):
    assert is_secret_filename(name)


@pytest.mark.parametrize("name", [".env.example", ".env.sample", "env.template", "README.md", "keys.py", ".npmrc"])
def test_safe_file_names(name):
    assert not is_secret_filename(name)


def jwt(payload: dict) -> str:
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(payload)}.c2lnbmF0dXJlc2lnbmF0dXJl"


@pytest.mark.parametrize(
    ("line", "rule"),
    [
        ("token = 'ghp_" + "a" * 36 + "'", "GitHub token"),
        ("AWS_KEY=AKIA" + "A" * 16, "AWS access key"),
        ("key: AIza" + "b" * 35, "Google API key"),
        ("OPENAI = 'sk-proj-" + "c" * 40 + "'", "OpenAI or Anthropic key"),
        ("-----BEGIN RSA PRIVATE KEY-----", "Private key"),
        ("client.login('MTA" + "x" * 21 + ".Gabcde." + "y" * 30 + "')", "Discord bot token"),
        ("//registry.npmjs.org/:_authToken=abcdef1234567890", "npm auth token"),
    ],
)
def test_detects_known_tokens(line, rule):
    findings = scan_text(line, "f.py")
    assert [f.rule for f in findings] == [rule]
    assert "****" in findings[0].preview


def test_supabase_service_role_is_secret_but_anon_key_is_not():
    assert scan_text(f"key = '{jwt({'role': 'service_role'})}'")[0].rule == "Supabase service_role key"
    assert scan_text(f"key = '{jwt({'role': 'anon'})}'") == []


def test_allow_marker_and_env_references_are_ignored():
    assert scan_text("t = 'ghp_" + "a" * 36 + "'  # keepcode:allow") == []
    assert scan_text("//registry.npmjs.org/:_authToken=${NPM_TOKEN}") == []
    assert scan_text("password = os.environ['DB_PASSWORD']") == []


def test_scan_file_skips_binaries_and_lockfiles(tmp_path: Path):
    (tmp_path / "image.png").write_bytes(b"\x89PNG\0\0" + b"ghp_" + b"a" * 36)
    (tmp_path / "package-lock.json").write_text("ghp_" + "a" * 36)
    assert scan_file(tmp_path, "image.png") == []
    assert scan_file(tmp_path, "package-lock.json") == []


def test_mask_never_shows_the_whole_value():
    secret = "ghp_" + "z" * 36
    assert secret not in mask(secret)
    assert mask("short") == "****"


def test_gitignore_is_completed_not_replaced(tmp_path: Path):
    (tmp_path / ".gitignore").write_text("my-own-rule\nnode_modules/\n", encoding="utf-8")
    added = gitignore.ensure(tmp_path, ["React", "Django"])
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert text.startswith("my-own-rule\nnode_modules/\n")
    assert "node_modules/" not in added
    assert ".env" in added and "dist/" in added and "*.py[cod]" in added
    assert gitignore.HEADER in text
    assert gitignore.ensure(tmp_path, ["React", "Django"]) == []  # idempotent


def test_gitignore_escape_matches_one_file():
    assert gitignore.escape("data/big[1].bin") == "/data/big\\[1\\].bin"
    assert gitignore.escape("folder\\file.zip") == "/folder/file.zip"
