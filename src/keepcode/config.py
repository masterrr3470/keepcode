"""User configuration and persistent state.

Everything lives in one folder per user:

* Windows: %APPDATA%\\keepcode
* macOS:   ~/Library/Application Support/keepcode
* Linux:   $XDG_CONFIG_HOME/keepcode (or ~/.config/keepcode)

The KEEPCODE_HOME environment variable overrides the location (used by tests).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

CONFIG_FILE = "config.json"
STATE_FILE = "state.json"
LOG_FILE = "keepcode.log"


def config_dir() -> Path:
    override = os.environ.get("KEEPCODE_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "keepcode"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "keepcode"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "keepcode"


def normalize(path: str | Path) -> str:
    """Absolute, case-normalized path used as a stable key on every platform."""
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))


@dataclass
class Config:
    roots: list[str] = field(default_factory=lambda: [str(Path.home())])
    ignore: list[str] = field(default_factory=list)
    max_depth: int = 7
    auto_backup: bool = False
    schedule_time: str = "18:00"

    @classmethod
    def load(cls) -> Config:
        data = _read_json(config_dir() / CONFIG_FILE)
        config = cls()
        for key, value in data.items():
            if hasattr(config, key):
                setattr(config, key, value)
        config._validate()
        return config

    def save(self) -> None:
        self._validate()
        _write_json(config_dir() / CONFIG_FILE, asdict(self))

    def _validate(self) -> None:
        if not isinstance(self.roots, list) or not all(isinstance(r, str) for r in self.roots):
            self.roots = [str(Path.home())]
        if not isinstance(self.ignore, list):
            self.ignore = []
        try:
            self.max_depth = max(1, min(int(self.max_depth), 20))
        except (TypeError, ValueError):
            self.max_depth = 7
        self.auto_backup = bool(self.auto_backup)

    def ignored_keys(self) -> set[str]:
        return {normalize(p) for p in self.ignore}


class State:
    """Remembers snapshots made by keepcode and the result of the last check."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data: dict[str, Any] = data or {}
        self.data.setdefault("snapshots", {})

    @classmethod
    def load(cls) -> State:
        return cls(_read_json(config_dir() / STATE_FILE))

    def save(self) -> None:
        _write_json(config_dir() / STATE_FILE, self.data)

    def snapshot_for(self, project_path: Path) -> dict[str, Any] | None:
        return self.data["snapshots"].get(normalize(project_path))

    def record_snapshot(self, project_path: Path, fingerprint: str, ref: str, when: float) -> None:
        self.data["snapshots"][normalize(project_path)] = {
            "fingerprint": fingerprint,
            "ref": ref,
            "time": when,
        }

    def record_check(self, when: float, at_risk: int, total: int) -> None:
        self.data["last_check"] = {"time": when, "at_risk": at_risk, "total": total}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Write atomically so a crash never leaves a half-written file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
