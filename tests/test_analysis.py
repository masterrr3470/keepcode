from pathlib import Path

from conftest import commit_all, new_repo, sh, write

from keepcode.analysis import analyze, sort_projects
from keepcode.config import State
from keepcode.models import IssueKind, Severity


def kinds(project):
    return {issue.kind for issue in project.issues}


def test_folder_without_git_is_critical(home: Path):
    write(home / "p" / "main.py")
    write(home / "p" / "node_modules" / "x.js")
    project = analyze(home / "p")
    assert kinds(project) == {IssueKind.NO_GIT}
    assert project.severity == Severity.CRITICAL
    assert project.file_count == 1
    assert project.fixable


def test_repository_without_commits(home: Path):
    (home / "p").mkdir()
    sh(home / "p", "init", "-q")
    write(home / "p" / "a.py")
    project = analyze(home / "p")
    assert kinds(project) == {IssueKind.NO_COMMITS}


def test_repository_without_remote(home: Path):
    repo = new_repo(home / "p")
    project = analyze(repo)
    assert kinds(project) == {IssueKind.NO_REMOTE}
    assert project.branch == "main"


def test_safe_repository(home: Path, bare):
    remote = bare("p")
    repo = new_repo(home / "p")
    sh(repo, "remote", "add", "origin", str(remote))
    sh(repo, "push", "-q", "-u", "origin", "main")
    project = analyze(repo)
    assert project.issues == []
    assert project.severity == Severity.OK
    assert not project.fixable


def test_uncommitted_unpushed_and_stash(home: Path, bare):
    remote = bare("p")
    repo = new_repo(home / "p")
    sh(repo, "remote", "add", "origin", str(remote))
    sh(repo, "push", "-q", "-u", "origin", "main")
    write(repo / "b.txt")
    commit_all(repo, "local")
    write(repo / "stash.txt")
    sh(repo, "stash", "-u", "-q")
    write(repo / "wip.txt")
    project = analyze(repo)
    assert project.get(IssueKind.UNPUSHED).count == 1
    assert project.get(IssueKind.UNCOMMITTED).count == 1
    assert project.get(IssueKind.STASHED).severity == Severity.INFO
    assert project.severity == Severity.WARNING


def test_new_branch_never_pushed_counts_as_unpushed(home: Path, bare):
    remote = bare("p")
    repo = new_repo(home / "p")
    sh(repo, "remote", "add", "origin", str(remote))
    sh(repo, "push", "-q", "-u", "origin", "main")
    sh(repo, "checkout", "-q", "-b", "feature")
    write(repo / "f.txt")
    commit_all(repo, "feature work")
    assert analyze(repo).get(IssueKind.UNPUSHED).count == 1


def test_tracked_secret_file_is_reported(home: Path):
    repo = new_repo(home / "p", {".env": "KEY=1\n", "app.py": "x\n"})
    issue = analyze(repo).get(IssueKind.TRACKED_SECRET)
    assert issue is not None and ".env" in issue.message


def test_subfolder_of_a_repository_is_analyzed_as_the_repository(home: Path):
    repo = new_repo(home / "mono", {"backend/manage.py": "", "README.md": "x"})
    project = analyze(repo / "backend")
    assert project.path.samefile(repo)
    assert IssueKind.NO_GIT not in kinds(project)


def test_snapshot_fingerprint_lowers_severity(home: Path, bare):
    from keepcode import git

    remote = bare("p")
    repo = new_repo(home / "p")
    sh(repo, "remote", "add", "origin", str(remote))
    sh(repo, "push", "-q", "-u", "origin", "main")
    write(repo / "wip.txt")
    state = State()
    state.record_snapshot(repo, git.fingerprint(repo, git.status(repo)), "origin/keepcode/main", 0)
    project = analyze(repo, state)
    assert project.get(IssueKind.UNCOMMITTED).severity == Severity.INFO
    assert not project.fixable
    write(repo / "wip.txt", "changed again, longer content\n")
    assert analyze(repo, state).get(IssueKind.UNCOMMITTED).severity == Severity.WARNING


def test_sort_puts_worst_first(home: Path):
    write(home / "nogit" / "a.py")
    safe = new_repo(home / "norem")
    projects = sort_projects([analyze(safe), analyze(home / "nogit")])
    assert projects[0].severity >= projects[1].severity
