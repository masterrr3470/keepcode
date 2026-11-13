"""Platform glue: covered with pure functions so it is tested on every OS."""

import sys
from pathlib import Path

import pytest

from keepcode import notify, schedule


def test_windows_toast_script_escapes_text():
    script = notify.windows_script("Title & <stuff>", "It's Jorge's project")
    assert "Title &amp; &lt;stuff&gt;" in script
    assert "It''s Jorge''s project" in script  # quotes doubled inside the PowerShell string
    assert notify.POWERSHELL_APP_ID in script


@pytest.mark.parametrize("value", ["18:00", "7:05", "23:59", "00:00"])
def test_parse_time_valid(value):
    hour, minute = schedule.parse_time(value)
    assert 0 <= hour <= 23 and 0 <= minute <= 59


@pytest.mark.parametrize("value", ["24:00", "18", "6pm", "12:60", ""])
def test_parse_time_invalid(value):
    with pytest.raises(schedule.ScheduleError):
        schedule.parse_time(value)


def test_cron_line_runs_check_quietly():
    line = schedule.cron_line(18, 30)
    assert line.startswith("30 18 * * * ")
    assert "-m keepcode check --notify --quiet" in line
    assert line.endswith(schedule.CRON_MARKER)


def test_windows_task_uses_a_launcher_when_the_path_is_too_long(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(schedule, "command", lambda: ["C:\\" + "x" * 300 + "\\pythonw.exe", "-m", "keepcode", "check"])
    task = schedule._windows_task_command()
    assert task.endswith('keepcode-check.cmd"')
    launcher = Path(task.strip('"'))
    assert "-m keepcode check" in launcher.read_text(encoding="utf-8")


def test_short_windows_task_command(monkeypatch):
    monkeypatch.setattr(schedule, "command", lambda: ["C:\\Python\\pythonw.exe", "-m", "keepcode", "check"])
    assert schedule._windows_task_command() == '"C:\\Python\\pythonw.exe" -m keepcode check'


@pytest.mark.skipif(sys.platform == "win32", reason="crontab is only used on macOS and Linux")
def test_cron_enable_and_disable(monkeypatch):
    table: list[str] = ["0 1 * * * other-job"]
    monkeypatch.setattr(schedule, "_read_crontab", lambda: list(table))

    def write(lines):
        table[:] = lines

    monkeypatch.setattr(schedule, "_write_crontab", write)
    schedule.enable("08:15")
    assert table[0] == "0 1 * * * other-job"
    assert schedule.status().enabled
    schedule.enable("09:00")  # replaces, never duplicates
    assert sum(schedule.CRON_MARKER in line for line in table) == 1
    assert schedule.disable()
    assert table == ["0 1 * * * other-job"]
    assert not schedule.status().enabled
