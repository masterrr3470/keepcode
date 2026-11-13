"""Back projects up without getting in the way.

Rules keepcode follows:

* Your working files, your staging area and your branches are never changed.
  Commits are built with a temporary index and `git commit-tree`, so no hooks
  run and nothing you staged is touched.
* On a remote you already use, keepcode never pushes your real branches (that
  could trigger a deploy). It saves work to `keepcode/<branch>` branches.
* A project with no remote gets a new private GitHub repository.
* Secret files are left out, and hardcoded secrets block the backup unless
  the user explicitly allows them.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from keepcode import git, gitignore, secretscan
from keepcode.config import State
from keepcode.github import GitHubError, RepoCreator
from keepcode.models import IssueKind, Project, Severity

MAX_FILE_BYTES = 95 * 1024 * 1024  # GitHub rejects files over 100 MB
BACKUP_PREFIX = "keepcode/"


class BackupBlocked(Exception):
    def __init__(self, findings: list[secretscan.Finding]) -> None:
        self.findings = findings
        count = len(findings)
        super().__init__(f"{count} hardcoded secret{'s' if count != 1 else ''} found")


@dataclass
class Plan:
    project: Project
    steps: list[str]
    creates_repo: bool = False


@dataclass
class Result:
    project: Project
    ok: bool
    summary: str
    details: list[str] = field(default_factory=list)
    url: str | None = None


def _commits(count: int) -> str:
    return f"{count} commit" if count == 1 else f"{count} commits"


def _needs_snapshot(project: Project) -> bool:
    issue = project.get(IssueKind.UNCOMMITTED)
    return issue is not None and issue.severity >= Severity.WARNING


def make_plan(project: Project) -> Plan:
    branch = project.branch or "detached-HEAD"
    stacks = ", ".join(project.stacks) or "this project"
    if project.has(IssueKind.NO_GIT) or project.has(IssueKind.NO_COMMITS):
        steps = []
        if project.has(IssueKind.NO_GIT):
            steps.append("Create a git repository (branch main)")
        steps += [
            f"Complete the .gitignore for {stacks}",
            "Leave secret files (.env, keys) and files over 95 MB out",
            "Save every other file in a first commit",
        ]
        if project.remote is None:
            steps += ["Create a private GitHub repository", "Push the project to it"]
            return Plan(project, steps, creates_repo=True)
        steps.append(f"Push it to {project.remote}/keepcode/{branch}")
        return Plan(project, steps)
    if project.has(IssueKind.NO_REMOTE):
        steps = ["Create a private GitHub repository", "Push every branch and tag to it"]
        if _needs_snapshot(project):
            steps.append(f"Save your uncommitted work to keepcode/{branch}")
        return Plan(project, steps, creates_repo=True)
    steps = []
    if _needs_snapshot(project):
        steps.append(
            f"Save your uncommitted work to {project.remote}/keepcode/{branch} "
            "(your files and branches stay as they are)"
        )
    unpushed = project.get(IssueKind.UNPUSHED)
    if unpushed:
        steps.append(f"Copy {_commits(unpushed.count)} not pushed yet to {project.remote}/keepcode/* branches")
    return Plan(project, steps)


@contextlib.contextmanager
def temp_index() -> Iterator[dict[str, str]]:
    """A private staging area, so the user's own index is never modified."""
    with tempfile.TemporaryDirectory(prefix="keepcode-") as folder:
        yield {"GIT_INDEX_FILE": str(Path(folder) / "index")}


def _remove_readonly(func: Callable[..., object], path: str, _exc: object) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree(path: Path) -> None:
    if not path.exists():
        return
    try:
        shutil.rmtree(path, onexc=_remove_readonly)  # type: ignore[call-arg]
    except TypeError:  # Python < 3.12
        shutil.rmtree(path, onerror=_remove_readonly)


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


