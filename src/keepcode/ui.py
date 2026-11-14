"""How keepcode looks in the terminal: brand colours and the animated grid.

The grid is the keepcode motif: a field of squares like the GitHub
contribution graph. While scanning, a light sweeps across it and squares light
up as folders are visited; every project found gets its own square in the
colour of its status, and squares turn cyan as projects are backed up.

Everything here only draws. The work (scanning, backing up) runs in a worker
thread, and the screen is redrawn by Rich's Live display from the shared state.
"""

from __future__ import annotations

import hashlib
import os
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from keepcode import __version__
from keepcode.models import Project, Severity

CYAN = "#22D3EE"
RED = "#FF5D73"
AMBER = "#FF9A3C"
INK = "#E8F1FF"
MUTED = "#7C8BA6"
BORDER = "#2B3E62"
EMPTY = "#1B2A44"
VISITED = "#1C5266"
SETTLED = "#16314A"
SWEEP = "#2A93AE"
FLASH = "#F2FBFF"

CELL = "■"
FPS = 30
MIN_SCAN_SECONDS = 1.4
REVEAL_SECONDS = 0.7

STATUS_COLORS = {
    Severity.CRITICAL: RED,
    Severity.WARNING: AMBER,
    Severity.INFO: CYAN,
    Severity.OK: CYAN,
}


def spinner() -> str:
    """Braille dots need a modern terminal font; the old Windows console gets a line."""
    if os.name == "nt" and not os.environ.get("WT_SESSION"):
        return "line"
    return "dots"


def animations_enabled(console: Console) -> bool:
    if os.environ.get("KEEPCODE_NO_ANIMATION"):
        return False
    return console.is_terminal and not console.is_dumb_terminal


def project_key(project: Project) -> str:
    return os.path.normcase(str(project.path))


# --------------------------------------------------------------------- grid


