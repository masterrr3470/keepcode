"""Command line interface."""

import contextlib
import json
import logging
import logging.handlers
import os
import platform
import sys
import time
from pathlib import Path
from typing import List, Optional

# pythonw.exe (used by the scheduled task on Windows) has no console at all.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115

import typer
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm, Prompt
from rich.table import Table

from keepcode import __version__, git
from keepcode import github as gh
from keepcode import schedule as sched
from keepcode.analysis import analyze, analyze_many, sort_projects
from keepcode.backup import Engine, Plan, make_plan
from keepcode.config import LOG_FILE, Config, State, config_dir, normalize
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

console = Console(highlight=False)
error_console = Console(stderr=True, highlight=False)

COLORS = {
    Severity.CRITICAL: "red",
    Severity.WARNING: "yellow",
    Severity.INFO: "cyan",
    Severity.OK: "green",
}


# --------------------------------------------------------------------- utils


def fail(message: str, code: int = 2) -> "typer.Exit":
    error_console.print(f"[red]Error:[/] {message}")
    return typer.Exit(code)


def require_git() -> None:
    if not git.git_available():
        raise fail("git is not installed or not on PATH. Get it from https://git-scm.com/downloads")


def time_ago(timestamp: Optional[float]) -> str:
    if not timestamp:
        return "unknown"
    seconds = max(0, time.time() - timestamp)
    if seconds < 60:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)} min ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)} h ago"
    days = hours / 24
    if days < 2:
        return "yesterday"
    if days < 31:
        return f"{int(days)} days ago"
    if days < 365:
        months = int(days / 30.4)
        return f"{months} month{'s' if months > 1 else ''} ago"
    return time.strftime("%b %Y", time.localtime(timestamp))


def short_path(path: Path) -> str:
    text = str(path)
    home = str(Path.home())
    if normalize(text).startswith(normalize(home) + os.sep):
        return "~" + text[len(home) :]
    return text


def collect(
    paths: Optional[List[Path]],
    config: Config,
    state: State,
    quiet: bool = False,
    depth: Optional[int] = None,
) -> "tuple[list[Project], ScanStats]":
    roots = [str(p) for p in paths] if paths else config.roots
    missing = [r for r in roots if not Path(os.path.expanduser(r)).is_dir()]
    if missing and not quiet:
        error_console.print(f"[yellow]Skipping folders that do not exist:[/] {', '.join(missing)}")
    roots = [r for r in roots if r not in missing]
    status = contextlib.nullcontext() if quiet else console.status("Searching for projects...")
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


def status_cell(project: Project) -> str:
    color = COLORS[project.severity]
    return f"[{color}]● {project.severity.label}[/]"


def render(projects: List[Project], show_all: bool) -> None:
    rows = [p for p in projects if show_all or p.severity >= Severity.WARNING]
    if not rows:
        return
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, expand=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Project", ratio=5, overflow="fold")
    table.add_column("What needs attention", ratio=4, overflow="fold")
    table.add_column("Last change", no_wrap=True, justify="right")
    for project in rows:
        cell = f"[bold]{escape(project.name)}[/]"
        if project.stacks:
            cell += f" [dim]{escape(', '.join(project.stacks[:3]))}[/]"
        cell += f"\n[dim]{escape(short_path(project.path))}[/]"
        issues = (
            "\n".join(
                f"[{COLORS[i.severity]}]{escape(i.message)}[/]"
                for i in sorted(project.issues, key=lambda i: -i.severity)[:3]
            )
            or "[green]Everything is pushed[/]"
        )
        table.add_row(status_cell(project), cell, issues, time_ago(project.last_activity))
    console.print(table)


def summary(projects: List[Project], stats: ScanStats, show_all: bool) -> None:
    critical = sum(1 for p in projects if p.severity == Severity.CRITICAL)
    warning = sum(1 for p in projects if p.severity == Severity.WARNING)
    safe = len(projects) - critical - warning
    console.print(
        f"Found [bold]{len(projects)}[/] project{'s' if len(projects) != 1 else ''} "
        f"in {stats.folders:,} folders ({stats.seconds:.1f}s)"
    )
    console.print(f"[red]● {critical} not backed up[/]   [yellow]● {warning} at risk[/]   [green]● {safe} safe[/]")
    if not show_all and safe:
        console.print("[dim]Safe projects are hidden. Use --all to list them.[/]")
    if any(p.fixable for p in projects):
        console.print("\nRun [bold cyan]keepcode backup[/] to back them up.")
    elif projects:
        console.print("\n[green]Everything is backed up.[/]")


def run_scan(
    paths: Optional[List[Path]] = None,
    show_all: bool = False,
    as_json: bool = False,
    depth: Optional[int] = None,
) -> None:
    require_git()
    config = Config.load()
    state = State.load()
    projects, stats = collect(paths, config, state, quiet=as_json, depth=depth)
    if as_json:
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


