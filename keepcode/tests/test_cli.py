import json
from pathlib import Path

import pytest
from conftest import new_repo, write
from typer.testing import CliRunner

from keepcode import __version__
from keepcode import cli as cli_module
from keepcode.cli import app
from keepcode.config import Config, State

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, list(args), catch_exceptions=False)


def test_version():
    result = invoke("--version")
    assert result.exit_code == 0
    assert __version__ in result.output


def test_scan_json_lists_projects(home: Path):
    write(home / "Documents" / "shop" / "manage.py")
    new_repo(home / "Desktop" / "bot", {"package.json": "{}"})
    result = invoke("scan", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    by_name = {p["name"]: p for p in data["projects"]}
    assert by_name["shop"]["status"] == "not backed up"
    assert by_name["bot"]["issues"][0]["kind"] == "no_remote"
    assert data["folders_scanned"] > 0


def test_running_without_command_scans(home: Path):
    write(home / "code" / "app" / "pyproject.toml")
    result = invoke()
    assert result.exit_code == 0
    assert "app" in result.output
    assert "keepcode backup" in result.output


def test_empty_home_has_a_helpful_message(home: Path):
    result = invoke("scan")
    assert "No projects found" in result.output


def test_check_exit_code_and_state(home: Path):
    assert invoke("check", "--quiet").exit_code == 0
    write(home / "code" / "app" / "pyproject.toml")
    assert invoke("check", "--quiet").exit_code == 1
    last = State.load().data["last_check"]
    assert last["at_risk"] == 1 and last["total"] == 1


def test_backup_dry_run_changes_nothing(home: Path):
    project = home / "code" / "app"
    write(project / "pyproject.toml")
    result = invoke("backup", str(project), "--dry-run")
    assert result.exit_code == 0
    assert "Create a private GitHub repository" in result.output
    assert not (project / ".git").exists()


def test_backup_without_token_explains_login(home: Path):
    project = home / "code" / "app"
    write(project / "pyproject.toml")
    result = runner.invoke(app, ["backup", str(project), "--yes"])
    assert result.exit_code == 2
    assert "keepcode login" in result.output
    assert not (project / ".git").exists()


def test_backup_end_to_end(home: Path, creator, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli_module, "github_creator", lambda required: creator)
    project = home / "code" / "app"
    write(project / "pyproject.toml")
    write(project / "main.py", "print('hi')\n")
    result = invoke("backup", str(project), "--yes")
    assert result.exit_code == 0, result.output
    assert "Backed up to me/app" in result.output
    assert invoke("backup", str(project), "--yes").output.count("nothing to back up") == 1


def test_backup_all_from_scan(home: Path, creator, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli_module, "github_creator", lambda required: creator)
    write(home / "code" / "one" / "pyproject.toml")
    write(home / "code" / "two" / "package.json", "{}")
    result = invoke("backup", "--all", "--yes")
    assert result.exit_code == 0, result.output
    assert "2 backed up" in result.output
    assert "Everything is backed up" in invoke("backup", "--all", "--yes").output


def test_backup_needs_yes_when_not_interactive(home: Path, creator, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli_module, "github_creator", lambda required: creator)
    write(home / "code" / "one" / "pyproject.toml")
    result = runner.invoke(app, ["backup", str(home / "code" / "one")])
    assert result.exit_code == 2
    assert "--yes" in result.output


def test_roots_and_ignore_commands(home: Path):
    work = home / "work"
    work.mkdir()
    assert invoke("roots", "add", str(work)).exit_code == 0
    assert str(work) in invoke("roots", "list").output
    assert invoke("roots", "remove", str(work)).exit_code == 0
    assert invoke("roots", "remove", str(home)).exit_code == 1  # cannot remove the last root
    assert invoke("ignore", "add", str(work)).exit_code == 0
    assert Config.load().ignore == [str(work.resolve())]
    assert invoke("ignore", "remove", str(work)).exit_code == 0
    assert invoke("roots", "add", str(home / "missing")).exit_code == 2


def test_ignored_folder_is_not_scanned(home: Path):
    write(home / "private" / "app" / "package.json", "{}")
    invoke("ignore", "add", str(home / "private"))
    data = json.loads(invoke("scan", "--json").output)
    assert data["projects"] == []


def test_config_command(home: Path):
    result = invoke("config", "--depth", "4", "--auto-backup")
    assert result.exit_code == 0
    config = Config.load()
    assert config.max_depth == 4 and config.auto_backup is True


def test_auto_backup_in_check_saves_work(home: Path, bare, creator, monkeypatch: pytest.MonkeyPatch):
    from conftest import sh

    monkeypatch.setattr(cli_module, "github_creator", lambda required: creator)
    remote = bare("app")
    repo = new_repo(home / "code" / "app")
    sh(repo, "remote", "add", "origin", str(remote))
    sh(repo, "push", "-q", "-u", "origin", "main")
    write(repo / "wip.py")
    invoke("config", "--auto-backup")
    assert invoke("check", "--quiet").exit_code == 0
    assert "keepcode/main" in sh(remote, "branch", "--list", "keepcode/*")


def test_doctor_runs(home: Path):
    result = invoke("doctor")
    assert result.exit_code == 0
    assert "git" in result.output and "GitHub" in result.output


def test_invalid_schedule_time(home: Path):
    result = invoke("schedule", "on", "--time", "25:99")
    assert result.exit_code == 2
    assert "HH:MM" in result.output


def test_choose_accepts_numbers_after_a_bad_answer(home: Path, monkeypatch: pytest.MonkeyPatch):
    from keepcode.analysis import analyze

    write(home / "a" / "pyproject.toml")
    write(home / "b" / "pyproject.toml")
    projects = [analyze(home / "a"), analyze(home / "b")]
    answers = iter(["banana", "9", "2, 1"])
    monkeypatch.setattr(cli_module.Prompt, "ask", lambda *args, **kwargs: next(answers))
    chosen = cli_module.choose(projects)
    assert [p.name for p in chosen] == ["a", "b"]


def test_time_ago_and_short_path(home: Path):
    import time

    assert cli_module.time_ago(None) == "unknown"
    assert cli_module.time_ago(time.time()) == "just now"
    assert cli_module.time_ago(time.time() - 3 * 86400) == "3 days ago"
    assert cli_module.short_path(home / "code" / "x").startswith("~")
