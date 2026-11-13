"""Minimal GitHub REST client (standard library only) and token handling.

Token lookup order:
1. KEEPCODE_GITHUB_TOKEN, GITHUB_TOKEN or GH_TOKEN environment variables;
2. the GitHub CLI (`gh auth token`), when it is installed and logged in;
3. the token saved with `keepcode login` in the system keychain
   (Windows Credential Manager, macOS Keychain, Secret Service on Linux).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from keepcode import __version__

API = "https://api.github.com"
KEYRING_SERVICE = "keepcode"
KEYRING_USER = "github-token"
ENV_VARS = ("KEEPCODE_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(f"GitHub API error {status}: {message}" if status else message)


@dataclass
class Token:
    value: str
    source: str


@dataclass
class Repo:
    full_name: str
    clone_url: str
    html_url: str


def _keyring():
    try:
        import keyring  # type: ignore[import-untyped]

        return keyring
    except Exception:  # noqa: BLE001 - a broken keyring backend must not crash keepcode
        return None


def _from_gh_cli() -> str | None:
    if shutil.which("gh") is None:
        return None
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = result.stdout.strip()
    return token if result.returncode == 0 and token else None


def find_token() -> Token | None:
    for name in ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return Token(value, f"environment variable {name}")
    value = _from_gh_cli()
    if value:
        return Token(value, "GitHub CLI (gh)")
    keyring = _keyring()
    if keyring is not None:
        try:
            value = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:  # noqa: BLE001
            value = None
        if value:
            return Token(value, "system keychain (keepcode login)")
    return None


def save_token(token: str) -> None:
    keyring = _keyring()
    if keyring is None:
        raise GitHubError(
            0, "No system keychain available. Use the KEEPCODE_GITHUB_TOKEN environment variable instead."
        )
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, token)
    except Exception as exc:  # noqa: BLE001
        raise GitHubError(
            0,
            f"Could not save the token in the system keychain ({exc}). "
            "Use the KEEPCODE_GITHUB_TOKEN environment variable instead.",
        ) from exc


def delete_token() -> bool:
    keyring = _keyring()
    if keyring is None:
        return False
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        return True
    except Exception:  # noqa: BLE001
        return False


def push_env(token: str) -> dict[str, str]:
    """Git environment that authenticates HTTPS requests to github.com with the token.

    The token travels as an HTTP header set through environment variables, so it
    is never written to .git/config and never appears in the command line.
    """
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }


def is_github_https(url: str | None) -> bool:
    return bool(url) and url.lower().startswith("https://github.com/")


def repo_name(folder_name: str) -> str:
    """GitHub accepts letters, digits, '-', '_' and '.' in repository names."""
    ascii_name = unicodedata.normalize("NFKD", folder_name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", ascii_name).strip("-.")
    name = re.sub(r"-{2,}", "-", name)
    return (name or "project")[:90]


class GitHub:
    def __init__(self, token: str, timeout: float = 20) -> None:
        self.token = token
        self.timeout = timeout
        self._user: dict[str, Any] | None = None

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            API + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": f"keepcode/{__version__}",
                **({"Content-Type": "application/json"} if data else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b"{}")
            except json.JSONDecodeError:
                payload = {}
            message = payload.get("message") or exc.reason
            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                details = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors)
                message = f"{message} ({details})"
            raise GitHubError(exc.code, str(message)) from exc
        except urllib.error.URLError as exc:
            raise GitHubError(0, f"Could not reach GitHub: {exc.reason}") from exc
        except TimeoutError as exc:
            raise GitHubError(0, "GitHub did not answer in time") from exc

    def user(self) -> dict[str, Any]:
        if self._user is None:
            self._user = self.request("GET", "/user")
        return self._user

    def identity(self) -> tuple[str, str]:
        user = self.user()
        login = user.get("login", "keepcode")
        name = user.get("name") or login
        return name, f"{user.get('id')}+{login}@users.noreply.github.com"

    def create_private_repo(self, folder_name: str, description: str) -> Repo:
        base = repo_name(folder_name)
        for attempt in range(1, 10):
            name = base if attempt == 1 else f"{base}-{attempt}"
            try:
                data = self.request(
                    "POST",
                    "/user/repos",
                    {
                        "name": name,
                        "private": True,
                        "description": description,
                        "auto_init": False,
                        "has_wiki": False,
                    },
                )
            except GitHubError as exc:
                if exc.status == 422 and "already exists" in str(exc).lower():
                    continue
                raise
            return Repo(data["full_name"], data["clone_url"], data["html_url"])
        raise GitHubError(422, f"Could not find a free repository name for '{base}'")


class RepoCreator:
    """Interface used by the backup engine, so tests can use local remotes."""

    def create(self, folder_name: str) -> Repo:  # pragma: no cover - interface
        raise NotImplementedError

    def push_env(self, url: str | None) -> dict[str, str]:  # pragma: no cover - interface
        return {}

    def identity(self) -> tuple[str, str]:  # pragma: no cover - interface
        return "keepcode", "keepcode@localhost"


class GitHubCreator(RepoCreator):
    def __init__(self, client: GitHub) -> None:
        self.client = client

    def create(self, folder_name: str) -> Repo:
        return self.client.create_private_repo(folder_name, "Private backup created by keepcode")

    def push_env(self, url: str | None) -> dict[str, str]:
        return push_env(self.client.token) if is_github_https(url) else {}

    def identity(self) -> tuple[str, str]:
        try:
            return self.client.identity()
        except GitHubError:
            return "keepcode", "keepcode@localhost"