def github_creator(required: bool) -> Optional[gh.GitHubCreator]:
    token = gh.find_token()
    if token is None:
        if required:
            raise fail(
                "Creating a GitHub repository needs a token. Run [bold]keepcode login[/] first.",
            )
        return None
    client = gh.GitHub(token.value)
    try:
        client.user()
    except gh.GitHubError as exc:
        if exc.status == 401:
            raise fail(f"The GitHub token from {token.source} is not valid. Run keepcode login again.") from None
        if required:
            raise fail(str(exc)) from None
        return None
    return gh.GitHubCreator(client)


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

    Run without a command to scan your projects.
    """
    if ctx.invoked_subcommand is None:
        run_scan()


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

    if paths:
        candidates = []
        for raw in paths:
            folder = raw.expanduser().resolve()
            if not folder.is_dir():
                raise fail(f"{raw} is not a folder")
            candidates.append(analyze(folder, state))
    else:
        projects, _ = collect(None, config, state)
        candidates = [p for p in projects if p.fixable]

    todo = [p for p in candidates if p.fixable]
    for project in candidates:
        if not project.fixable:
            console.print(f"[green]✔[/] {escape(project.name)}: nothing to back up")
    if not todo:
        if not paths:
            console.print("[green]Everything is backed up.[/]")
        return

    interactive = sys.stdin.isatty() and not yes
    if not paths and not every and interactive:
        todo = choose(todo)
        if not todo:
            console.print("Nothing selected.")
            return

    plans = [make_plan(p) for p in todo]
    show_plans(plans)
    if dry_run:
        console.print("[dim]Dry run: nothing was changed.[/]")
        return

    needs_repo = any(plan.creates_repo for plan in plans)
    creator = github_creator(required=needs_repo)

    if not yes:
        if not interactive:
            raise fail("Confirmation needed. Run again with --yes to back up without asking.")
        if not Confirm.ask(f"Back up {len(plans)} project{'s' if len(plans) != 1 else ''}?", default=True):
            raise typer.Exit(1)

    engine = Engine(creator, state, allow_secrets=allow_secrets)
    failures = 0
    for plan in plans:
        project = plan.project
        with console.status(f"Backing up {escape(project.name)}...") as spinner:
            engine.log = lambda message, s=spinner, n=project.name: s.update(f"{escape(n)}: {escape(message)}")
            result = engine.run(project)
        mark = "[green]✔[/]" if result.ok else "[red]✖[/]"
        console.print(f"{mark} [bold]{escape(project.name)}[/]  {escape(result.summary)}")
        for line in result.details:
            console.print(f"    [dim]{escape(line)}[/]")
        if not result.ok:
            failures += 1
    state.save()
    done = len(plans) - failures
    console.print(f"\n{done} backed up, {failures} failed." if failures else f"\n[green]{done} backed up.[/]")
    if failures:
        raise typer.Exit(1)


def choose(projects: List[Project]) -> List[Project]:
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    table.add_column("#", justify="right")
    table.add_column("Status", no_wrap=True)
    table.add_column("Project")
    table.add_column("What needs attention", overflow="fold")
    for number, project in enumerate(projects, start=1):
        top = max(project.issues, key=lambda i: i.severity)
        table.add_row(str(number), status_cell(project), escape(project.name), escape(top.message))
    console.print(table)
    while True:
        answer = Prompt.ask("Which projects? Numbers separated by commas, or [bold]all[/]", default="all")
        answer = answer.strip().lower()
        if answer in ("all", "a", "*"):
            return projects
        try:
            numbers = {int(part) for part in answer.replace(" ", "").split(",") if part}
        except ValueError:
            console.print("[yellow]Write numbers like 1,3,4 or all.[/]")
            continue
        if not numbers or any(n < 1 or n > len(projects) for n in numbers):
            console.print(f"[yellow]Choose numbers between 1 and {len(projects)}.[/]")
            continue
        return [projects[n - 1] for n in sorted(numbers)]


def show_plans(plans: List[Plan]) -> None:
    for plan in plans:
        console.print(f"\n[bold]{escape(plan.project.name)}[/] [dim]{escape(short_path(plan.project.path))}[/]")
        for number, step in enumerate(plan.steps, start=1):
            console.print(f"  {number}. {escape(step)}")
    console.print()


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

    at_risk = [p for p in projects if p.at_risk]
    state.record_check(time.time(), len(at_risk), len(projects))
    state.save()
    logger.info("check: %d projects, %d at risk (%.1fs)", len(projects), len(at_risk), stats.seconds)

    if send_notification and at_risk:
        names = ", ".join(p.name for p in at_risk[:3])
        more = f" and {len(at_risk) - 3} more" if len(at_risk) > 3 else ""
        title = f"{len(at_risk)} project{'s are' if len(at_risk) != 1 else ' is'} not backed up"
        if not notify(f"keepcode: {title}", f"{names}{more}. Run keepcode backup."):
            logger.warning("could not show a notification")
    if not quiet:
        render(projects, show_all=False)
        summary(projects, stats, show_all=False)
    raise typer.Exit(1 if at_risk else 0)


@app.command()
def login(
    token: Optional[str] = typer.Option(
        None, "--token", help="The token. When omitted, it is asked without being shown."
    ),
) -> None:
    """Save a GitHub token so keepcode can create private repositories."""
    if token is None:
        console.print(
            "keepcode needs a GitHub token with permission to create repositories.\n"
            "1. Open [link]https://github.com/settings/tokens/new?scopes=repo&description=keepcode[/link]\n"
            "2. Choose an expiration date and click [bold]Generate token[/]\n"
            "3. Copy the token and paste it below\n"
        )
        token = Prompt.ask("GitHub token", password=True).strip()
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
    console.print(
        f"[green]✔[/] Logged in as [bold]{escape(user.get('login', '?'))}[/]. Token saved in the system keychain."
    )


@app.command()
def logout() -> None:
    """Remove the token saved with keepcode login."""
    if gh.delete_token():
        console.print("[green]✔[/] Token removed from the system keychain.")
    else:
        console.print("No saved token found.")


@app.command()
def doctor() -> None:
    """Check that keepcode has everything it needs."""
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, show_header=False)
    table.add_column("Check", style="bold")
    table.add_column("Result", overflow="fold")
    ok, warn, bad = "[green]✔[/]", "[yellow]![/]", "[red]✖[/]"

    version = git.git_version()
    table.add_row(
        "git", f"{ok} {version}" if version else f"{bad} not found, get it from https://git-scm.com/downloads"
    )
    table.add_row("Python", f"{ok} {platform.python_version()} on {platform.system()}")

    token = gh.find_token()
    if token is None:
        table.add_row("GitHub", f"{warn} no token (only needed to create repositories): run keepcode login")
    else:
        try:
            login_name = gh.GitHub(token.value).user().get("login")
            table.add_row("GitHub", f"{ok} logged in as {login_name} ({token.source})")
        except gh.GitHubError as exc:
            table.add_row("GitHub", f"{bad} token from {token.source} failed: {exc}")

    config = Config.load()
    for root in config.roots:
        exists = Path(os.path.expanduser(root)).is_dir()
        table.add_row("Root", f"{ok if exists else bad} {escape(root)}")
    if config.ignore:
        count = len(config.ignore)
        table.add_row("Ignored", f"{count} folder{'s' if count != 1 else ''}")
    table.add_row("Auto backup", "on" if config.auto_backup else "off")

    try:
        status = sched.status()
        table.add_row("Schedule", f"{ok if status.enabled else warn} {status.detail}")
    except sched.ScheduleError as exc:
        table.add_row("Schedule", f"{warn} unknown ({exc})")

    last = State.load().data.get("last_check")
    if last:
        table.add_row(
            "Last check", f"{time_ago(last.get('time'))}: {last.get('at_risk')} of {last.get('total')} at risk"
        )
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
    if changed:
        config.save()
        console.print("[green]✔[/] Settings saved.")
    console.print(f"Search depth: {config.max_depth}")
    console.print(f"Auto backup in the daily check: {'on' if config.auto_backup else 'off'}")
    console.print(f"Roots: {', '.join(config.roots)}")
    console.print(f"Config file: {config_dir() / 'config.json'}")


# --------------------------------------------------------------- sub-apps


@roots_app.command("list")
def roots_list() -> None:
    """Show the folders keepcode searches."""
    for root in Config.load().roots:
        console.print(escape(root))


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
            console.print(f"[green]+[/] {escape(str(resolved))}")
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
    console.print("[green]✔[/] Removed.")


@roots_app.command("reset")
def roots_reset() -> None:
    """Search the whole home folder again (the default)."""
    config = Config.load()
    config.roots = [str(Path.home())]
    config.save()
    console.print(f"[green]✔[/] Roots reset to {escape(str(Path.home()))}")


@ignore_app.command("list")
def ignore_list() -> None:
    """Show the folders keepcode skips."""
    ignored = Config.load().ignore
    if not ignored:
        console.print("No folders are ignored.")
    for folder in ignored:
        console.print(escape(folder))


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
            console.print(f"[green]+[/] {escape(resolved)}")
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
    console.print("[green]✔[/] Removed.")


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
    console.print(f"[green]✔[/] {detail}")


@schedule_app.command("off")
def schedule_off() -> None:
    """Stop the daily check."""
    try:
        removed = sched.disable()
    except sched.ScheduleError as exc:
        raise fail(str(exc)) from None
    console.print("[green]✔[/] Daily check removed." if removed else "No daily check was scheduled.")


@schedule_app.command("status")
def schedule_status() -> None:
    """Show whether the daily check is on."""
    try:
        console.print(sched.status().detail)
    except sched.ScheduleError as exc:
        raise fail(str(exc)) from None


def main() -> None:
    app()
