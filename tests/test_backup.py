from pathlib import Path

from conftest import commit_all, new_repo, sh, write

from keepcode.analysis import analyze
from keepcode.backup import Engine, make_plan
from keepcode.config import State
from keepcode.models import IssueKind, Severity

TOKEN = "ghp_" + "a" * 36


def remote_files(bare_repo: Path, ref: str) -> set[str]:
    return set(sh(bare_repo, "ls-tree", "-r", "--name-only", ref).split())


def pushed_repo(home: Path, bare, name: str = "p") -> tuple[Path, Path]:
    remote = bare(name)
    repo = new_repo(home / name, {"app.py": "print(1)\n"})
    sh(repo, "remote", "add", "origin", str(remote))
    sh(repo, "push", "-q", "-u", "origin", "main")
    return repo, remote


def test_folder_without_git_becomes_a_private_repository(home: Path, creator):
    project_dir = home / "shop"
    write(project_dir / "manage.py", "")
    write(project_dir / "requirements.txt", "django\n")
    write(project_dir / ".env", "SECRET=1\n")
    write(project_dir / "node_modules" / "lib.js")
    write(project_dir / "app" / "views.py", "x = 1\n")

    project = analyze(project_dir)
    plan = make_plan(project)
    assert plan.creates_repo

    result = Engine(creator, State()).run(project)
    assert result.ok, result.summary
    remote = creator.created[0]
    files = remote_files(remote, "main")
    assert files == {".gitignore", "manage.py", "requirements.txt", "app/views.py"}
    # The local folder is now a clean repository tracking the new remote.
    assert sh(project_dir, "status", "--porcelain") == ""
    assert analyze(project_dir).severity == Severity.OK
    assert (project_dir / ".env").read_text() == "SECRET=1\n"
    assert "Kept out (secret files): .env" in result.details


def test_hardcoded_secret_blocks_and_rolls_back(home: Path, creator):
    project_dir = home / "bot"
    write(project_dir / "package.json", "{}")
    write(project_dir / "index.js", f"client.login('{TOKEN}')\n")
    result = Engine(creator, State()).run(analyze(project_dir))
    assert not result.ok
    assert "secrets" in result.summary.lower()
    assert any("index.js:1" in line for line in result.details)
    assert TOKEN not in "\n".join(result.details)
    assert not (project_dir / ".git").exists()
    assert not (project_dir / ".gitignore").exists()
    assert creator.created == []


def test_rollback_restores_an_existing_gitignore(home: Path, creator):
    project_dir = home / "bot"
    write(project_dir / ".gitignore", "custom\n")
    write(project_dir / "package.json", "{}")
    write(project_dir / "index.js", f"t = '{TOKEN}'\n")
    Engine(creator, State()).run(analyze(project_dir))
    assert (project_dir / ".gitignore").read_text() == "custom\n"


def test_allow_secrets_and_allow_marker(home: Path, creator):
    first = home / "a"
    write(first / "package.json", "{}")
    write(first / "index.js", f"t = '{TOKEN}'\n")
    assert Engine(creator, State(), allow_secrets=True).run(analyze(first)).ok

    second = home / "b"
    write(second / "package.json", "{}")
    write(second / "index.js", f"t = '{TOKEN}'  // keepcode:allow\n")
    assert Engine(creator, State()).run(analyze(second)).ok


def test_large_files_are_left_out(home: Path, creator, monkeypatch):
    import keepcode.backup as backup

    monkeypatch.setattr(backup, "MAX_FILE_BYTES", 1000)
    project_dir = home / "data"
    write(project_dir / "pyproject.toml", "")
    write(project_dir / "big.bin", "0" * 5000)
    result = Engine(creator, State()).run(analyze(project_dir))
    assert result.ok
    assert any("big.bin" in d for d in result.details)
    assert "big.bin" not in remote_files(creator.created[0], "main")
    assert "/big.bin" in (project_dir / ".gitignore").read_text()


def test_repository_without_commits_gets_first_commit(home: Path, creator):
    project_dir = home / "empty"
    project_dir.mkdir()
    sh(project_dir, "init", "-q")
    write(project_dir / "main.py")
    result = Engine(creator, State()).run(analyze(project_dir))
    assert result.ok, result.summary
    assert "main.py" in remote_files(creator.created[0], "main")


