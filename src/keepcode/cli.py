"""Command line interface.

Double-clicking keepcode.exe opens the app window (`keepcode app`). Running
`keepcode` in a terminal with no command opens the guided mode instead: check
for updates, scan with the animated grid, offer to back up what is at risk, and
offer the daily check. Every step is also a command of its own for people who
prefer to type them.
"""

import contextlib
import json
import logging
import logging.handlers
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

# pythonw.exe and windowless runs have no console at all.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115

import typer
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from keepcode import __version__, git, ui, updater
from keepcode import github as gh
from keepcode import schedule as sched
from keepcode.analysis import analyze, analyze_many, sort_projects
from keepcode.backup import Engine, Plan, Result, describe, make_plan
from keepcode.config import LOG_FILE, Config, State, config_dir, normalize, pretty_path
from keepcode.fmt import plural, time_ago
from keepcode.models import Project, Severity
from keepcode.notify import notify
from keepcode.scanner import ScanStats, find_projects

app = typer.Typer(
    name="keepcode",
    help="Find every project on your computer that is not backed up, and fix it.",
    add_completion=False,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)
roots_app = typer.Typer(help="Folders that keepcode searches for projects.", no_args_is_help=True)
ignore_app = typer.Typer(help="Folders that keepcode skips.", no_args_is_help=True)
schedule_app = typer.Typer(help="Check your projects automatically once a day.", no_args_is_help=True)
app.add_typer(roots_app, name="roots")
app.add_typer(ignore_app, name="ignore")
app.add_typer(schedule_app, name="schedule")

THEME = Theme({"prompt.choices": ui.MUTED, "prompt.default": f"bold {ui.CYAN}", "status.spinner": ui.CYAN})
console = Console(highlight=False, theme=THEME)
error_console = Console(stderr=True, highlight=False)

COLORS = {
    Severity.CRITICAL: ui.RED,
    Severity.WARNING: ui.AMBER,
    Severity.INFO: ui.CYAN,
    Severity.OK: ui.CYAN,
}
# The square of the grid marks every result. It exists in every console font,
# including Consolas on Windows 10, unlike check marks or crosses.
OK = f"[{ui.CYAN}]{ui.CELL}[/]"
WARN = f"[{ui.AMBER}]{ui.CELL}[/]"
BAD = f"[{ui.RED}]{ui.CELL}[/]"


# --------------------------------------------------------------------- utils


def fail(message: str, code: int = 2) -> "typer.Exit":
    error_console.print(f"[{ui.RED}]Error:[/] {message}")
    return typer.Exit(code)


def require_git() -> None:
    if not git.git_available():
        raise fail("git is not installed or not on PATH. Get it from https://git-scm.com/downloads")


def short_path(path: Path) -> str:
    return pretty_path(path)


def interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty() and console.is_terminal


def launched_by_double_click() -> bool:
    """True when keepcode owns its console window, so it would close at the end.

    Windows lists the processes attached to a console. Started from a terminal,
    the terminal is in that list too. The standalone executable and the pip
    launcher both run as two processes, so up to two means nobody else is there.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        processes = (ctypes.c_uint * 16)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(processes, 16)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False
    own = 2 if updater.is_frozen() or sys.argv[0].lower().endswith(".exe") else 1
    return 0 < count <= own


def _roots(paths: Optional[List[Path]], config: Config, quiet: bool) -> List[str]:
    roots = [str(p) for p in paths] if paths else config.roots
    missing = [r for r in roots if not Path(os.path.expanduser(r)).is_dir()]
    if missing and not quiet:
        error_console.print(f"[{ui.AMBER}]Skipping folders that do not exist:[/] {', '.join(missing)}")
    return [r for r in roots if r not in missing]


def collect(
    paths: Optional[List[Path]],
    config: Config,
    state: State,
    quiet: bool = False,
    depth: Optional[int] = None,
) -> "tuple[list[Project], ScanStats]":
    roots = _roots(paths, config, quiet)
    status = (
        contextlib.nullcontext()
        if quiet
        else console.status("Searching for projects...", spinner=ui.spinner(), spinner_style=ui.CYAN)
    )
    with status as spinner:

        def progress(folders: int) -> None:
            if spinner is not None:
                spinner.update(f"Searching for projects... {folders:,} folders")

        found, stats = find_projects(roots, config.ignored_keys(), depth or config.max_depth, progress)

        def checked(done: int) -> None:
            if spinner is not None:
                spinner.update(f"Checking projects... {done}/{len(found)}")

        projects = analyze_many(found, state, checked)
    return sort_projects(projects), stats


def collect_animated(
    view: ui.View,
    paths: Optional[List[Path]],
    config: Config,
    state: State,
    depth: Optional[int] = None,
) -> "tuple[list[Project], ScanStats]":
    """Scan with the animated grid, then light up each project's square."""
    roots = _roots(paths, config, quiet=False)
    progress = ui.Progress()

    def work() -> "tuple[list[Project], ScanStats]":
        found, stats = find_projects(roots, config.ignored_keys(), depth or config.max_depth, progress=progress)
        progress.phase = "check"

        def checked(done: int) -> None:
            progress.checked = done

        return sort_projects(analyze_many(found, state, checked)), stats

    projects, stats = ui.run_with_animation(
        console, view, work, ui.scan_frame(view, progress, pretty_path), minimum=ui.MIN_SCAN_SECONDS
    )
    view.status = Text.assemble(
        (plural(len(projects), "project"), f"bold {ui.INK}"),
        (f" in {stats.folders:,} folders", ui.MUTED),
    )
    view.counts = ui.counts_text(projects)
    ui.reveal(console, view, projects)
    return projects, stats


