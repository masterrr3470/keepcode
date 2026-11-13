"""Thin, safe wrapper around the git command line.

Every call disables features that could run code stored inside a repository
(fsmonitor) and never waits for a password prompt, because keepcode looks at
many folders it did not create, including repositories downloaded from the web.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SAFE_CONFIG = [
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.longpaths=true",
    "-c",
    "core.quotepath=false",
    "-c",
    "color.ui=false",
]

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class GitError(RuntimeError):
    def __init__(self, args: list[str], code: int, stderr: str) -> None:
        self.code = code
        self.stderr = stderr.strip()
        message = self.stderr.splitlines()[-1] if self.stderr else f"exit code {code}"
        super().__init__(f"git {' '.join(args[:2])}: {message}")


def git_available() -> bool:
    return shutil.which("git") is not None


def git_version() -> str | None:
    try:
        return run(["--version"], cwd=Path.cwd()).strip()
    except (GitError, OSError):
        return None


def base_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GCM_INTERACTIVE": "never",
            "LC_ALL": "C",
            "LANGUAGE": "C",
        }
    )
    if extra:
        env.update(extra)
    return env


def run(
    args: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout: float = 120,
    input_text: str | None = None,
) -> str:
    """Run git and return stdout. Raises GitError when check is true and git fails."""
    try:
        completed = subprocess.run(
            ["git", *SAFE_CONFIG, *args],
            cwd=str(cwd),
            env=base_env(env),
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(args, -1, f"timed out after {timeout:.0f}s") from exc
    except FileNotFoundError as exc:
        raise GitError(args, -1, "git is not installed or not on PATH") from exc
    if check and completed.returncode != 0:
        raise GitError(args, completed.returncode, completed.stderr)
    return completed.stdout


def succeeds(args: list[str], cwd: Path, env: dict[str, str] | None = None) -> bool:
    try:
        run(args, cwd, env=env)
        return True
    except GitError:
        return False


def is_repo(path: Path) -> bool:
    return (path / ".git").exists()


def toplevel(path: Path) -> Path | None:
    """Root of the work tree that contains path, or None outside any repository."""
    try:
        out = run(["rev-parse", "--show-toplevel"], path).strip()
    except GitError:
        return None
    return Path(out) if out else None


def has_commits(path: Path) -> bool:
    return succeeds(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], path)


def current_branch(path: Path) -> str | None:
    """Branch name, also for an unborn branch. None when HEAD is detached."""
    try:
        return run(["symbolic-ref", "--quiet", "--short", "HEAD"], path).strip() or None
    except GitError:
        return None


def remotes(path: Path) -> list[str]:
    return [line.strip() for line in run(["remote"], path).splitlines() if line.strip()]


def preferred_remote(path: Path) -> str | None:
    names = remotes(path)
    if not names:
        return None
    return "origin" if "origin" in names else names[0]


def remote_url(path: Path, name: str) -> str | None:
    try:
        return run(["remote", "get-url", name], path).strip() or None
    except GitError:
        return None


@dataclass
class Change:
    code: str
    path: str


def status(path: Path, env: dict[str, str] | None = None) -> list[Change]:
    """Uncommitted changes, one entry per file, untracked files included."""
    out = run(["status", "--porcelain=v1", "-z", "--untracked-files=all"], path, env=env)
    records = out.split("\0")
    changes: list[Change] = []
    i = 0
    while i < len(records):
        record = records[i]
        i += 1
        if len(record) < 4:
            continue
        code, file_path = record[:2], record[3:]
        changes.append(Change(code, file_path))
        if code[0] in "RC":
            i += 1  # the next record is the original path of a rename or copy
    return changes


def fingerprint(path: Path, changes: list[Change]) -> str:
    """Cheap identity of the current uncommitted work.

    Used to know whether the working tree changed since keepcode last made a
    snapshot of it, without building a new snapshot on every scan.
    """
    digest = hashlib.sha256()
    for change in sorted(changes, key=lambda c: c.path):
        digest.update(change.code.encode())
        digest.update(change.path.encode("utf-8", "replace"))
        try:
            info = os.stat(path / change.path)
            digest.update(f"{info.st_size}:{info.st_mtime_ns}".encode())
        except OSError:
            digest.update(b"missing")
    return digest.hexdigest()


def unpushed_total(path: Path) -> int:
    """Commits on any local branch that no remote-tracking ref contains."""
    out = run(["rev-list", "--count", "--branches", "--not", "--remotes"], path)
    return int(out.strip() or 0)


def unpushed_by_branch(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    out = run(["for-each-ref", "--format=%(refname:short)", "refs/heads"], path)
    for branch in (line.strip() for line in out.splitlines()):
        if not branch:
            continue
        count = int(run(["rev-list", "--count", f"refs/heads/{branch}", "--not", "--remotes"], path).strip() or 0)
        if count:
            result[branch] = count
    return result


def stash_count(path: Path) -> int:
    return len([line for line in run(["stash", "list"], path).splitlines() if line.strip()])


def last_commit_time(path: Path) -> float | None:
    try:
        out = run(["log", "-1", "--format=%ct"], path).strip()
        return float(out) if out else None
    except (GitError, ValueError):
        return None


def tracked_files(path: Path, env: dict[str, str] | None = None) -> list[str]:
    out = run(["ls-files", "-z"], path, env=env)
    return [name for name in out.split("\0") if name]


def identity_env(path: Path, fallback_name: str, fallback_email: str) -> dict[str, str]:
    """Author and committer for keepcode commits.

    Uses the identity already configured in git. When there is none, the
    fallback is applied to keepcode's own commands only; git config is not changed.
    """
    try:
        name = run(["config", "user.name"], path).strip()
    except GitError:
        name = ""
    try:
        email = run(["config", "user.email"], path).strip()
    except GitError:
        email = ""
    name = name or fallback_name
    email = email or fallback_email
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }
