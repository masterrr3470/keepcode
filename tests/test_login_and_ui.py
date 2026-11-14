import io
import sys
import time

import pytest
from rich.console import Console
from typer.testing import CliRunner

from keepcode import cli as cli_module
from keepcode import github as gh
from keepcode import schedule, ui, updater
from keepcode.cli import app

runner = CliRunner()


# ---------------------------------------------------------------- device flow


def test_client_id_can_be_overridden(monkeypatch):
    monkeypatch.setenv("KEEPCODE_CLIENT_ID", "Ov23liTest")
    assert gh.client_id() == "Ov23liTest"


def test_start_device_flow():
    def post(url, data):
        assert data == {"client_id": "app", "scope": "repo"}
        return {
            "device_code": "d",
            "user_code": "ABCD-1234",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }

    code = gh.start_device_flow("app", post)
    assert code.user_code == "ABCD-1234" and code.interval == 5


def test_start_device_flow_reports_errors():
    with pytest.raises(gh.GitHubError, match="turned off"):
        gh.start_device_flow("app", lambda url, data: {"error": "device_flow_disabled"})


def poller(answers):
    calls = []

    def post(url, data):
        calls.append(data)
        return answers.pop(0)

    return post, calls


def test_wait_for_token_handles_pending_and_slow_down():
    code = gh.DeviceCode("d", "ABCD-1234", "https://github.com/login/device", 900, 5)
    post, _ = poller(
        [{"error": "authorization_pending"}, {"error": "slow_down", "interval": 10}, {"access_token": "gho_abc"}]
    )
    waits = []
    assert gh.wait_for_token("app", code, post, sleep=waits.append, clock=lambda: 0) == "gho_abc"
    assert waits == [5, 5, 10]


@pytest.mark.parametrize(("error", "message"), [("access_denied", "cancelled"), ("expired_token", "expired")])
def test_wait_for_token_failures(error, message):
    code = gh.DeviceCode("d", "X", "u", 900, 1)
    post, _ = poller([{"error": error}])
    with pytest.raises(gh.GitHubError, match=message):
        gh.wait_for_token("app", code, post, sleep=lambda s: None, clock=lambda: 0)


class FakeClient:
    def __init__(self, token):
        self.token = token

    def user(self):
        return {"login": "masterrr3470"}


def test_login_with_the_browser(monkeypatch):
    saved = []
    monkeypatch.setenv("KEEPCODE_CLIENT_ID", "Ov23liTest")
    monkeypatch.setattr(
        gh,
        "start_device_flow",
        lambda app_id: gh.DeviceCode("d", "WXYZ-9876", "https://github.com/login/device", 900, 5),
    )
    monkeypatch.setattr(gh, "wait_for_token", lambda app_id, code: "gho_token")
    monkeypatch.setattr(gh, "open_browser", lambda url: True)
    monkeypatch.setattr(gh, "copy_to_clipboard", lambda text: True)
    monkeypatch.setattr(gh, "GitHub", FakeClient)
    monkeypatch.setattr(gh, "save_token", saved.append)
    result = runner.invoke(app, ["login"])
    assert result.exit_code == 0, result.output
    assert "WXYZ-9876" in result.output and "masterrr3470" in result.output
    assert saved == ["gho_token"]


def test_login_without_app_falls_back_to_pasting(monkeypatch):
    saved = []
    monkeypatch.setattr(gh, "DEFAULT_CLIENT_ID", "")
    monkeypatch.setattr(gh, "open_browser", lambda url: False)
    monkeypatch.setattr(cli_module.console, "input", lambda prompt="": "  ghp_pasted  ")
    monkeypatch.setattr(gh, "GitHub", FakeClient)
    monkeypatch.setattr(gh, "save_token", saved.append)
    result = runner.invoke(app, ["login"])
    assert result.exit_code == 0, result.output
    assert saved == ["ghp_pasted"]


# ----------------------------------------------------------------- updates