def status_cell(project: Project) -> str:
    color = COLORS[project.severity]
    return f"[{color}]{ui.CELL} {project.severity.label}[/]"


def render(projects: List[Project], show_all: bool) -> None:
    rows = [p for p in projects if show_all or p.severity >= Severity.WARNING]
    if not rows:
        return
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, expand=True, border_style=ui.BORDER)
    table.add_column("Status", no_wrap=True)
    table.add_column("Project", ratio=5, overflow="fold")
    table.add_column("What needs attention", ratio=4, overflow="fold")
    table.add_column("Last change", no_wrap=True, justify="right")
    for project in rows:
        cell = f"[bold]{escape(project.name)}[/]"
        if project.stacks:
            cell += f" [{ui.MUTED}]{escape(', '.join(project.stacks[:3]))}[/]"
        cell += f"\n[{ui.MUTED}]{escape(short_path(project.path))}[/]"
        issues = (
            "\n".join(
                f"[{COLORS[i.severity]}]{escape(i.message)}[/]"
                for i in sorted(project.issues, key=lambda i: -i.severity)[:3]
            )
            or f"[{ui.CYAN}]Everything is pushed[/]"
        )
        table.add_row(status_cell(project), cell, issues, f"[{ui.MUTED}]{time_ago(project.last_activity)}[/]")
    console.print(table)


def summary(projects: List[Project], stats: ScanStats, show_all: bool) -> None:
    critical = sum(1 for p in projects if p.severity == Severity.CRITICAL)
    warning = sum(1 for p in projects if p.severity == Severity.WARNING)
    safe = len(projects) - critical - warning
    console.print(
        f"Found [bold]{plural(len(projects), 'project')}[/] in {stats.folders:,} folders ({stats.seconds:.1f}s)"
    )
    console.print(
        f"[{ui.RED}]{ui.CELL} {critical} not backed up[/]   "
        f"[{ui.AMBER}]{ui.CELL} {warning} at risk[/]   [{ui.CYAN}]{ui.CELL} {safe} safe[/]"
    )
    if not show_all and safe:
        console.print(f"[{ui.MUTED}]Safe projects are hidden. Use --all to list them.[/]")
    if any(p.fixable for p in projects):
        console.print(f"\nRun [bold {ui.CYAN}]keepcode backup[/] to back them up.")
    elif projects:
        console.print(f"\n[{ui.CYAN}]Everything is backed up.[/]")


