from pathlib import Path

from conftest import new_repo, write

from keepcode.scanner import count_files, detect_stacks, find_projects


def names(paths):
    return sorted(p.name for p in paths)


def test_finds_projects_by_markers(home: Path):
    write(home / "Downloads" / "api" / "manage.py")
    write(home / "Documents" / "site" / "index.html", "<html></html>")
    write(home / "Documents" / "site" / "style.css", "body{}")
    write(home / "Desktop" / "game" / "Cargo.toml", "[package]")
    new_repo(home / "code" / "repo")
    found, stats = find_projects([home])
    assert names(found) == ["api", "game", "repo", "site"]
    assert stats.folders > 0


def test_skips_dependency_and_tool_folders(home: Path):
    write(home / "AppData" / "Roaming" / "x" / "package.json", "{}")
    write(home / ".vscode" / "extensions" / "y" / "package.json", "{}")
    write(home / "Documents" / "stuff" / "node_modules" / "z" / "package.json", "{}")
    write(home / "Documents" / "stuff" / "venv" / "lib" / "setup.py")
    found, _ = find_projects([home])
    assert found == []


def test_does_not_look_inside_a_project(home: Path):
    project = home / "mono"
    write(project / "package.json", "{}")
    write(project / "packages" / "a" / "package.json", "{}")
    write(project / "backend" / "manage.py")
    found, _ = find_projects([home])
    assert found == [project]


def test_container_folder_with_project_file_is_still_searched(home: Path):
    # A stray package.json in Downloads must not hide the projects inside it.
    write(home / "Downloads" / "package.json", "{}")
    write(home / "Downloads" / "real" / "pyproject.toml")
    found, _ = find_projects([home])
    assert names(found) == ["real"]


def test_home_with_git_is_searched_not_reported(home: Path):
    new_repo(home)  # dotfiles repository in the home folder
    write(home / "projects" / "app" / "go.mod", "module app")
    found, _ = find_projects([home])
    assert names(found) == ["app"]


def test_respects_ignore_and_depth(home: Path):
    write(home / "a" / "b" / "c" / "d" / "deep" / "package.json", "{}")
    write(home / "skip" / "app" / "package.json", "{}")
    from keepcode.config import normalize

    found, _ = find_projects([home], ignore={normalize(home / "skip")}, max_depth=3)
    assert found == []
    found, _ = find_projects([home], max_depth=7)
    assert names(found) == ["app", "deep"]


def test_overlapping_roots_report_each_project_once(home: Path):
    write(home / "work" / "app" / "package.json", "{}")
    found, _ = find_projects([home, home / "work"])
    assert names(found) == ["app"]


def test_detect_stacks(home: Path):
    project = home / "web"
    write(
        project / "package.json",
        '{"dependencies": {"react": "18", "@supabase/supabase-js": "2"}, "devDependencies": {"vite": "5"}}',
    )
    write(project / "requirements.txt", "Django==5.0\ndjangorestframework\n")
    stacks = detect_stacks(project)
    assert {"Django", "React", "Vite", "Node.js", "Python"} <= set(stacks)
    assert stacks[-2:] == ["Node.js", "Python"]  # generic names come last


def test_detect_stacks_does_not_confuse_similar_names(home: Path):
    project = home / "py"
    write(project / "requirements.txt", "flask-cors\ndjangorestframework-stubs\n")
    assert "Flask" not in detect_stacks(project)
    assert "Django" not in detect_stacks(project)


def test_count_files_ignores_dependencies(home: Path):
    project = home / "p"
    write(project / "a.py")
    write(project / "src" / "b.py")
    write(project / "node_modules" / "lib" / "c.js")
    write(project / ".git" / "config")
    count, newest = count_files(project)
    assert count == 2
    assert newest is not None
