"""Run `keepcode check` once a day.

Windows uses the Task Scheduler (no administrator rights needed). macOS and
Linux use the user's crontab.
"""

from __future__ import annotations

import csv
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from keepcode.config import config_dir
from keepcode.updater import executable, is_frozen

TASK_NAME = "keepcode daily check"
CRON_MARKER = "# keepcode daily check"
TIME_FORMAT = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class ScheduleError(RuntimeError):
    pass


@dataclass
class ScheduleStatus:
    enabled: bool
    detail: str


def parse_time(value: str) -> tuple[int, int]:
    match = TIME_FORMAT.match(value.strip())
    if not match:
        raise ScheduleError(f"Invalid time '{value}'. Use HH:MM, for example 18:30.")
    return int(match.group(1)), int(match.group(2))


def _python_for_background() -> str:
    """On Windows, pythonw.exe runs without opening a console window."""
    executable = Path(sys.executable)
    if sys.platform == "win32":
        candidate = executable.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(executable)


def command() -> list[str]:
    if is_frozen():
        return [str(executable()), "check", "--notify", "--quiet"]
    return [_python_for_background(), "-m", "keepcode", "check", "--notify", "--quiet"]


def _headless_console() -> str | None:
    """conhost.exe --headless runs a console program without showing a window.

    Available on Windows 10 2004 (build 19041) and later. The standalone
    executable is a console program, so without this a window would flash
    every day when the check runs.
    """
    if sys.platform != "win32" or not is_frozen():
        return None
    try:
        if sys.getwindowsversion().build < 19041:  # type: ignore[attr-defined]
            return None
    except AttributeError:
        return None
    conhost = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "conhost.exe"
    return str(conhost) if conhost.exists() else None


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScheduleError(str(exc)) from exc


# ------------------------------------------------------------------ Windows


def _windows_task_command() -> str:
    parts = command()
    host = _headless_console()
    line = f'"{parts[0]}" ' + " ".join(parts[1:])
    if host:
        line = f'"{host}" --headless {line}'
    if len(line) <= 260:
        return line
    # schtasks limits /TR to 261 characters: fall back to a small launcher script.
    launcher = config_dir() / "keepcode-check.cmd"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(f'@echo off\r\n"{parts[0]}" {" ".join(parts[1:])}\r\n', encoding="utf-8")
    if host:
        return f'"{host}" --headless cmd.exe /c "{launcher}"'
    return f'"{launcher}"'


def _windows_enable(hour: int, minute: int) -> str:
    result = _run(
        [
            "schtasks",
            "/Create",
            "/F",
            "/SC",
            "DAILY",
            "/ST",
            f"{hour:02d}:{minute:02d}",
            "/TN",
            TASK_NAME,
            "/TR",
            _windows_task_command(),
        ]
    )
    if result.returncode != 0:
        raise ScheduleError((result.stderr or result.stdout).strip() or "schtasks failed")
    return f"Windows Task Scheduler: every day at {hour:02d}:{minute:02d}"


def _windows_disable() -> bool:
    result = _run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
    return result.returncode == 0


def _windows_status() -> ScheduleStatus:
    # CSV without headers: TaskName, Next Run Time, Status (column names are localized).
    result = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "CSV", "/NH"])
    if result.returncode != 0:
        return ScheduleStatus(False, "No daily check scheduled")
    rows = list(csv.reader(line for line in result.stdout.splitlines() if line.strip()))
    next_run = rows[0][1] if rows and len(rows[0]) > 1 else "unknown"
    return ScheduleStatus(True, f"Scheduled in Windows Task Scheduler (next run: {next_run})")


# -------------------------------------------------------------- macOS/Linux


def _read_crontab() -> list[str]:
    result = _run(["crontab", "-l"])
    if result.returncode != 0:
        return []  # no crontab yet
    return result.stdout.splitlines()


def _write_crontab(lines: list[str]) -> None:
    try:
        result = subprocess.run(
            ["crontab", "-"], input="\n".join(lines) + "\n", capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScheduleError(f"crontab is not available: {exc}") from exc
    if result.returncode != 0:
        raise ScheduleError(result.stderr.strip() or "crontab failed")


def cron_line(hour: int, minute: int) -> str:
    parts = command()
    quoted = " ".join(f'"{p}"' if " " in p else p for p in parts)
    return f"{minute} {hour} * * * {quoted} {CRON_MARKER}"


def _cron_enable(hour: int, minute: int) -> str:
    lines = [line for line in _read_crontab() if CRON_MARKER not in line]
    lines.append(cron_line(hour, minute))
    _write_crontab(lines)
    return f"crontab: every day at {hour:02d}:{minute:02d}"


def _cron_disable() -> bool:
    lines = _read_crontab()
    kept = [line for line in lines if CRON_MARKER not in line]
    if len(kept) == len(lines):
        return False
    _write_crontab(kept)
    return True


def _cron_status() -> ScheduleStatus:
    line = next((line for line in _read_crontab() if CRON_MARKER in line), None)
    if line is None:
        return ScheduleStatus(False, "No daily check scheduled")
    minute, hour = line.split()[:2]
    return ScheduleStatus(True, f"Scheduled in crontab every day at {int(hour):02d}:{int(minute):02d}")


# ------------------------------------------------------------------ public


def enable(time_text: str) -> str:
    hour, minute = parse_time(time_text)
    return _windows_enable(hour, minute) if sys.platform == "win32" else _cron_enable(hour, minute)


def disable() -> bool:
    return _windows_disable() if sys.platform == "win32" else _cron_disable()


def status() -> ScheduleStatus:
    return _windows_status() if sys.platform == "win32" else _cron_status()