def print_json(projects: List[Project], stats: ScanStats) -> None:
    print(
        json.dumps(
            {
                "version": __version__,
                "folders_scanned": stats.folders,
                "seconds": round(stats.seconds, 2),
                "projects": [p.to_dict() for p in projects],
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def run_scan(
    paths: Optional[List[Path]] = None,
    show_all: bool = False,
    as_json: bool = False,
    depth: Optional[int] = None,
) -> None:
    require_git()
    config = Config.load()
    state = State.load()
    if not as_json and ui.animations_enabled(console):
        projects, stats = collect_animated(ui.make_view(console), paths, config, state, depth)
    else:
        projects, stats = collect(paths, config, state, quiet=as_json, depth=depth)
    if as_json:
        print_json(projects, stats)
        return
    if not projects:
        console.print("No projects found. Add the folders where you keep code with [bold]keepcode roots add[/].")
        return
    render(projects, show_all)
    summary(projects, stats, show_all)


def setup_file_log() -> logging.Logger:
    logger = logging.getLogger("keepcode")
    if not logger.handlers:
        folder = config_dir()
        folder.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            folder / LOG_FILE, maxBytes=200_000, backupCount=1, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


# -------------------------------------------------------------------- login


def login_flow() -> str:
    """Connect keepcode to GitHub and return the account name."""
    app_id = gh.client_id()
    token = _device_login(app_id) if app_id else _pasted_token()
    client = gh.GitHub(token)
    try:
        user = client.user()
    except gh.GitHubError as exc:
        raise fail(f"GitHub did not accept the login: {exc}") from None
    try:
        gh.save_token(token)
    except gh.GitHubError as exc:
        raise fail(str(exc)) from None
    name = str(user.get("login", "?"))
    console.print(f"{OK} Connected to GitHub as [bold]{escape(name)}[/]")
    return name


def _device_login(app_id: str) -> str:
    try:
        code = gh.start_device_flow(app_id)
    except gh.GitHubError as exc:
        raise fail(str(exc)) from None
    copied = gh.copy_to_clipboard(code.user_code)
    opened = gh.open_browser(code.verification_uri)
    where = code.verification_uri.replace("https://", "")
    body = Text()
    body.append("1  ", style=ui.MUTED)
    body.append("Your browser is open at " if opened else "Open ", style=ui.INK)
    body.append(where, style=f"bold {ui.CYAN}")
    body.append("\n2  ", style=ui.MUTED)
    body.append("Enter the code   ", style=ui.INK)
    body.append(code.user_code, style=f"bold {ui.INK} on {ui.BORDER}")
    if copied:
        body.append("   copied", style=ui.MUTED)
    body.append("\n3  ", style=ui.MUTED)
    body.append("Click ", style=ui.INK)
    body.append("Authorize", style="bold")
    console.print(
        Panel(
            body,
            title=Text.assemble((f" {ui.CELL} ", ui.CYAN), ("Connect keepcode to GitHub ", f"bold {ui.INK}")),
            title_align="left",
            box=box.ROUNDED,
            border_style=ui.BORDER,
            padding=(1, 2),
            width=min(console.width, 72),
        )
    )
    try:
        with console.status("Waiting for you to approve on GitHub...", spinner=ui.spinner(), spinner_style=ui.CYAN):
            return gh.wait_for_token(app_id, code)
    except gh.GitHubError as exc:
        raise fail(str(exc)) from None


def _pasted_token() -> str:
    console.print(
        "keepcode needs a GitHub token with permission to create repositories.\n"
        "1. Open [link]https://github.com/settings/tokens/new?scopes=repo&description=keepcode[/link]\n"
        "2. Choose an expiration date and click [bold]Generate token[/]\n"
        "3. Copy the token and paste it below (Ctrl+V or right click)\n"
    )
    gh.open_browser("https://github.com/settings/tokens/new?scopes=repo&description=keepcode")
    token = console.input("GitHub token: ").strip()
    if not token:
        raise fail("No token given.")
    return token


def github_creator(required: bool, can_login: bool = False) -> Optional[gh.GitHubCreator]:
    token = gh.find_token()
    if token is None:
        if required and can_login:
            login_flow()
            token = gh.find_token()
        if token is None:
            if required:
                raise fail("Creating a GitHub repository needs a GitHub login. Run [bold]keepcode login[/] first.")
            return None
    client = gh.GitHub(token.value)
    try:
        client.user()
    except gh.GitHubError as exc:
        if exc.status == 401:
            if can_login:
                console.print(f"{WARN} The saved GitHub login no longer works. Connect again:")
                login_flow()
                return github_creator(required, can_login=False)
            raise fail(f"The GitHub token from {token.source} is not valid. Run keepcode login again.") from None
        if required:
            raise fail(str(exc)) from None
        return None
    return gh.GitHubCreator(client)


# ------------------------------------------------------------------ updates


def check_updates(config: Config, args: List[str]) -> "tuple[Optional[int], Optional[Text]]":
    """Check GitHub for a newer release. Returns (exit code if restarted, note for the panel)."""
    if os.environ.get("KEEPCODE_JUST_UPDATED"):
        console.print(f"{OK} keepcode was updated to [bold]{__version__}[/]\n")
        return None, Text.assemble((f"{ui.CELL} ", ui.CYAN), ("updated ", ui.MUTED))
    if config.auto_update == "off":
        return None, None
    with console.status("Checking for updates...", spinner=ui.spinner(), spinner_style=ui.CYAN):
        release = updater.latest_release(timeout=2.5)
    if release is None:
        return None, None
    if not updater.is_newer(release.version):
        return None, Text.assemble((f"{ui.CELL} ", ui.CYAN), ("up to date ", ui.MUTED))
    if not updater.is_frozen():
        console.print(
            f"{WARN} keepcode [bold]{release.version}[/] is available (you have {__version__}). "
            f"Update with [bold]{updater.update_command()}[/]\n"
        )
        return None, Text.assemble((f"{ui.CELL} ", ui.AMBER), (f"{release.version} available ", ui.MUTED))
    if not release.has_windows_build:
        return None, None
    if config.auto_update != "auto" and not Confirm.ask(
        console=console,
        prompt=f"keepcode [bold]{release.version}[/] is available (you have {__version__}). Update now?",
        default=True,
    ):
        return None, Text.assemble((f"{ui.CELL} ", ui.AMBER), (f"{release.version} available ", ui.MUTED))
    target = install_update(release)
    if target is None:
        return None, None
    console.print(f"{OK} Updated to [bold]{release.version}[/]. Starting the new version...\n")
    return updater.restart(target, args), None


def install_update(release: updater.Release) -> Optional[Path]:
    bar = Progress(
        TextColumn("  Downloading"),
        BarColumn(complete_style=ui.CYAN, finished_style=ui.CYAN, pulse_style=ui.CYAN),
        DownloadColumn(),
        console=console,
        transient=True,
    )
    task = bar.add_task("download", total=None)
    try:
        with bar:
            return updater.install(
                release, on_progress=lambda done, total: bar.update(task, completed=done, total=total or None)
            )
    except updater.UpdateError as exc:
        console.print(f"{BAD} Update failed: {escape(str(exc))}. You can keep using version {__version__}.")
        return None


# ------------------------------------------------------------------- backup


def plan_line(plan: Plan) -> str:
    return describe(plan)


ROUTINE_DETAILS = (".gitignore: added", "First commit created", "Private repository:")


def print_result(result: Result, brief: bool = False) -> None:
    """One line per project. Brief mode keeps only the details worth reading."""
    mark = OK if result.ok else BAD
    console.print(f"{mark} [bold]{escape(result.project.name)}[/]  [{ui.MUTED}]{escape(result.summary)}[/]")
    for line in result.details:
        if brief and result.ok and line.startswith(ROUTINE_DETAILS):
            continue
        console.print(f"    [{ui.MUTED}]{escape(line)}[/]")


def run_plans(plans: List[Plan], engine: Engine, view: Optional[ui.View] = None) -> List[Result]:
    """Back up each project. With a view, the grid shows the progress."""
    results: List[Result] = []
    if view is None:
        for plan in plans:
            project = plan.project
            with console.status(
                f"Backing up {escape(project.name)}...", spinner=ui.spinner(), spinner_style=ui.CYAN
            ) as spinner:
                engine.log = lambda message, s=spinner, n=project.name: s.update(f"{escape(n)}: {escape(message)}")
                result = engine.run(project)
            print_result(result)
            results.append(result)
        return results

    grid = view.grid
    for plan in plans:
        key = ui.project_key(plan.project)
        if grid.slot(key) not in grid.colors:
            grid.show(key, ui.STATUS_COLORS[plan.project.severity], 0.0)
    shared: Dict[str, Text] = {}

    def work() -> None:
        for number, plan in enumerate(plans, start=1):
            project = plan.project
            grid.pulsing = grid.slot(ui.project_key(project))
            shared["status"] = Text.assemble(("Backing up ", ui.INK), (project.name, f"bold {ui.INK}"))
            shared["counts"] = Text(f"{number} of {len(plans)}", style=ui.MUTED)

            def log(message: str, name: str = project.name) -> None:
                shared["status"] = Text.assemble((name + "  ", f"bold {ui.INK}"), (message, ui.MUTED))

            engine.log = log
            result = engine.run(project)
            grid.pulsing = None
            if result.ok:
                ui.mark_saved(grid, project)
            print_result(result, brief=True)
            results.append(result)

    ui.run_with_animation(console, view, work, ui.backup_frame(view, shared), minimum=0.4)
    ui.settle(grid)
    done = sum(1 for r in results if r.ok)
    view.status = Text.assemble((f"{plural(done, 'project')} backed up", f"bold {ui.INK}"))
    if done < len(results):
        view.status.append(f"   {len(results) - done} failed", style=ui.RED)
    view.counts = Text("")
    console.print(view)
    return results


def choose(projects: List[Project]) -> List[Project]:
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, border_style=ui.BORDER)
    table.add_column("#", justify="right", style=ui.MUTED)
    table.add_column("Status", no_wrap=True)
    table.add_column("Project")
    table.add_column("What needs attention", overflow="fold")
    for number, project in enumerate(projects, start=1):
        top = max(project.issues, key=lambda i: i.severity)
        table.add_row(str(number), status_cell(project), escape(project.name), escape(top.message))
    console.print(table)
    while True:
        answer = Prompt.ask(
            console=console, prompt="Which projects? Numbers separated by commas, or [bold]all[/]", default="all"
        )
        answer = answer.strip().lower()
        if answer in ("all", "a", "*"):
            return projects
        try:
            numbers = {int(part) for part in answer.replace(" ", "").split(",") if part}
        except ValueError:
            console.print(f"[{ui.AMBER}]Write numbers like 1,3,4 or all.[/]")
            continue
        if not numbers or any(n < 1 or n > len(projects) for n in numbers):
            console.print(f"[{ui.AMBER}]Choose numbers between 1 and {len(projects)}.[/]")
            continue
        return [projects[n - 1] for n in sorted(numbers)]


def show_plans(plans: List[Plan]) -> None:
    for plan in plans:
        console.print(f"\n[bold]{escape(plan.project.name)}[/] [{ui.MUTED}]{escape(short_path(plan.project.path))}[/]")
        for number, step in enumerate(plan.steps, start=1):
            console.print(f"  [{ui.MUTED}]{number}.[/] {escape(step)}")
    console.print()


def show_compact_plans(plans: List[Plan]) -> None:
    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2, 0, 0))
    table.add_column(no_wrap=True)
    table.add_column(overflow="fold")
    table.add_column(overflow="fold", style=ui.MUTED)
    for plan in plans:
        color = COLORS[plan.project.severity]
        table.add_row(
            f"[{color}]{ui.CELL}[/] [bold]{escape(plan.project.name)}[/]",
            escape(plan_line(plan)),
            escape(short_path(plan.project.path)),
        )
    console.print(table)
    console.print()