class Engine:
    def __init__(
        self,
        creator: RepoCreator | None,
        state: State,
        allow_secrets: bool = False,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.creator = creator
        self.state = state
        self.allow_secrets = allow_secrets
        self.log = log or (lambda _message: None)

    # ----------------------------------------------------------------- public

    def run(self, project: Project) -> Result:
        if self.creator is None and make_plan(project).creates_repo:
            return Result(project, False, "A GitHub token is needed to create the repository (keepcode login)")
        try:
            if project.has(IssueKind.NO_GIT) or project.has(IssueKind.NO_COMMITS):
                return self._first_backup(project)
            if project.has(IssueKind.NO_REMOTE):
                return self._new_remote(project)
            return self._sync(project)
        except BackupBlocked as exc:
            return Result(
                project,
                False,
                "Stopped: secrets found in the code",
                [str(f) for f in exc.findings[:10]]
                + [
                    "Move them to a .env file, mark the line with 'keepcode:allow' "
                    "or run again with --allow-secrets (the repository is private)."
                ],
            )
        except (git.GitError, GitHubError, OSError) as exc:
            return Result(project, False, f"Failed: {exc}")

    # ------------------------------------------------------------ main flows

    def _first_backup(self, project: Project) -> Result:
        path = project.path
        created_git = not git.is_repo(path)
        gitignore_file = path / ".gitignore"
        original = gitignore_file.read_text(encoding="utf-8") if gitignore_file.exists() else None
        details: list[str] = []
        try:
            if created_git:
                self.log("Creating git repository")
                if not git.succeeds(["init", "-q", "-b", "main"], path):
                    git.run(["init", "-q"], path)
                    git.run(["symbolic-ref", "HEAD", "refs/heads/main"], path)
            branch = git.current_branch(path) or "main"
            added = gitignore.ensure(path, project.stacks)
            if added:
                details.append(f".gitignore: added {len(added)} entries")
            with temp_index() as index:
                git.run(["add", "-A"], path, env=index, timeout=600)
                staged = git.tracked_files(path, env=index)
                excluded = self._exclusions(path, staged)
                if excluded:
                    gitignore.ensure(path, [], [gitignore.escape(f) for f in excluded])
                    git.run(["rm", "--cached", "-q", "--", *excluded], path, env=index)
                    git.run(["add", "--", ".gitignore"], path, env=index)
                    details.append("Left out: " + ", ".join(excluded[:5]) + ("..." if len(excluded) > 5 else ""))
                skip = set(excluded)
                kept = [f for f in staged if f not in skip]
                self._check_secrets(path, kept)
                tree = git.run(["write-tree"], path, env=index).strip()
            identity = self._identity(path)
            commit = git.run(["commit-tree", tree, "-m", "Initial backup by keepcode"], path, env=identity).strip()
            git.run(["update-ref", f"refs/heads/{branch}", commit], path)
            git.run(["reset", "-q"], path)  # align the user's index with the new commit
        except BaseException:
            if created_git:
                _rmtree(path / ".git")
            if original is None:
                with contextlib.suppress(OSError):
                    gitignore_file.unlink()
            else:
                gitignore_file.write_text(original, encoding="utf-8")
            raise

        details.append(f"First commit created on {branch}")
        if project.remote is None:
            return self._publish_new_repo(project, details, push_args=["-u", "origin", branch])
        url = git.remote_url(path, project.remote)
        self._push(path, [project.remote, f"refs/heads/{branch}:refs/heads/{BACKUP_PREFIX}{branch}"], url, force=True)
        return Result(project, True, f"Saved to {project.remote}/{BACKUP_PREFIX}{branch}", details)

    def _new_remote(self, project: Project) -> Result:
        details: list[str] = []
        if project.has(IssueKind.TRACKED_SECRET):
            details.append(
                "Warning: secret files are part of the git history. "
                "Remove them from history before making this repository public."
            )
        result = self._publish_new_repo(project, details, push_args=["-u", "origin", "--all"])
        if not result.ok:
            return result
        path = project.path
        url = git.remote_url(path, "origin")
        with contextlib.suppress(git.GitError):
            self._push(path, ["origin", "--tags"], url)
        if _needs_snapshot(project):
            project.remote = "origin"
            try:
                saved = self._save_work(project, url)
            except BackupBlocked as exc:
                saved = f"Uncommitted work not saved: {exc} (see keepcode backup --help)"
            if saved:
                result.details.append(saved)
        return result

    def _sync(self, project: Project) -> Result:
        path = project.path
        remote = project.remote or git.preferred_remote(path)
        if remote is None:
            return Result(project, False, "No remote configured")
        project.remote = remote
        url = git.remote_url(path, remote)
        details: list[str] = []
        blocked: BackupBlocked | None = None
        if _needs_snapshot(project):
            try:
                saved = self._save_work(project, url)
            except BackupBlocked as exc:
                blocked, saved = exc, None
            if saved:
                details.append(saved)
        # Counted after the snapshot, which already carries the current branch's commits.
        unpushed = git.unpushed_by_branch(path)
        if unpushed:
            refspecs = [f"refs/heads/{b}:refs/heads/{BACKUP_PREFIX}{b}" for b in unpushed]
            self._push(path, [remote, *refspecs], url, force=True)
            details.append(
                f"Copied {_commits(sum(unpushed.values()))} to " + ", ".join(f"{BACKUP_PREFIX}{b}" for b in unpushed)
            )
        if blocked is not None:
            if not details:
                raise blocked
            return Result(
                project,
                False,
                "Partly saved: uncommitted work has secrets in the code",
                details + [str(f) for f in blocked.findings[:10]],
            )
        if not details:
            return Result(project, True, "Nothing to save")
        return Result(project, True, f"Saved to {remote}", details)

    # --------------------------------------------------------------- helpers

    def _publish_new_repo(self, project: Project, details: list[str], push_args: list[str]) -> Result:
        if self.creator is None:
            return Result(project, False, "A GitHub token is needed to create the repository", details)
        self.log("Creating private GitHub repository")
        repo = self.creator.create(project.name)
        path = project.path
        git.run(["remote", "add", "origin", repo.clone_url], path)
        self.log(f"Pushing to {repo.full_name}")
        self._push(path, push_args, repo.clone_url)
        details.append(f"Private repository: {repo.html_url}")
        return Result(project, True, f"Backed up to {repo.full_name}", details, url=repo.html_url)

    def _push(self, path: Path, args: list[str], url: str | None, force: bool = False) -> None:
        env = self.creator.push_env(url) if self.creator else {}
        options = ["push", "--no-verify", "--porcelain"] + (["--force"] if force else [])
        git.run(options + args, path, env=env, timeout=900)

    def _exclusions(self, path: Path, files: list[str]) -> list[str]:
        excluded = []
        for name in files:
            if secretscan.is_secret_filename(name) or _file_size(path / name) > MAX_FILE_BYTES:
                excluded.append(name)
        return excluded

    def _check_secrets(self, path: Path, files: list[str]) -> None:
        findings = secretscan.scan_files(path, files)
        if findings and not self.allow_secrets:
            raise BackupBlocked(findings)

    def _identity(self, path: Path) -> dict[str, str]:
        name, email = self.creator.identity() if self.creator else ("keepcode", "keepcode@localhost")
        return git.identity_env(path, name, email)

    def _save_work(self, project: Project, url: str | None) -> str | None:
        """Snapshot uncommitted work and push it to <remote>/keepcode/<branch>."""
        path = project.path
        remote = project.remote or "origin"
        label = git.current_branch(path) or "detached-HEAD"
        changes = git.status(path)
        if not changes:
            return None
        fingerprint = git.fingerprint(path, changes)
        self.log("Saving uncommitted work")
        with temp_index() as index:
            git.run(["read-tree", "HEAD"], path, env=index)
            git.run(["add", "-A"], path, env=index, timeout=600)
            changed = [
                f
                for f in git.run(
                    ["diff", "--cached", "--name-only", "-z", "--no-renames", "--diff-filter=d", "HEAD"],
                    path,
                    env=index,
                ).split("\0")
                if f
            ]
            excluded = self._exclusions(path, changed)
            if excluded:
                git.run(["reset", "-q", "HEAD", "--", *excluded], path, env=index)
            self._check_secrets(path, [f for f in changed if f not in set(excluded)])
            tree = git.run(["write-tree"], path, env=index).strip()
        head_tree = git.run(["rev-parse", "HEAD^{tree}"], path).strip()
        if tree == head_tree:
            return "Only secret or oversized files changed: nothing else to save"
        stamp = time.strftime("%Y-%m-%d %H:%M")
        commit = git.run(
            ["commit-tree", tree, "-p", "HEAD", "-m", f"keepcode: uncommitted work on {label} ({stamp})"],
            path,
            env=self._identity(path),
        ).strip()
        ref = f"{BACKUP_PREFIX}{label}"
        self._push(path, [remote, f"{commit}:refs/heads/{ref}"], url, force=True)
        self.state.record_snapshot(path, fingerprint, f"{remote}/{ref}", time.time())
        note = f"Uncommitted work saved to {remote}/{ref}"
        if excluded:
            note += f" (left out: {', '.join(excluded[:3])})"
        return note
