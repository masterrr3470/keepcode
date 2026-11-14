"""Decide how safe each project is."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from keepcode import git
from keepcode.config import State, normalize, pretty_path
from keepcode.models import Issue, IssueKind, Project, Severity
from keepcode.scanner import count_files, detect_stacks, display_name
from keepcode.secretscan import is_secret_filename


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def analyze(path: Path, state: State | None = None) -> Project:
    path = Path(path)
    project = Project(path=path, name=display_name(path), stacks=detect_stacks(path))

    if not git.is_repo(path):
        top = git.toplevel(path)
        if top is not None and _is_regular_folder(top) and normalize(top) != normalize(path):
            return analyze(top, state)  # the folder belongs to a bigger repository
        files, newest = count_files(path)
        project.file_count = files
        project.last_activity = newest
        project.issues.append(
            Issue(
                IssueKind.NO_GIT,
                Severity.CRITICAL,
                f"No version control ({_plural(files, 'file')} only on this computer)",
                files,
            )
        )
        return project

    project.is_git = True
    try:
        _analyze_repo(project, state)
    except git.GitError as exc:
        project.issues.append(Issue(IssueKind.GIT_ERROR, Severity.WARNING, f"Could not read repository: {exc}"))
    return project


def _is_regular_folder(path: Path) -> bool:
    """False for the home folder and drive roots, where dotfile repositories live."""
    return normalize(path) != normalize(Path.home()) and path.parent != path


def _analyze_repo(project: Project, state: State | None) -> None:
    path = project.path
    project.branch = git.current_branch(path)
    commits = git.has_commits(path)
    project.remote = git.preferred_remote(path)
    project.remote_url = git.remote_url(path, project.remote) if project.remote else None

    changes = git.status(path)
    newest_change = _newest_mtime(path, (c.path for c in changes[:500]))
    last_commit = git.last_commit_time(path) if commits else None
    project.last_activity = max(filter(None, [last_commit, newest_change]), default=None)

    if not commits:
        files, newest = count_files(path)
        project.file_count = files
        project.last_activity = project.last_activity or newest
        project.issues.append(
            Issue(
                IssueKind.NO_COMMITS,
                Severity.CRITICAL,
                f"Git repository with no commits ({_plural(files, 'file')} not saved)",
                files,
            )
        )
        return

    if project.remote is None:
        project.issues.append(
            Issue(
                IssueKind.NO_REMOTE,
                Severity.CRITICAL,
                "No remote: commits only exist on this computer",
            )
        )

    if changes:
        issue = Issue(
            IssueKind.UNCOMMITTED,
            Severity.WARNING,
            f"{_plural(len(changes), 'uncommitted change')}",
            len(changes),
        )
        snapshot = state.snapshot_for(path) if state else None
        if snapshot and snapshot.get("fingerprint") == git.fingerprint(path, changes):
            issue.severity = Severity.INFO
            issue.message += " (backed up by keepcode)"
        project.issues.append(issue)

    if project.remote is not None:
        unpushed = git.unpushed_total(path)
        if unpushed:
            project.issues.append(
                Issue(
                    IssueKind.UNPUSHED,
                    Severity.WARNING,
                    f"{_plural(unpushed, 'commit')} not pushed",
                    unpushed,
                )
            )

    stashes = git.stash_count(path)
    if stashes:
        project.issues.append(
            Issue(
                IssueKind.STASHED,
                Severity.INFO,
                f"{_plural(stashes, 'stash')} kept only locally",
                stashes,
            )
        )

    secrets = [name for name in git.tracked_files(path) if is_secret_filename(name)]
    if secrets:
        shown = ", ".join(secrets[:3]) + ("..." if len(secrets) > 3 else "")
        project.issues.append(
            Issue(
                IssueKind.TRACKED_SECRET,
                Severity.WARNING,
                f"Secret file tracked in git: {shown}",
                len(secrets),
            )
        )


def _newest_mtime(root: Path, relative_paths: Iterable[str]) -> float | None:
    newest = None
    for relative in relative_paths:
        try:
            mtime = os.stat(root / relative).st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    return newest


def analyze_many(
    paths: list[Path],
    state: State | None = None,
    on_done: Callable[[int], None] | None = None,
) -> list[Project]:
    workers = max(2, min(8, (os.cpu_count() or 4)))
    results: list[Project] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index, project in enumerate(pool.map(lambda p: analyze(p, state), paths), start=1):
            results.append(project)
            if on_done:
                on_done(index)
    return mark_copies(results)


_COPY_SUFFIX = re.compile(r"(\s*\(\d+\)|\s*-\s*(copy|copia|cópia)(\s*\d+)?)$", re.IGNORECASE)


def _base_name(name: str) -> str:
    return _COPY_SUFFIX.sub("", name.strip()).lower()


def mark_copies(projects: list[Project]) -> list[Project]:
    """Flag folders without git that look like copies of each other.

    Two folders are copies when they have the same name (ignoring " (1)" or
    " - copy") and the same number of files. The most recently changed one is
    treated as the original.
    """
    groups: dict[tuple[str, int], list[Project]] = {}
    for project in projects:
        if project.has(IssueKind.NO_GIT) and project.file_count:
            groups.setdefault((_base_name(project.name), project.file_count), []).append(project)
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda p: (-(p.last_activity or 0), len(str(p.path))))
        original = group[0]
        for copy in group[1:]:
            copy.copy_of = original.path
            copy.issues.append(
                Issue(IssueKind.DUPLICATE, Severity.INFO, f"Looks like a copy of {pretty_path(original.path)}")
            )
    return projects


def sort_projects(projects: list[Project]) -> list[Project]:
    return sorted(projects, key=lambda p: (-p.severity, -(p.last_activity or 0), p.name.lower()))