# ------------------------------------------------------------------- guided


def offer_git_install() -> None:
    console.print(f"{BAD} keepcode needs [bold]Git[/], and it is not installed.")
    if sys.platform == "win32" and shutil.which("winget") and interactive():
        if Confirm.ask(console=console, prompt="Install Git now with winget?", default=True):
            code = subprocess.call(
                [
                    "winget",
                    "install",
                    "--id",
                    "Git.Git",
                    "-e",
                    "--source",
                    "winget",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                ]
            )
            if code == 0:
                console.print(f"{OK} Git is installed. Close this window and open keepcode again.")
                return
    console.print("Get it from [link]https://git-scm.com/downloads[/link], then open keepcode again.")
    gh.open_browser("https://git-scm.com/downloads")


def offer_schedule(config: Config) -> None:
    try:
        if sched.status().enabled:
            return
    except sched.ScheduleError:
        return
    question = f"\nCheck your projects every day at {config.schedule_time} and tell you when something is at risk?"
    if not Confirm.ask(console=console, prompt=question, default=True):
        console.print(f"[{ui.MUTED}]You can turn it on later with keepcode schedule on.[/]")
        return
    try:
        detail = sched.enable(config.schedule_time)
    except sched.ScheduleError as exc:
        console.print(f"{WARN} Could not schedule the daily check: {escape(str(exc))}")
        return
    console.print(f"{OK} {escape(detail)}")


