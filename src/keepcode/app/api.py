"""What the app window can ask keepcode to do.

pywebview exposes the public methods of `Api` to the page as
`window.pywebview.api.<name>(...)`, each returning a Promise. Every call runs on
its own thread, so long work (scanning, backing up, logging in, updating) starts
a worker thread and the page reads the progress with `poll()`.

Nothing in this module imports pywebview, so it can be tested anywhere.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from keepcode import __version__, git, updater
from keepcode import github as gh
from keepcode import schedule as sched
from keepcode.analysis import analyze, analyze_many, sort_projects
from keepcode.backup import Engine, Result, describe, make_plan
from keepcode.config import Config, State, normalize, pretty_path
from keepcode.fmt import time_ago
from keepcode.models import Project
from keepcode.scanner import find_projects

ROUTINE_DETAILS = (".gitignore: added", "First commit created", "Private repository:")


class Cancelled(Exception):
    pass


def project_id(path: Path) -> str:
    return hashlib.sha1(normalize(path).encode("utf-8", "replace")).hexdigest()[:12]


def project_dict(project: Project) -> dict[str, Any]:
    plan = make_plan(project) if project.fixable else None
    return {
        "id": project_id(project.path),
        "name": project.name,
        "path": pretty_path(project.path),
        "stacks": project.stacks[:4],
        "severity": project.severity.name.lower(),
        "status": project.severity.label,
        "issues": [
            {"kind": i.kind.value, "severity": i.severity.name.lower(), "message": i.message}
            for i in sorted(project.issues, key=lambda i: -i.severity)
        ],
        "lastChange": time_ago(project.last_activity),
        "fixable": project.fixable,
        "copyOf": pretty_path(project.copy_of) if project.copy_of else None,
        "plan": describe(plan) if plan else None,
        "createsRepo": bool(plan and plan.creates_repo),
    }


SECRETS_ADVICE = (
    "Move them to a .env file or add the comment keepcode:allow on that line. "
    "The repository is private, so you can also back up anyway."
)


def _gui_line(line: str) -> str | None:
    """Rewrites the terminal advice in a result for the window app."""
    if line.startswith("Move them to a .env file"):
        return None
    if line.endswith("(see keepcode backup --help)"):
        return line.removesuffix("(see keepcode backup --help)").rstrip()
    return line


def result_dict(result: Result) -> dict[str, Any]:
    blocked = result.summary.lower().startswith(("stopped: secrets", "partly saved"))
    details = []
    for line in result.details:
        if result.ok and line.startswith(ROUTINE_DETAILS):
            continue
        shown = _gui_line(line)
        if shown:
            details.append(shown)
    if blocked:
        details.append(SECRETS_ADVICE)
    summary = result.summary
    if summary.endswith("(keepcode login)"):
        summary = "Connect GitHub to create the repository"
    return {
        "ok": result.ok,
        "summary": summary,
        "details": details,
        "url": result.url,
        "blocked": blocked,
    }


def default_creator() -> tuple[gh.GitHubCreator | None, str | None]:
    """The GitHub connection, or (None, None) when there is no working login."""
    token = gh.find_token()
    if token is None:
        return None, None
    client = gh.GitHub(token.value)
    try:
        login = str(client.user().get("login", ""))
    except gh.GitHubError:
        return None, None
    return gh.GitHubCreator(client), login


class Api:
    def __init__(
        self,
        creator_factory: Callable[[], tuple[Any, str | None]] = default_creator,
        background: bool = True,
    ) -> None:
        self._lock = threading.RLock()
        self._window: Any = None
        self._creator_factory = creator_factory
        self._background = background
        self._config = Config.load()
        self._state = State.load()
        self._projects: dict[str, Project] = {}
        self._order: list[str] = []
        self._rev = 0
        self._account: dict[str, Any] = {"state": "checking", "login": None}
        self._scan: dict[str, Any] = {"running": False, "done": False}
        self._backup: dict[str, Any] = {"running": False, "results": {}}
        self._login: dict[str, Any] = {"state": "idle"}
        self._update: dict[str, Any] = {"state": "idle"}
        self._release: updater.Release | None = None
        self._login_cancel = threading.Event()
        self._smoke = threading.Event()
        self._smoke_mode = False
        self.exit_code = 0

    # ------------------------------------------------------------- plumbing

    def _attach(self, window: Any) -> None:
        self._window = window

    def _spawn(self, target: Callable[[], None]) -> None:
        if self._background:
            threading.Thread(target=target, daemon=True).start()
        else:
            target()

    def _settings(self) -> dict[str, Any]:
        config = self._config
        try:
            status = sched.status()
            schedule = {"enabled": status.enabled, "detail": status.detail}
        except sched.ScheduleError as exc:
            schedule = {"enabled": False, "detail": str(exc)}
        return {
            "roots": [pretty_path(r) for r in config.roots],
            "rootsFull": list(config.roots),
            "ignore": [pretty_path(i) for i in config.ignore],
            "ignoreFull": list(config.ignore),
            "maxDepth": config.max_depth,
            "autoBackup": config.auto_backup,
            "autoUpdate": config.auto_update,
            "scheduleTime": config.schedule_time,
            "schedule": schedule,
        }

    # ----------------------------------------------------------------- page

    def init(self) -> dict[str, Any]:
        """Everything the page needs to draw itself. Starts the update and account checks."""
        if not self._smoke_mode:
            self._spawn(self._check_account)
            if self._config.auto_update != "off":
                self._spawn(self._check_update)
        return {
            "smoke": self._smoke_mode,
            "version": __version__,
            "platform": sys.platform,
            "frozen": updater.is_frozen(),
            "git": git.git_available(),
            "gitVersion": git.git_version(),
            "browserLogin": bool(gh.client_id()),
            "home": pretty_path(Path.home()),
            "settings": self._settings(),
            "justUpdated": bool(os.environ.get("KEEPCODE_JUST_UPDATED")),
        }

    def poll(self, rev: int = -1) -> dict[str, Any]:
        """Progress of the running work. Projects are sent only when they changed."""
        with self._lock:
            answer: dict[str, Any] = {
                "rev": self._rev,
                "scan": dict(self._scan),
                "backup": {**self._backup, "results": dict(self._backup.get("results", {}))},
                "login": dict(self._login),
                "update": dict(self._update),
                "account": dict(self._account),
            }
            if rev != self._rev:
                answer["projects"] = [project_dict(self._projects[i]) for i in self._order if i in self._projects]
        return answer

    def smoke_ok(self) -> bool:
        """Called by the page once it loaded and reached Python (used by the build check)."""
        self._smoke.set()
        return True

    def set_theme(self, dark: bool) -> bool:
        if self._window is not None:
            from keepcode.app.window import style_title_bar

            style_title_bar(self._window, bool(dark))
        return True

    # ------------------------------------------------------------- account

    def _check_account(self) -> None:
        _creator, login = self._creator_factory()
        with self._lock:
            self._account = {"state": "connected" if login else "disconnected", "login": login}

    def logout(self) -> dict[str, Any]:
        gh.delete_token()
        with self._lock:
            self._account = {"state": "disconnected", "login": None}
        return dict(self._account)

    def login_start(self) -> dict[str, Any]:
        app_id = gh.client_id()
        if not app_id:
            with self._lock:
                self._login = {"state": "token"}
            return dict(self._login)
        try:
            code = gh.start_device_flow(app_id)
        except gh.GitHubError as exc:
            with self._lock:
                self._login = {"state": "error", "error": str(exc)}
            return dict(self._login)
        copied = gh.copy_to_clipboard(code.user_code)
        self._login_cancel.clear()
        with self._lock:
            self._login = {
                "state": "waiting",
                "code": code.user_code,
                "uri": code.verification_uri,
                "copied": copied,
            }

        def wait() -> None:
            def sleep(seconds: float) -> None:
                if self._login_cancel.wait(seconds):
                    raise Cancelled

            try:
                token = gh.wait_for_token(app_id, code, sleep=sleep)
                self._finish_login(token)
            except Cancelled:
                with self._lock:
                    self._login = {"state": "idle"}
            except gh.GitHubError as exc:
                with self._lock:
                    self._login = {"state": "error", "error": str(exc)}

        self._spawn(wait)
        return dict(self._login)

    def login_cancel(self) -> bool:
        self._login_cancel.set()
        with self._lock:
            self._login = {"state": "idle"}
        return True

    def login_token(self, token: str) -> dict[str, Any]:
        token = (token or "").strip()
        if not token:
            return {"state": "error", "error": "Paste a token first."}
        try:
            self._finish_login(token)
        except gh.GitHubError as exc:
            with self._lock:
                self._login = {"state": "error", "error": str(exc)}
        return dict(self._login)

    def _finish_login(self, token: str) -> None:
        try:
            login = str(gh.GitHub(token).user().get("login", "?"))
        except gh.GitHubError as exc:
            raise gh.GitHubError(0, f"GitHub did not accept the login: {exc}") from exc
        gh.save_token(token)
        with self._lock:
            self._login = {"state": "done", "login": login}
            self._account = {"state": "connected", "login": login}

    # ----------------------------------------------------------------- scan

    def start_scan(self) -> bool:
        with self._lock:
            if self._scan.get("running") or self._backup.get("running"):
                return False
            self._scan = {"running": True, "done": False, "folders": 0, "current": "", "found": 0, "checked": 0}
        self._spawn(self._run_scan)
        return True

    def _run_scan(self) -> None:
        scan = self._scan
        started = time.monotonic()

        class Live:
            def __setattr__(self, name: str, value: Any) -> None:
                if name == "current":
                    value = pretty_path(value)
                scan[name] = value

        try:
            roots = [r for r in self._config.roots if Path(os.path.expanduser(r)).is_dir()]
            found, stats = find_projects(roots, self._config.ignored_keys(), self._config.max_depth, progress=Live())
            scan["phase"] = "check"

            def checked(done: int) -> None:
                scan["checked"] = done

            projects = sort_projects(analyze_many(found, self._state, checked))
            with self._lock:
                self._projects = {project_id(p.path): p for p in projects}
                self._order = [project_id(p.path) for p in projects]
                self._rev += 1
                self._scan = {
                    "running": False,
                    "done": True,
                    "folders": stats.folders,
                    "found": len(projects),
                    "seconds": round(time.monotonic() - started, 1),
                }
                self._backup = {"running": False, "results": {}}
        except Exception as exc:  # noqa: BLE001 - shown in the window
            with self._lock:
                self._scan = {"running": False, "done": True, "error": str(exc)}

    # --------------------------------------------------------------- backup

    def plan(self, ids: list[str]) -> dict[str, Any]:
        with self._lock:
            projects = [self._projects[i] for i in ids if i in self._projects]
        plans = [make_plan(p) for p in projects if p.fixable]
        return {
            "count": len(plans),
            "newRepos": sum(1 for p in plans if p.creates_repo),
            "branches": sum(1 for p in plans if not p.creates_repo),
            "needsLogin": any(p.creates_repo for p in plans) and self._account.get("login") is None,
        }

    def start_backup(self, ids: list[str], allow_secrets: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._backup.get("running") or self._scan.get("running"):
                return {"started": False, "reason": "busy"}
            projects = [self._projects[i] for i in ids if i in self._projects and self._projects[i].fixable]
        if not projects:
            return {"started": False, "reason": "nothing"}
        plans = [make_plan(p) for p in projects]
        creator, login = self._creator_factory()
        if any(p.creates_repo for p in plans) and creator is None:
            return {"started": False, "reason": "login"}
        with self._lock:
            # Results add up until the next scan, so "Back up anyway" keeps the earlier ones.
            previous = self._backup.get("results", {})
            self._backup = {
                "running": True,
                "finished": False,
                "total": len(plans),
                "index": 0,
                "current": None,
                "message": "",
                "results": dict(previous),
            }
            if login:
                self._account = {"state": "connected", "login": login}

        def work() -> None:
            engine = Engine(creator, self._state, allow_secrets=allow_secrets)
            for number, plan in enumerate(plans, start=1):
                pid = project_id(plan.project.path)
                with self._lock:
                    self._backup.update(index=number, current=pid, message="Starting")

                def log(message: str) -> None:
                    with self._lock:
                        self._backup["message"] = message

                engine.log = log
                try:
                    result = engine.run(plan.project)
                    payload = result_dict(result)
                except Exception as exc:  # noqa: BLE001 - one failure must not stop the others
                    payload = {"ok": False, "summary": f"Failed: {exc}", "details": [], "url": None, "blocked": False}
                refreshed = analyze(plan.project.path, self._state)
                with self._lock:
                    self._backup["results"][pid] = payload
                    if pid in self._projects:
                        refreshed.copy_of = self._projects[pid].copy_of
                        self._projects[pid] = refreshed
                    self._rev += 1
            self._state.save()
            with self._lock:
                self._backup.update(running=False, finished=True, current=None, message="")

        self._spawn(work)
        return {"started": True}

    # ------------------------------------------------------------- settings

    def schedule_set(self, enabled: bool, at: str | None = None) -> dict[str, Any]:
        try:
            if enabled:
                when = at or self._config.schedule_time
                sched.enable(when)
                self._config.schedule_time = when
                self._config.save()
            else:
                sched.disable()
        except sched.ScheduleError as exc:
            return {"error": str(exc), **self._settings()}
        return self._settings()

    def settings_set(self, changes: dict[str, Any]) -> dict[str, Any]:
        config = self._config
        if "autoUpdate" in changes and changes["autoUpdate"] in ("ask", "auto", "off"):
            config.auto_update = changes["autoUpdate"]
        if "autoBackup" in changes:
            config.auto_backup = bool(changes["autoBackup"])
        if "maxDepth" in changes:
            config.max_depth = int(changes["maxDepth"])
        config.save()
        return self._settings()

    def pick_root(self) -> dict[str, Any]:
        if self._window is None:
            return self._settings()
        import webview

        chosen = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=str(Path.home()))
        if chosen:
            folder = str(Path(chosen[0]).resolve())
            if normalize(folder) not in {normalize(r) for r in self._config.roots}:
                self._config.roots.append(folder)
                self._config.save()
        return self._settings()

    def remove_root(self, folder: str) -> dict[str, Any]:
        remaining = [r for r in self._config.roots if normalize(r) != normalize(folder)]
        if remaining:
            self._config.roots = remaining
            self._config.save()
        return self._settings()

    def ignore_project(self, pid: str) -> dict[str, Any]:
        with self._lock:
            project = self._projects.pop(pid, None)
            if project is not None:
                self._order = [i for i in self._order if i != pid]
                self._rev += 1
        if project is not None and normalize(project.path) not in self._config.ignored_keys():
            self._config.ignore.append(str(project.path))
            self._config.save()
        return self._settings()

    def unignore(self, folder: str) -> dict[str, Any]:
        self._config.ignore = [i for i in self._config.ignore if normalize(i) != normalize(folder)]
        self._config.save()
        return self._settings()

    # ---------------------------------------------------------------- system

    def open_folder(self, pid: str) -> bool:
        project = self._projects.get(pid)
        if project is None:
            return False
        path = str(project.path)
        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except OSError:
            return False
        return True

    def open_url(self, url: str) -> bool:
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            return False
        return bool(webbrowser.open(url, new=2))

    def copy_text(self, text: str) -> bool:
        return gh.copy_to_clipboard(str(text))

    def install_git(self) -> bool:
        if sys.platform != "win32":
            return self.open_url("https://git-scm.com/downloads")
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "Install Git", "winget", "install", "--id", "Git.Git", "-e",
                 "--source", "winget", "--accept-package-agreements", "--accept-source-agreements"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )  # fmt: skip
        except OSError:
            return self.open_url("https://git-scm.com/downloads")
        return True

    def git_check(self) -> dict[str, Any]:
        return {"git": git.git_available(), "gitVersion": git.git_version()}

    # --------------------------------------------------------------- updates

    def _check_update(self) -> None:
        with self._lock:
            self._update = {"state": "checking"}
        release = updater.latest_release(timeout=5)
        with self._lock:
            if release is None:
                self._update = {"state": "unknown"}
            elif not updater.is_newer(release.version):
                self._update = {"state": "current", "version": release.version}
            else:
                self._release = release
                self._update = {
                    "state": "available",
                    "version": release.version,
                    "url": release.url,
                    "canInstall": updater.is_frozen() and release.has_windows_build,
                    "command": None if updater.is_frozen() else updater.update_command(),
                }
        if self._update.get("canInstall") and self._config.auto_update == "auto":
            self.update_install()

    def update_install(self) -> bool:
        release = self._release
        if release is None or not updater.is_frozen():
            return False
        with self._lock:
            self._update = {**self._update, "state": "downloading", "progress": 0.0}

        def work() -> None:
            def progress(done: int, total: int) -> None:
                with self._lock:
                    self._update["progress"] = round(done / total, 3) if total else 0.0

            try:
                target = updater.install(release, on_progress=progress)
            except updater.UpdateError as exc:
                with self._lock:
                    self._update = {**self._update, "state": "error", "error": str(exc)}
                return
            with self._lock:
                self._update = {**self._update, "state": "restarting"}
            time.sleep(0.8)
            env = dict(os.environ, KEEPCODE_JUST_UPDATED=release.version)
            subprocess.Popen(
                [str(target)],
                env=env,
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                close_fds=True,
            )
            if self._window is not None:
                self._window.destroy()

        self._spawn(work)
        return True