def test_repository_without_remote_pushes_branches_and_tags(home: Path, creator):
    repo = new_repo(home / "local")
    sh(repo, "tag", "v1")
    sh(repo, "checkout", "-q", "-b", "feature")
    write(repo / "f.py")
    commit_all(repo, "feature")
    sh(repo, "checkout", "-q", "main")
    result = Engine(creator, State()).run(analyze(repo))
    assert result.ok, result.summary
    remote = creator.created[0]
    refs = sh(remote, "for-each-ref", "--format=%(refname)").split()
    assert {"refs/heads/main", "refs/heads/feature", "refs/tags/v1"} <= set(refs)
    assert analyze(repo).severity == Severity.OK


def test_uncommitted_work_goes_to_a_backup_branch_without_touching_anything(home: Path, bare, creator):
    repo, remote = pushed_repo(home, bare)
    write(repo / "app.py", "print(2)\n")
    write(repo / "new.py", "y = 2\n")
    write(repo / "staged.py", "z = 3\n")
    sh(repo, "add", "staged.py")
    write(repo / ".env", "KEY=1\n")
    before_status = sh(repo, "status", "--porcelain")
    before_head = sh(repo, "rev-parse", "HEAD")

    state = State()
    result = Engine(creator, state).run(analyze(repo))
    assert result.ok, result.summary

    # Nothing changed locally: same files, same staging area, same branch.
    assert sh(repo, "status", "--porcelain") == before_status
    assert sh(repo, "rev-parse", "HEAD") == before_head
    assert (repo / "app.py").read_text() == "print(2)\n"
    # The real branch on the remote is untouched; the work is on keepcode/main.
    assert sh(remote, "rev-parse", "main") == before_head
    files = remote_files(remote, "keepcode/main")
    assert {"app.py", "new.py", "staged.py"} <= files
    assert ".env" not in files
    assert sh(remote, "show", "keepcode/main:app.py") == "print(2)\n"
    # The next scan knows the work is saved.
    after = analyze(repo, state)
    assert after.get(IssueKind.UNCOMMITTED).severity == Severity.INFO
    assert not after.fixable


def test_unpushed_commits_are_copied_not_pushed(home: Path, bare, creator):
    repo, remote = pushed_repo(home, bare)
    main_on_remote = sh(remote, "rev-parse", "main")
    write(repo / "b.py")
    commit_all(repo, "local commit")
    sh(repo, "checkout", "-q", "-b", "feature")
    write(repo / "c.py")
    commit_all(repo, "feature commit")
    sh(repo, "checkout", "-q", "main")

    result = Engine(creator, State()).run(analyze(repo))
    assert result.ok, result.summary
    assert sh(remote, "rev-parse", "main") == main_on_remote
    assert sh(remote, "rev-parse", "keepcode/main") == sh(repo, "rev-parse", "main")
    assert sh(remote, "rev-parse", "keepcode/feature") == sh(repo, "rev-parse", "feature")
    assert analyze(repo).get(IssueKind.UNPUSHED) is None


def test_secret_in_uncommitted_work_still_saves_commits(home: Path, bare, creator):
    repo, remote = pushed_repo(home, bare)
    write(repo / "b.py")
    commit_all(repo, "local commit")
    sh(repo, "checkout", "-q", "-b", "feature")
    write(repo / "c.py")
    commit_all(repo, "feature commit")
    sh(repo, "checkout", "-q", "main")
    write(repo / "config.py", f"KEY = '{TOKEN}'\n")

    result = Engine(creator, State()).run(analyze(repo))
    assert not result.ok
    assert result.summary.startswith("Partly saved")
    refs = sh(remote, "for-each-ref", "--format=%(refname)").split()
    assert "refs/heads/keepcode/main" in refs
    assert "config.py" not in remote_files(remote, "keepcode/main")


def test_detached_head_snapshot(home: Path, bare, creator):
    repo, remote = pushed_repo(home, bare)
    sh(repo, "checkout", "-q", "--detach")
    write(repo / "wip.py")
    result = Engine(creator, State()).run(analyze(repo))
    assert result.ok, result.summary
    assert "wip.py" in remote_files(remote, "keepcode/detached-HEAD")


def test_token_never_reaches_git_config(home: Path):
    from keepcode.github import push_env

    env = push_env("secret-token-value")
    assert "secret-token-value" not in "".join(env.values())
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"


def test_no_creator_means_no_local_changes(home: Path):
    project_dir = home / "p"
    write(project_dir / "main.py")
    project = analyze(project_dir)
    result = Engine(None, State()).run(project)
    assert not result.ok
    assert "token" in result.summary
    assert not (project_dir / ".git").exists()
    assert not (project_dir / ".gitignore").exists()