def guided() -> int:
    """What happens when keepcode opens without a command."""
    with contextlib.suppress(Exception):
        console.set_window_title("keepcode")
    updater.remove_leftovers()
    config = Config.load()
    restarted, note = check_updates(config, [])
    if restarted is not None:
        return restarted
    if not git.git_available():
        offer_git_install()
        return 2

    state = State.load()
    view = ui.make_view(console, note)
    projects, stats = collect_animated(view, None, config, state)
    if not projects:
        console.print("No projects found. Add the folders where you keep code with [bold]keepcode roots add[/].")
        return 0

    render(projects, show_all=False)
    todo = [p for p in projects if p.fixable]
    if not todo:
        console.print(f"{OK} [bold]Everything is backed up.[/]")
        offer_schedule(config)
        return 0

    copies = [p for p in todo if p.copy_of]
    main = [p for p in todo if not p.copy_of] or todo
    note_copies = ""
    if copies and main is not todo:
        note_copies = f" [{ui.MUTED}](skipping {plural(len(copies), 'copy')} of other folders)[/]"
    console.print(
        f"\n[bold]{plural(len(main), 'project')}[/] can be backed up to private GitHub repositories.{note_copies}"
    )
    answer = Prompt.ask(
        console=console,
        prompt=f"Back them up? [{ui.MUTED}]y = yes, n = no, c = choose[/]",
        choices=["y", "n", "c"],
        default="y",
        show_choices=False,
    )
    if answer == "n":
        offer_schedule(config)
        return 0
    selected = choose(todo) if answer == "c" else main
    plans = [make_plan(p) for p in selected]
    console.print()
    show_compact_plans(plans)

    creator = github_creator(required=any(p.creates_repo for p in plans), can_login=True)
    engine = Engine(creator, state)
    results = run_plans(plans, engine, view)
    state.save()
    failed = [r for r in results if not r.ok]
    if failed:
        console.print(f"\n{WARN} {plural(len(failed), 'project')} could not be backed up. The reasons are above.")
    offer_schedule(config)
    return 1 if failed else 0


def open_window(smoke_test: bool = False) -> Optional[int]:
    """Runs the app window. None when it cannot open here, after saying why."""
    from keepcode import app as window_app
    from keepcode.app.window import AppUnavailable, show_console

    try:
        return window_app.run(smoke_test=smoke_test)
    except AppUnavailable as exc:
        show_console()
        setup_file_log().warning("App window unavailable: %s", exc)
        console.print(f"{WARN} The keepcode window could not open: {escape(str(exc))}")
        return None


def run_guided() -> None:
    double_click = launched_by_double_click()
    if double_click and os.environ.get("KEEPCODE_CONSOLE") != "1":
        opened = open_window()
        if opened is not None:
            raise typer.Exit(opened)
        console.print(f"[{ui.MUTED}]Using the terminal version instead.[/]\n")
    code = 0
    try:
        if not interactive():
            run_scan()
        else:
            code = guided()
    except typer.Exit as exc:
        code = exc.exit_code
    except KeyboardInterrupt:
        console.print(f"\n[{ui.MUTED}]Stopped.[/]")
        code = 130
    except Exception as exc:  # noqa: BLE001 - shown to the user instead of a window that just closes
        console.print(f"{BAD} Something went wrong: {escape(str(exc))}")
        code = 1
    if double_click:
        with contextlib.suppress(EOFError, KeyboardInterrupt):
            console.input(f"\n[{ui.MUTED}]Press Enter to close[/]")
    raise typer.Exit(code)


# ------------------------------------------------------------------ commands


def version_callback(value: bool) -> None:
    if value:
        console.print(f"keepcode {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", "-V", callback=version_callback, is_eager=True, help="Show the version and exit."
    ),
) -> None:
    """Find every project on your computer that is not backed up, and fix it.

    Run without a command for the guided mode: scan, back up and set up the daily check.
    """
    if ctx.invoked_subcommand is None:
        run_guided()


