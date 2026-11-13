from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from keepcode.github import Repo, RepoCreator


def sh(cwd: Path, *args: str) -> str:
    """Run git in a test repository."""
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake home folder, fake keepcode config and a private git identity for every test."""
    home = tmp_path / "home"
    home.mkdir()
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = Test User\n\temail = test@example.com\n"
        "[init]\n\tdefaultBranch = main\n"
        "[commit]\n\tgpgsign = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("KEEPCODE_HOME", str(tmp_path / "keepcode"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("KEEPCODE_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def home(isolated: Path) -> Path:
    return isolated


class LocalCreator(RepoCreator):
    """Creates bare repositories on disk instead of on GitHub."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.base.mkdir(parents=True, exist_ok=True)
        self.created: list[Path] = []

    def create(self, folder_name: str) -> Repo:
        path = self.base / f"{folder_name}.git"
        subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
        self.created.append(path)
        return Repo(f"me/{folder_name}", str(path), path.as_uri())

    def push_env(self, url: str | None) -> dict[str, str]:
        return {}

    def identity(self) -> tuple[str, str]:
        return "Fake", "fake@example.com"


@pytest.fixture
def creator(tmp_path: Path) -> LocalCreator:
    return LocalCreator(tmp_path / "remotes")


@pytest.fixture
def bare(tmp_path: Path):
    """Factory for bare remotes."""

    def make(name: str) -> Path:
        path = tmp_path / "bare" / f"{name}.git"
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
        return path

    return make


def write(path: Path, text: str = "x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def commit_all(repo: Path, message: str = "commit") -> None:
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", message)


def new_repo(path: Path, files: dict[str, str] | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    sh(path, "init", "-q")
    for name, content in (files or {"README.md": "hello\n"}).items():
        write(path / name, content)
    commit_all(path, "init")
    return path