def test_update_command_for_pip_installs(monkeypatch):
    monkeypatch.setattr(updater, "latest_release", lambda timeout=3: updater.Release("99.0.0", "", {}))
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert "99.0.0" in result.output and "pip" in result.output


def test_update_when_current(monkeypatch):
    monkeypatch.setattr(updater, "latest_release", lambda timeout=3: updater.Release(cli_module.__version__, "", {}))
    assert "newest version" in runner.invoke(app, ["update"]).output


def test_frozen_update_restarts(monkeypatch, tmp_path):
    assets = {updater.WINDOWS_ASSET: "u", updater.CHECKSUMS_ASSET: "s"}
    monkeypatch.setattr(updater, "latest_release", lambda timeout=3: updater.Release("99.0.0", "", assets))
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(cli_module.Confirm, "ask", lambda *a, **k: True)
    monkeypatch.setattr(cli_module, "install_update", lambda release: tmp_path / "keepcode.exe")
    monkeypatch.setattr(updater, "restart", lambda target, args: 0)
    code, note = cli_module.check_updates(cli_module.Config(), [])
    assert code == 0 and note is None


def test_auto_update_setting(home):
    assert runner.invoke(app, ["config", "--auto-update", "off"]).exit_code == 0
    assert cli_module.Config.load().auto_update == "off"
    assert runner.invoke(app, ["config", "--auto-update", "sometimes"]).exit_code == 2


# ---------------------------------------------------------------- schedule


def test_frozen_schedule_runs_the_exe_without_a_window(monkeypatch):
    monkeypatch.setattr(schedule, "is_frozen", lambda: True)
    monkeypatch.setattr(
        schedule, "executable", lambda: "C:\\Users\\jorge\\AppData\\Local\\Programs\\keepcode\\keepcode.exe"
    )
    monkeypatch.setattr(schedule, "_headless_console", lambda: "C:\\Windows\\System32\\conhost.exe")
    line = schedule._windows_task_command()
    assert line.startswith('"C:\\Windows\\System32\\conhost.exe" --headless "C:\\Users\\jorge')
    assert line.endswith('keepcode.exe" check --notify --quiet')


@pytest.mark.skipif(sys.platform == "win32", reason="on Windows the answer depends on the console")
def test_double_click_detection_is_windows_only():
    assert cli_module.launched_by_double_click() is False


# ------------------------------------------------------------------- grid


def test_grid_slots_are_stable_and_unique():
    grid = ui.Grid.for_width(100)
    assert grid.cols == 47 and grid.size == grid.cols * grid.rows
    slots = {grid.slot(f"project-{i}") for i in range(50)}
    assert len(slots) == 50
    assert grid.slot("project-3") == grid.slot("project-3")


def test_grid_renders_project_colours():
    grid = ui.Grid(cols=12)
    grid.show("a", ui.RED, 0.0)
    text = grid.render(time.monotonic())
    assert text.plain.count(ui.CELL) == 12 * 5
    assert any(span.style == ui.RED for span in text.spans)


def test_run_with_animation_returns_and_raises():
    console = Console(file=io.StringIO(), force_terminal=False)
    view = ui.View(ui.Grid(cols=12), 40)
    assert ui.run_with_animation(console, view, lambda: 42, lambda t: None) == 42
    with pytest.raises(ValueError):
        ui.run_with_animation(console, view, lambda: (_ for _ in ()).throw(ValueError("x")), lambda t: None)


def test_animations_can_be_turned_off(monkeypatch):
    console = Console(force_terminal=True)
    monkeypatch.setenv("KEEPCODE_NO_ANIMATION", "1")
    assert ui.animations_enabled(console) is False
    monkeypatch.delenv("KEEPCODE_NO_ANIMATION")
    assert ui.animations_enabled(console) is True


def test_keepcode_ships_with_its_own_oauth_app():
    """The browser login works out of the box; the Client ID is public, not a secret."""
    assert gh.DEFAULT_CLIENT_ID.startswith("Ov23")