@dataclass
class Grid:
    cols: int
    rows: int = 5
    seed: int = 7
    visited: int = 0
    sweeping: bool = False
    sweep_started: float = 0.0
    pulsing: int | None = None
    settled: bool = False
    colors: dict[int, str] = field(default_factory=dict)
    shown_at: dict[int, float] = field(default_factory=dict)
    slots: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.size = self.cols * self.rows
        order = list(range(self.size))
        random.Random(self.seed).shuffle(order)
        self.order = order

    @classmethod
    def for_width(cls, width: int) -> Grid:
        inner = min(width, 100) - 6  # panel border and padding
        return cls(cols=max(12, (inner + 1) // 2))

    def slot(self, key: str) -> int:
        """A stable square for each project, spread over the grid."""
        if key in self.slots:
            return self.slots[key]
        start = int(hashlib.sha1(key.encode("utf-8", "replace")).hexdigest(), 16) % self.size
        taken = set(self.slots.values())
        index = start
        for step in range(self.size):
            candidate = (start + step * 7) % self.size
            if candidate not in taken:
                index = candidate
                break
        self.slots[key] = index
        return index

    def show(self, key: str, color: str, at: float) -> int:
        index = self.slot(key)
        self.colors[index] = color
        self.shown_at[index] = at
        return index

    def column(self, key: str) -> int:
        return self.slot(key) % self.cols

    def render(self, now: float) -> Text:
        visited = set(self.order[: self.visited])
        sweep = None
        if self.sweeping:
            span = self.cols + 8
            sweep = ((now - self.sweep_started) * span / 1.3) % span - 4
        text = Text(no_wrap=True, overflow="crop")
        for row in range(self.rows):
            for col in range(self.cols):
                index = row * self.cols + col
                color = (SETTLED if self.settled else VISITED) if index in visited else EMPTY
                if sweep is not None:
                    distance = abs(col - sweep)
                    if distance < 0.8:
                        color = SWEEP
                    elif distance < 2.2 and color == EMPTY:
                        color = VISITED
                shown = self.shown_at.get(index)
                if shown is not None and now >= shown:
                    color = FLASH if now - shown < 0.12 else self.colors[index]
                if index == self.pulsing and int(now * 4) % 2 == 0:
                    color = SWEEP
                text.append(CELL, style=color)
                if col < self.cols - 1:
                    text.append(" ")
            if row < self.rows - 1:
                text.append("\n")
        return text


# --------------------------------------------------------------------- view


class View:
    """The panel shown while keepcode works. Rich redraws it on every frame."""

    def __init__(self, grid: Grid, width: int, update_note: Text | None = None) -> None:
        self.grid = grid
        self.width = width
        self.status: Text = Text("")
        self.counts: Text = Text("")
        self.update_note = update_note

    def __rich__(self) -> RenderableType:
        footer = Table.grid(expand=True)
        footer.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
        footer.add_column(justify="right", no_wrap=True)
        footer.add_row(self.status, self.counts)
        title = Text.assemble((f" {CELL} ", CYAN), ("keepcode ", f"bold {INK}"), (f"{__version__} ", MUTED))
        return Panel(
            Group(self.grid.render(time.monotonic()), Text(""), footer),
            box=box.ROUNDED,
            border_style=BORDER,
            padding=(1, 2),
            width=self.width,
            title=title,
            title_align="left",
            subtitle=self.update_note,
            subtitle_align="right",
        )


def counts_text(projects: list[Project]) -> Text:
    critical = sum(1 for p in projects if p.severity == Severity.CRITICAL)
    warning = sum(1 for p in projects if p.severity == Severity.WARNING)
    safe = len(projects) - critical - warning
    return Text.assemble(
        (f"{CELL} ", RED),
        (f"{critical}  ", INK),
        (f"{CELL} ", AMBER),
        (f"{warning}  ", INK),
        (f"{CELL} ", CYAN),
        (f"{safe}", INK),
    )


def legend() -> Text:
    return Text.assemble(
        (f"{CELL} ", RED),
        ("not backed up   ", MUTED),
        (f"{CELL} ", AMBER),
        ("at risk   ", MUTED),
        (f"{CELL} ", CYAN),
        ("safe", MUTED),
    )


def make_view(console: Console, update_note: Text | None = None) -> View:
    width = min(console.width, 100)
    return View(Grid.for_width(width), width, update_note)


# --------------------------------------------------------------------- scan


@dataclass
class Progress:
    folders: int = 0
    current: str = ""
    found: int = 0
    checked: int = 0
    phase: str = "scan"


def run_with_animation(
    console: Console,
    view: View,
    work: Callable[[], Any],
    frame: Callable[[float], None],
    minimum: float = 0.0,
) -> Any:
    """Run work() in a thread while the view animates. Re-raises its errors."""
    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["value"] = work()
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller
            outcome["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    started = time.monotonic()
    worker.start()
    with Live(view, console=console, refresh_per_second=FPS, transient=True):
        while worker.is_alive() or time.monotonic() - started < minimum:
            frame(time.monotonic() - started)
            time.sleep(1 / FPS)
    worker.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def reveal(console: Console, view: View, projects: list[Project]) -> None:
    """Light up each project's square, left to right, then print the final frame."""
    grid = view.grid
    grid.sweeping = False
    start = time.monotonic() + 0.05
    for project in projects:
        key = project_key(project)
        delay = grid.column(key) / max(grid.cols - 1, 1) * REVEAL_SECONDS
        grid.show(key, STATUS_COLORS[project.severity], start + delay)
    grid.visited = max(grid.visited, int(grid.size * 0.35))
    if animations_enabled(console):
        with Live(view, console=console, refresh_per_second=FPS, transient=True):
            time.sleep(REVEAL_SECONDS * 0.6)
            grid.settled = True
            time.sleep(REVEAL_SECONDS * 0.4 + 0.25)
    grid.settled = True
    settle(grid)
    console.print(view)


def settle(grid: Grid) -> None:
    """Finish every flash so a printed frame shows the final colours."""
    for index in grid.shown_at:
        grid.shown_at[index] = 0.0
    grid.pulsing = None


def scan_frame(view: View, progress: Progress, pretty: Callable[[str], str]) -> Callable[[float], None]:
    grid = view.grid

    def frame(elapsed: float) -> None:
        grid.sweeping = True
        grid.visited = min(int(grid.size * 0.45), progress.folders // 3 + int(elapsed * 10))
        if progress.phase == "check" and progress.found:
            view.status = Text.assemble(("Checking projects ", INK), (f"{progress.checked}/{progress.found}", MUTED))
        else:
            view.status = Text.assemble(("Scanning ", INK), (pretty(progress.current), MUTED))
        view.counts = Text.assemble(
            (f"{progress.folders:,} folders", MUTED), ("  ·  ", BORDER), (f"{progress.found} projects", INK)
        )

    return frame


# ------------------------------------------------------------------- backup


def backup_frame(view: View, state: dict[str, Any]) -> Callable[[float], None]:
    def frame(elapsed: float) -> None:
        view.status = state.get("status", Text(""))
        view.counts = state.get("counts", Text(""))

    return frame


def pulse_index(grid: Grid, project: Project) -> int:
    return grid.slot(project_key(project))


def mark_saved(grid: Grid, project: Project) -> None:
    grid.show(project_key(project), CYAN, time.monotonic())
