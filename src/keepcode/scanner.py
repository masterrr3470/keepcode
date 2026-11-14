"""Walk the disk and find project folders.

A folder is a project when it has version control or a typical project file
(package.json, manage.py, Cargo.toml...). Once a project is found, keepcode does
not look inside it for more projects: monorepos are reported as one project.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from keepcode.config import normalize

MARKERS: dict[str, str] = {
    "package.json": "Node.js",
    "deno.json": "Deno",
    "pyproject.toml": "Python",
    "requirements.txt": "Python",
    "setup.py": "Python",
    "Pipfile": "Python",
    "manage.py": "Django",
    "Cargo.toml": "Rust",
    "go.mod": "Go",
    "pom.xml": "Java",
    "build.gradle": "Java",
    "build.gradle.kts": "Kotlin",
    "composer.json": "PHP",
    "Gemfile": "Ruby",
    "pubspec.yaml": "Flutter",
    "CMakeLists.txt": "C/C++",
    "platformio.ini": "PlatformIO",
    "fxmanifest.lua": "Lua",
    "__resource.lua": "Lua",
    "project.godot": "Godot",
}

SUFFIX_MARKERS: dict[str, str] = {
    ".sln": ".NET",
    ".csproj": ".NET",
    ".ino": "Arduino",
}

# Folders that never contain the user's own projects, skipped wherever they are.
SKIP_NAMES = {
    "node_modules",
    "bower_components",
    "__pycache__",
    "site-packages",
    "venv",
    "env",
    "dist",
    "build",
    "target",
    "out",
    "obj",
    "bin",
    "vendor",
    "coverage",
    "Pods",
    "DerivedData",
    "AppData",
    "Application Data",
    "$RECYCLE.BIN",
    "System Volume Information",
    "Program Files",
    "Program Files (x86)",
    "ProgramData",
    "Windows",
    "OneDriveTemp",
}

# Tool folders that live directly in the home folder.
SKIP_IN_HOME = {
    "Library",
    "go",
    "scoop",
    "snap",
    "anaconda3",
    "miniconda3",
    "Anaconda3",
    "Miniconda3",
    "Applications",
    "Music",
    "Pictures",
    "Videos",
    "Movies",
}

# Big personal folders that are containers, never projects on their own.
CONTAINERS = {
    "desktop",
    "documents",
    "downloads",
    "onedrive",
    "dropbox",
    "google drive",
    "icloud drive",
    "source",
    "repos",
    "projects",
    "projetos",
    "dev",
    "code",
    "github",
}

NODE_FRAMEWORKS = [
    ("next", "Next.js"),
    ("react", "React"),
    ("vue", "Vue"),
    ("svelte", "Svelte"),
    ("@angular/core", "Angular"),
    ("vite", "Vite"),
    ("express", "Express"),
    ("electron", "Electron"),
    ("@tauri-apps/api", "Tauri"),
    ("@tauri-apps/cli", "Tauri"),
    ("typescript", "TypeScript"),
]

PYTHON_FRAMEWORKS = [
    ("django", "Django"),
    ("flask", "Flask"),
    ("fastapi", "FastAPI"),
]

IGNORE_IN_COUNT = SKIP_NAMES | {".git", ".venv", ".next", ".nuxt", ".svelte-kit", ".turbo", ".gradle", "cache"}

# FiveM: generic names of the folder that holds server.cfg and resources/.
GENERIC_SERVER_NAMES = {"server-data", "txdata", "data", "server", "fxserver"}


@dataclass
class ScanStats:
    folders: int = 0
    seconds: float = 0.0


def _is_hidden(name: str) -> bool:
    return name.startswith(".")


def _is_link(entry: os.DirEntry[str]) -> bool:
    if entry.is_symlink():
        return True
    is_junction = getattr(entry, "is_junction", None)
    if is_junction is not None:
        try:
            return bool(is_junction())
        except OSError:
            return True
    return False


def is_fxserver_artifacts(names: set[str]) -> bool:
    """The FXServer program itself (artifacts), which ships its own resources."""
    return "FXServer.exe" in names or {"run.sh", "alpine"} <= names


def is_fivem_server(names: set[str]) -> bool:
    return "server.cfg" in names and "resources" in names


def _is_bracket(name: str) -> bool:
    return name.startswith("[") and name.endswith("]")


def looks_like_project(path: Path, names: set[str]) -> bool:
    if ".git" in names or is_fivem_server(names):
        return True
    if any(marker in names for marker in MARKERS):
        return True
    if any(name.lower().endswith(tuple(SUFFIX_MARKERS)) for name in names):
        return True
    if "Assets" in names and "ProjectSettings" in names:
        return True
    # A static website: an index.html next to its styles or scripts.
    return "index.html" in names and bool(
        {"css", "js", "assets", "styles", "scripts"} & names
        or any(name.lower().endswith((".css", ".js")) for name in names)
    )


def _is_container(path: Path) -> bool:
    name = path.name.lower()
    return name in CONTAINERS or name.startswith("onedrive")


def find_projects(
    roots: Iterable[str | Path],
    ignore: set[str] | None = None,
    max_depth: int = 7,
    on_progress: Callable[[int], None] | None = None,
    progress: Any = None,
) -> tuple[list[Path], ScanStats]:
    """Return project folders under the given roots, sorted by path."""
    started = time.monotonic()
    home = normalize(Path.home())
    ignore = ignore or set()
    stats = ScanStats()
    seen: set[str] = set()
    found: list[Path] = []
    stack: list[tuple[Path, int]] = [(Path(os.path.expanduser(str(r))).absolute(), 0) for r in roots]

    while stack:
        folder, depth = stack.pop()
        key = normalize(folder)
        if key in seen or key in ignore:
            continue
        seen.add(key)
        stats.folders += 1
        if progress is not None:
            progress.folders = stats.folders
            progress.current = str(folder)
            progress.found = len(found)
        if on_progress and stats.folders % 200 == 0:
            on_progress(stats.folders)
        try:
            with os.scandir(folder) as iterator:
                entries = list(iterator)
        except OSError:
            continue
        names = {entry.name for entry in entries}
        if is_fxserver_artifacts(names):
            continue  # FXServer binaries and its bundled resources are not the user's code

        if looks_like_project(folder, names):
            # The home folder and drive roots are always searched, never reported.
            top_level = key == home or folder.parent == folder
            if not top_level and (".git" in names or not _is_container(folder)):
                found.append(folder)
                continue

        if depth >= max_depth:
            continue
        in_home = key == home
        for entry in entries:
            name = entry.name
            if _is_hidden(name) or name in SKIP_NAMES or (in_home and name in SKIP_IN_HOME):
                continue
            try:
                if not entry.is_dir(follow_symlinks=False) or _is_link(entry):
                    continue
            except OSError:
                continue
            stack.append((Path(entry.path), depth + 1))

    stats.seconds = time.monotonic() - started
    projects = _drop_nested(group_fivem(found, home))
    if progress is not None:
        progress.found = len(projects)
    return projects, stats


def fivem_root(path: Path, home: str) -> Path | None:
    """Server folder that a FiveM resource belongs to, or None when it stands alone.

    Resources live under resources/ and in [category] folders; the server is the
    folder that contains the outermost of those.
    """
    parts = path.parts
    for index in range(1, len(parts) - 1):
        part = parts[index]
        if part.lower() == "resources" or _is_bracket(part):
            root = Path(*parts[:index])
            if normalize(root) == home or root.parent == root or _is_container(root):
                return None
            return root
    return None


def group_fivem(paths: list[Path], home: str) -> list[Path]:
    """Report a FiveM server once instead of once per resource."""
    result: list[Path] = []
    for path in paths:
        root = None
        if not (path / ".git").exists() and _has_fivem_manifest(path):
            root = fivem_root(path, home)
        result.append(root or path)
    unique: dict[str, Path] = {}
    for path in result:
        unique.setdefault(normalize(path), path)
    return list(unique.values())


def _has_fivem_manifest(path: Path) -> bool:
    return (path / "fxmanifest.lua").exists() or (path / "__resource.lua").exists()


def display_name(path: Path) -> str:
    """A readable name: "VerticeServidor/server-data" instead of a bare "server-data"."""
    name = path.name or str(path)
    if name.lower() in GENERIC_SERVER_NAMES and path.parent.name and path.parent != path:
        return f"{path.parent.name}/{name}"
    return name


def _drop_nested(paths: list[Path]) -> list[Path]:
    """When two roots overlap, keep only the outermost project."""
    result: list[Path] = []
    keys: list[str] = []
    for path in sorted(paths, key=lambda p: len(str(p))):
        key = normalize(path)
        if any(key == k or key.startswith(k + os.sep) for k in keys):
            continue
        keys.append(key)
        result.append(path)
    return sorted(result, key=lambda p: normalize(p))


def _read_text(path: Path, limit: int = 200_000) -> str:
    try:
        with open(path, encoding="utf-8", errors="ignore") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def _python_frameworks(project: Path) -> list[str]:
    text = "\n".join(
        _read_text(project / name).lower() for name in ("requirements.txt", "pyproject.toml", "Pipfile", "setup.py")
    )
    found = []
    for package, label in PYTHON_FRAMEWORKS:
        if re.search(rf"(^|[\s\"'\[,]){re.escape(package)}($|[\s\"'<>=~!\[\],;])", text, re.M):
            found.append(label)
    return found


def _node_frameworks(project: Path) -> list[str]:
    try:
        data = json.loads(_read_text(project / "package.json"))
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    deps: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            deps.update(section)
    labels = []
    for package, label in NODE_FRAMEWORKS:
        if package in deps and label not in labels:
            labels.append(label)
    return labels


def detect_stacks(project: Path) -> list[str]:
    try:
        names = set(os.listdir(project))
    except OSError:
        return []
    stacks: list[str] = []

    def add(label: str) -> None:
        if label not in stacks:
            stacks.append(label)

    for marker, label in MARKERS.items():
        if marker in names:
            add(label)
    for name in names:
        for suffix, label in SUFFIX_MARKERS.items():
            if name.lower().endswith(suffix):
                add(label)
    if "Django" in stacks:
        add("Python")
    if "Python" in stacks:
        for label in _python_frameworks(project):
            add(label)
    if "Node.js" in stacks:
        for label in _node_frameworks(project):
            add(label)
    if "server.cfg" in names or any(_is_bracket(n) for n in names) or _has_bracket_resources(project):
        add("FiveM")
    if "src-tauri" in names:
        add("Tauri")
    if "Assets" in names and "ProjectSettings" in names:
        add("Unity")
    if not stacks and "index.html" in names:
        add("HTML/CSS")
    # Most specific first: "React, Vite, Node.js" reads better than the reverse.
    generic = {"Node.js", "Python"}
    return [s for s in stacks if s not in generic] + [s for s in stacks if s in generic]


def _has_bracket_resources(project: Path) -> bool:
    try:
        return any(_is_bracket(n) for n in os.listdir(project / "resources"))
    except OSError:
        return False


def count_files(project: Path, limit: int = 100_000) -> tuple[int, float | None]:
    """Count the user's files (dependencies excluded) and find the newest change."""
    count = 0
    newest: float | None = None
    stack = [project]
    while stack and count < limit:
        folder = stack.pop()
        try:
            with os.scandir(folder) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in IGNORE_IN_COUNT and not _is_link(entry):
                                stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            count += 1
                            mtime = entry.stat(follow_symlinks=False).st_mtime
                            if newest is None or mtime > newest:
                                newest = mtime
                    except OSError:
                        continue
        except OSError:
            continue
    return count, newest
