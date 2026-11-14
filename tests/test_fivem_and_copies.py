from pathlib import Path

from conftest import new_repo, write

from keepcode.analysis import analyze_many
from keepcode.models import IssueKind, Severity
from keepcode.scanner import detect_stacks, display_name, find_projects
from keepcode.secretscan import scan_text


def names(paths):
    return sorted(p.name for p in paths)


def resource(path: Path) -> None:
    write(path / "fxmanifest.lua", "fx_version 'cerulean'\n")


def test_fxserver_artifacts_are_skipped(home: Path):
    write(home / "Downloads" / "server" / "artifacts" / "FXServer.exe", "")
    resource(home / "Downloads" / "server" / "artifacts" / "citizen" / "system_resources" / "monitor")
    write(home / "Downloads" / "linux" / "run.sh", "")
    (home / "Downloads" / "linux" / "alpine").mkdir(parents=True)
    resource(home / "Downloads" / "linux" / "alpine" / "opt" / "cfx-server" / "citizen" / "chat")
    found, _ = find_projects([home])
    assert found == []


def test_resources_count_as_one_server(home: Path):
    server = home / "Downloads" / "VerticeServidor" / "server-data"
    write(server / "server.cfg", "")
    resource(server / "resources" / "[treino]" / "treino")
    resource(server / "resources" / "[cfx]" / "mapmanager")
    found, _ = find_projects([home])
    assert found == [server]
    assert display_name(server) == "VerticeServidor/server-data"
    assert "FiveM" in detect_stacks(server)


def test_resources_without_server_cfg_are_grouped(home: Path):
    server = home / "Downloads" / "servidor-treino-pvp" / "servidor-treino-pvp"
    resource(server / "resources" / "[treino]" / "treino_lobby")
    resource(server / "resources" / "[treino]" / "treino_bots")
    copy = home / "Downloads" / "VerticeServidor" / "copia-versao-8.8"
    resource(copy / "[treino]" / "treino")
    resource(copy / "[treino]" / "vertice-carregar")
    found, _ = find_projects([home])
    assert sorted(found) == sorted([server, copy])
    assert "FiveM" in detect_stacks(server)


def test_standalone_resources_stay_separate(home: Path):
    resource(home / "Downloads" / "[solo]" / "myres")  # Downloads is a container, not a server
    repo = new_repo(home / "srv" / "resources" / "[mine]" / "tracked", {"fxmanifest.lua": "x"})
    found, _ = find_projects([home])
    assert sorted(names(found)) == ["myres", "tracked"]
    assert repo in found


def test_copies_are_flagged(home: Path):
    for folder in ("koomo-main", "koomo-main (1)"):
        write(home / "Downloads" / folder / "koomo-main" / "package.json", "{}")
        write(home / "Downloads" / folder / "koomo-main" / "src" / "a.ts", "x")
    write(home / "Downloads" / "other" / "koomo-main" / "package.json", "{}")  # different size
    found, _ = find_projects([home])
    projects = analyze_many(found)
    copies = [p for p in projects if p.copy_of]
    assert len(copies) == 1
    issue = copies[0].get(IssueKind.DUPLICATE)
    assert issue.severity == Severity.INFO and "copy of ~" in issue.message
    assert copies[0].severity == Severity.CRITICAL  # still not backed up


def test_fivem_secrets_are_detected():
    assert scan_text('sv_licenseKey "cfxk_' + "a1B2" * 8 + '"')[0].rule == "Cfx.re license key"
    assert scan_text('rcon_password "hunter22"')[0].rule == "FiveM rcon password"
    assert scan_text('set steam_webApiKey "' + "0123456789ABCDEF" * 2 + '"')[0].rule == "Steam Web API key"
    assert scan_text('# rcon_password ""') == []
    assert scan_text("set steam_webApiKey none") == []
