"""The Python side of the app window. Nothing here opens a window or needs pywebview."""

import os
from pathlib import Path

from conftest import new_repo, write

from keepcode.app.api import SECRETS_ADVICE, Api, project_id, result_dict
from keepcode.backup import Result
from keepcode.config import Config
from keepcode.models import Project

TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def make_api(creator=None, login=None) -> Api:
    return Api(creator_factory=lambda: (creator, login), background=False)


def scanned(api: Api) -> dict:
    assert api.start_scan()
    return api.poll(-1)


def by_name(answer: dict, name: str) -> dict:
    return next(p for p in answer["projects"] if p["name"] == name)


def test_init_describes_the_app(home: Path):
    info = make_api().init()
    assert info["git"] is True
    assert info["smoke"] is False
    assert info["home"] == "~"
    assert info["settings"]["roots"] == ["~"]
    assert info["settings"]["autoUpdate"] == "ask"


def test_scan_finds_projects_and_sends_them_once(home: Path):
    write(home / "site" / "package.json", "{}")
    new_repo(home / "notes")
    api = make_api()
    answer = scanned(api)
    assert answer["scan"]["done"] and not answer["scan"]["running"]
    site = by_name(answer, "site")
    assert site["severity"] == "critical"
    assert site["fixable"] and site["createsRepo"]
    assert site["plan"] == "new private GitHub repository"
    assert site["path"] == "~" + os.sep + "site"
    # Polling again with the same revision does not resend the list.
    assert "projects" not in api.poll(answer["rev"])


def test_backup_without_login_asks_for_one(home: Path):
    write(home / "site" / "package.json", "{}")
    api = make_api(creator=None)
    site = by_name(scanned(api), "site")
    assert api.start_backup([site["id"]]) == {"started": False, "reason": "login"}


def test_backup_reports_results_and_refreshes_the_project(home: Path, creator):
    write(home / "site" / "package.json", "{}")
    write(home / "site" / "index.js", "console.log('hi')\n")
    api = make_api(creator, "me")
    site = by_name(scanned(api), "site")
    assert api.start_backup([site["id"]]) == {"started": True}
    answer = api.poll(-1)
    assert answer["backup"]["finished"] and not answer["backup"]["running"]
    result = answer["backup"]["results"][site["id"]]
    assert result["ok"] and result["summary"] == "Backed up to me/site"
    # Routine lines (.gitignore, first commit, URL) stay out of the window.
    assert result["details"] == []
    assert by_name(answer, "site")["severity"] != "critical"
    assert answer["account"] == {"state": "connected", "login": "me"}


def test_secrets_block_with_window_wording(home: Path, creator):
    write(home / "bot" / "package.json", "{}")
    write(home / "bot" / "index.js", f"client.login('{TOKEN}')\n")
    write(home / "site" / "package.json", "{}")
    api = make_api(creator, "me")
    answer = scanned(api)
    bot, site = by_name(answer, "bot"), by_name(answer, "site")
    api.start_backup([bot["id"], site["id"]])
    result = api.poll(-1)["backup"]["results"][bot["id"]]
    assert not result["ok"] and result["blocked"]
    assert result["details"][-1] == SECRETS_ADVICE
    assert not any("--allow-secrets" in line for line in result["details"])
    assert TOKEN not in "\n".join(result["details"])
    # "Back up anyway" sends allow_secrets, and earlier results stay until the next scan.
    api.start_backup([bot["id"]], allow_secrets=True)
    results = api.poll(-1)["backup"]["results"]
    assert results[bot["id"]]["ok"]
    assert results[site["id"]]["ok"]


def test_result_dict_rewrites_terminal_hints(home: Path):
    project = Project(path=home / "p", name="p")
    no_login = result_dict(Result(project, False, "A GitHub token is needed to create the repository (keepcode login)"))
    assert no_login["summary"] == "Connect GitHub to create the repository"
    partly = result_dict(
        Result(project, True, "Saved to origin", ["Uncommitted work not saved: secret (see keepcode backup --help)"])
    )
    assert partly["details"] == ["Uncommitted work not saved: secret"]


def test_skipping_a_project_removes_it_and_saves_the_folder(home: Path):
    write(home / "site" / "package.json", "{}")
    api = make_api()
    site = by_name(scanned(api), "site")
    settings = api.ignore_project(site["id"])
    assert settings["ignore"] == ["~" + os.sep + "site"]
    assert all(p["id"] != site["id"] for p in api.poll(-1)["projects"])
    assert Config.load().ignored_keys()
    assert api.unignore(settings["ignoreFull"][0])["ignore"] == []


def test_settings_are_validated_and_saved(home: Path):
    api = make_api()
    assert api.settings_set({"autoUpdate": "auto", "autoBackup": True})["autoUpdate"] == "auto"
    assert api.settings_set({"autoUpdate": "sometimes"})["autoUpdate"] == "auto"
    config = Config.load()
    assert config.auto_update == "auto" and config.auto_backup is True


def test_the_last_root_cannot_be_removed(home: Path):
    api = make_api()
    assert api.remove_root(str(home))["roots"] == ["~"]


def test_open_url_only_opens_web_links(home: Path, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url, new=0: opened.append(url) or True)
    api = make_api()
    assert api.open_url("https://github.com/masterrr3470/keepcode")
    assert not api.open_url("file:///C:/Windows/System32/calc.exe")
    assert not api.open_url("javascript:alert(1)")
    assert opened == ["https://github.com/masterrr3470/keepcode"]


def test_login_without_a_client_id_falls_back_to_a_token(home: Path, monkeypatch):
    from keepcode import github as gh

    monkeypatch.setattr(gh, "DEFAULT_CLIENT_ID", "")
    assert make_api().login_start() == {"state": "token"}
    assert make_api().login_token("  ")["state"] == "error"


def test_project_ids_are_stable(home: Path):
    assert project_id(home / "a") == project_id(home / "a")
    assert project_id(home / "a") != project_id(home / "b")


def test_smoke_ok_is_recorded(home: Path):
    api = make_api()
    assert not api._smoke.is_set()
    assert api.smoke_ok() and api._smoke.is_set()


def test_window_reports_when_it_cannot_open(home: Path, monkeypatch):
    import builtins

    from keepcode.app import window

    real_import = builtins.__import__

    def no_webview(name, *args, **kwargs):
        if name == "webview" or name.startswith("webview."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_webview)
    try:
        window.load_engine()
    except window.AppUnavailable as exc:
        assert "pywebview" in str(exc)
    else:
        raise AssertionError("load_engine should fail without pywebview")


def test_web_files_ship_with_the_package():
    from keepcode.app.window import icon_path, web_root

    root = web_root()
    for name in ("index.html", "app.css", "app.js", "fonts/plex-sans-400.woff2"):
        assert (root / name).is_file(), name
    assert icon_path() is not None