@app.command("app")
def app_command(
    smoke_test: bool = typer.Option(
        False, "--smoke-test", hidden=True, help="Open the window, check that the page loads, and close it."
    ),
) -> None:
    """Open keepcode in its own window."""
    code = open_window(smoke_test=smoke_test)
    raise typer.Exit(2 if code is None else code)


@app.command()
def scan(
    path: Optional[List[Path]] = typer.Option(
        None, "--path", "-p", help="Folder to search instead of the configured roots. Repeatable."
    ),
    show_all: bool = typer.Option(False, "--all", "-a", help="Also list the projects that are safe."),
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON."),
    depth: Optional[int] = typer.Option(None, "--depth", min=1, max=20, help="How many folder levels to search."),
) -> None:
    """List your projects and show which ones are not backed up."""
    run_scan(path, show_all, as_json, depth)


@app.command()
def backup(
    paths: Optional[List[Path]] = typer.Argument(
        None, help="Project folders to back up. Without paths, keepcode scans and lets you choose."
    ),
    every: bool = typer.Option(False, "--all", "-a", help="Back up every project at risk without asking which."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen without changing anything."),
    allow_secrets: bool = typer.Option(
        False, "--allow-secrets", help="Back up even if secrets are found in the code (repositories are private)."
    ),
) -> None:
    """Back up projects to GitHub without touching your files or branches."""
    require_git()
    config = Config.load()
    state = State.load()
    animate = ui.animations_enabled(console)
    view = ui.make_view(console) if animate else None

    if paths:
        candidates = []
        for raw in paths:
            folder = raw.expanduser().resolve()
            if not folder.is_dir():
                raise fail(f"{raw} is not a folder")
            candidates.append(analyze(folder, state))
    else:
        if view is not None:
            projects, _ = collect_animated(view, None, config, state)
        else:
            projects, _ = collect(None, config, state)
        candidates = [p for p in projects if p.fixable]

    todo = [p for p in candidates if p.fixable]
    for project in candidates:
        if not project.fixable:
            console.print(f"{OK} {escape(project.name)}: nothing to back up")
    if not todo:
        if not paths:
            console.print(f"{OK} Everything is backed up.")
        return

    is_interactive = interactive() and not yes
    if not paths and not every and is_interactive:
        todo = choose(todo)
        if not todo:
            console.print("Nothing selected.")
            return

    plans = [make_plan(p) for p in todo]
    show_plans(plans)
    if dry_run:
        console.print(f"[{ui.MUTED}]Dry run: nothing was changed.[/]")
        return

    needs_repo = any(plan.creates_repo for plan in plans)
    creator = github_creator(required=needs_repo, can_login=is_interactive)

    if not yes:
        if not is_interactive:
            raise fail("Confirmation needed. Run again with --yes to back up without asking.")
        if not Confirm.ask(console=console, prompt=f"Back up {plural(len(plans), 'project')}?", default=True):
            raise typer.Exit(1)

    engine = Engine(creator, state, allow_secrets=allow_secrets)
    results = run_plans(plans, engine, view)
    state.save()
    failures = sum(1 for r in results if not r.ok)
    done = len(results) - failures
    if view is None:
        console.print(f"\n{done} backed up, {failures} failed." if failures else f"\n[{ui.CYAN}]{done} backed up.[/]")
    if failures:
        raise typer.Exit(1)


@app.command()
def check(
    send_notification: bool = typer.Option(
        False, "--notify", help="Show a desktop notification when something is at risk."
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print nothing (for scheduled runs)."),
) -> None:
    """Scan and exit with code 1 when a project is at risk. Used by the daily schedule."""
    logger = setup_file_log()
    if not git.git_available():
        logger.error("git not found")
        raise typer.Exit(2)
    config = Config.load()
    state = State.load()

    if config.auto_update == "auto" and updater.is_frozen():
        updater.remove_leftovers()
        release = updater.available_update(timeout=5)
        if release is not None:
            try:
                updater.install(release)
                logger.info("updated to %s", release.version)
            except updater.UpdateError as exc:
                logger.warning("update failed: %s", exc)

    projects, stats = collect(None, config, state, quiet=True)

    if config.auto_backup:
        creator = github_creator(required=False)
        engine = Engine(creator, state)
        refreshed = []
        for project in projects:
            plan = make_plan(project) if project.fixable else None
            if plan is not None and not plan.creates_repo:
                result = engine.run(project)
                logger.info("auto backup %s: %s", project.path, result.summary)
                project = analyze(project.path, state)
            refreshed.append(project)
        projects = sort_projects(refreshed)

    at_risk = [p for p in projects if p.at_risk and not p.copy_of]
    state.record_check(time.time(), len(at_risk), len(projects))
    state.save()
    logger.info("check: %d projects, %d at risk (%.1fs)", len(projects), len(at_risk), stats.seconds)

    if send_notification and at_risk:
        names = ", ".join(p.name for p in at_risk[:3])
        more = f" and {len(at_risk) - 3} more" if len(at_risk) > 3 else ""
        title = f"{len(at_risk)} project{'s are' if len(at_risk) != 1 else ' is'} not backed up"
        if not notify(f"keepcode: {title}", f"{names}{more}. Open keepcode to back them up."):
            logger.warning("could not show a notification")
    if not quiet:
        render(projects, show_all=False)
        summary(projects, stats, show_all=False)
    raise typer.Exit(1 if at_risk else 0)


@app.command()
def update() -> None:
    """Update keepcode to the newest version published on GitHub."""
    updater.remove_leftovers()
    with console.status("Checking for updates...", spinner=ui.spinner(), spinner_style=ui.CYAN):
        release = updater.latest_release(timeout=10)
    if release is None:
        raise fail("Could not check for updates. Are you online?")
    if not updater.is_newer(release.version):
        console.print(f"{OK} You have the newest version ({__version__}).")
        return
    if not updater.is_frozen():
        command = updater.update_command()
        console.print(f"keepcode [bold]{release.version}[/] is available. Update with [bold]{command}[/]")
        return
    if not release.has_windows_build:
        raise fail(f"Version {release.version} is still being published. Try again in a few minutes.")
    target = install_update(release)
    if target is None:
        raise typer.Exit(1)
    console.print(f"{OK} Updated to [bold]{release.version}[/].")


@app.command()
def login(
    token: Optional[str] = typer.Option(None, "--token", help="Use this token instead of logging in with the browser."),
) -> None:
    """Connect keepcode to GitHub so it can create private repositories."""
    if token is None:
        login_flow()
        return
    token = token.strip()
    if not token:
        raise fail("No token given.")
    client = gh.GitHub(token)
    try:
        user = client.user()
    except gh.GitHubError as exc:
        raise fail(f"GitHub did not accept the token: {exc}") from None
    try:
        gh.save_token(token)
    except gh.GitHubError as exc:
        raise fail(str(exc)) from None
    console.print(f"{OK} Logged in as [bold]{escape(user.get('login', '?'))}[/]. Saved in the system keychain.")


@app.command()
def logout() -> None:
    """Remove the GitHub login saved by keepcode."""
    if gh.delete_token():
        console.print(f"{OK} Logged out. The login was removed from the system keychain.")
    else:
        console.print("No saved login found.")


@app.command()
def doctor() -> None:
    """Check that keepcode has everything it needs."""
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, show_header=False, border_style=ui.BORDER)
    table.add_column("Check", style="bold")
    table.add_column("Result", overflow="fold")

    kind = "standalone executable" if updater.is_frozen() else f"Python {platform.python_version()}"
    table.add_row("keepcode", f"{OK} {__version__} ({kind} on {platform.system()})")
    release = updater.latest_release(timeout=3)
    if release is not None:
        newer = updater.is_newer(release.version)
        text = f"{WARN} {release.version} available: keepcode update" if newer else f"{OK} up to date"
        table.add_row("Updates", text)
    version = git.git_version()
    table.add_row(
        "git", f"{OK} {version}" if version else f"{BAD} not found, get it from https://git-scm.com/downloads"
    )
    from keepcode.app.window import AppUnavailable, load_engine

    try:
        load_engine()
        table.add_row("App window", f"{OK} ready: keepcode app")
    except AppUnavailable as exc:
        table.add_row("App window", f"{WARN} {escape(str(exc))}")

    token = gh.find_token()
    if token is None:
        table.add_row("GitHub", f"{WARN} not connected (only needed to create repositories): keepcode login")
    else:
        try:
            login_name = gh.GitHub(token.value).user().get("login")
            table.add_row("GitHub", f"{OK} connected as {login_name} ({token.source})")
        except gh.GitHubError as exc:
            table.add_row("GitHub", f"{BAD} login from {token.source} failed: {exc}")

    config = Config.load()
    for root in config.roots:
        exists = Path(os.path.expanduser(root)).is_dir()
        table.add_row("Root", f"{OK if exists else BAD} {escape(root)}")
    if config.ignore:
        table.add_row("Ignored", plural(len(config.ignore), "folder"))
    table.add_row("Auto backup", "on" if config.auto_backup else "off")
    table.add_row("Auto update", config.auto_update)

    try:
        status = sched.status()
        table.add_row("Schedule", f"{OK if status.enabled else WARN} {status.detail}")
    except sched.ScheduleError as exc:
        table.add_row("Schedule", f"{WARN} unknown ({exc})")

    last = State.load().data.get("last_check")
    if last:
        when = time_ago(last.get("time"))
        table.add_row("Last check", f"{when}: {last.get('at_risk')} of {last.get('total')} at risk")
    table.add_row("Config folder", escape(str(config_dir())))
    console.print(table)


@app.command("config")
def config_command(
    depth: Optional[int] = typer.Option(None, "--depth", min=1, max=20, help="How many folder levels to search."),
    auto_backup: Optional[bool] = typer.Option(
        None,
        "--auto-backup/--no-auto-backup",
        help="In the daily check, also save uncommitted and unpushed work to keepcode/* branches.",
    ),
    auto_update: Optional[str] = typer.Option(
        None, "--auto-update", help="ask (default), auto (update without asking) or off."
    ),
) -> None:
    """Show or change settings."""
    config = Config.load()
    changed = False
    if depth is not None:
        config.max_depth = depth
        changed = True
    if auto_backup is not None:
        config.auto_backup = auto_backup
        changed = True
    if auto_update is not None:
        if auto_update not in ("ask", "auto", "off"):
            raise fail("--auto-update must be ask, auto or off.")
        config.auto_update = auto_update
        changed = True
    if changed:
        config.save()
        console.print(f"{OK} Settings saved.")
    console.print(f"Search depth: {config.max_depth}")
    console.print(f"Auto backup in the daily check: {'on' if config.auto_backup else 'off'}")
    console.print(f"Updates: {config.auto_update}")
    console.print(f"Roots: {', '.join(config.roots)}")
    console.print(f"Config file: {config_dir() / 'config.json'}")


# --------------------------------------------------------------- sub-apps


@roots_app.command("list")
def roots_list() -> None:
    """Show the folders keepcode searches."""
    for root in Config.load().roots:
        console.print(escape(root), soft_wrap=True)


@roots_app.command("add")
def roots_add(folders: List[Path] = typer.Argument(..., help="Folders to add.")) -> None:
    """Add folders to search."""
    config = Config.load()
    keys = {normalize(r) for r in config.roots}
    for folder in folders:
        resolved = folder.expanduser().resolve()
        if not resolved.is_dir():
            raise fail(f"{folder} is not a folder")
        if normalize(resolved) not in keys:
            config.roots.append(str(resolved))
            keys.add(normalize(resolved))
            console.print(f"[{ui.CYAN}]+[/] {escape(str(resolved))}")
    config.save()


@roots_app.command("remove")
def roots_remove(folders: List[Path] = typer.Argument(..., help="Folders to remove.")) -> None:
    """Stop searching folders."""
    config = Config.load()
    targets = {normalize(f) for f in folders}
    before = len(config.roots)
    config.roots = [r for r in config.roots if normalize(r) not in targets]
    if len(config.roots) == before:
        raise fail("None of those folders are roots. See keepcode roots list.", code=1)
    if not config.roots:
        raise fail("keepcode needs at least one root. Add another one first.", code=1)
    config.save()
    console.print(f"{OK} Removed.")


@roots_app.command("reset")
def roots_reset() -> None:
    """Search the whole home folder again (the default)."""
    config = Config.load()
    config.roots = [str(Path.home())]
    config.save()
    console.print(f"{OK} Roots reset to {escape(str(Path.home()))}")


@ignore_app.command("list")
def ignore_list() -> None:
    """Show the folders keepcode skips."""
    ignored = Config.load().ignore
    if not ignored:
        console.print("No folders are ignored.")
    for folder in ignored:
        console.print(escape(folder), soft_wrap=True)


@ignore_app.command("add")
def ignore_add(folders: List[Path] = typer.Argument(..., help="Folders to skip.")) -> None:
    """Skip folders (and everything inside them)."""
    config = Config.load()
    keys = config.ignored_keys()
    for folder in folders:
        resolved = str(folder.expanduser().resolve())
        if normalize(resolved) not in keys:
            config.ignore.append(resolved)
            keys.add(normalize(resolved))
            console.print(f"[{ui.CYAN}]+[/] {escape(resolved)}")
    config.save()


@ignore_app.command("remove")
def ignore_remove(folders: List[Path] = typer.Argument(..., help="Folders to stop skipping.")) -> None:
    """Stop skipping folders."""
    config = Config.load()
    targets = {normalize(f) for f in folders}
    before = len(config.ignore)
    config.ignore = [f for f in config.ignore if normalize(f) not in targets]
    if len(config.ignore) == before:
        raise fail("None of those folders are ignored.", code=1)
    config.save()
    console.print(f"{OK} Removed.")


@schedule_app.command("on")
def schedule_on(
    at: Optional[str] = typer.Option(None, "--time", "-t", help="Time of day, HH:MM (default 18:00)."),
) -> None:
    """Check your projects every day and notify you when something is at risk."""
    config = Config.load()
    when = at or config.schedule_time
    try:
        detail = sched.enable(when)
    except sched.ScheduleError as exc:
        raise fail(str(exc)) from None
    config.schedule_time = when
    config.save()
    console.print(f"{OK} {detail}")


@schedule_app.command("off")
def schedule_off() -> None:
    """Stop the daily check."""
    try:
        removed = sched.disable()
    except sched.ScheduleError as exc:
        raise fail(str(exc)) from None
    console.print(f"{OK} Daily check removed." if removed else "No daily check was scheduled.")


@schedule_app.command("status")
def schedule_status() -> None:
    """Show whether the daily check is on."""
    try:
        console.print(sched.status().detail)
    except sched.ScheduleError as exc:
        raise fail(str(exc)) from None


def main() -> None:
    app()
