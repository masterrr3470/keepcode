"""Keep keepcode up to date from the GitHub Releases of the project.

Each release published on GitHub has a Windows executable and a SHA256SUMS.txt
file, both built by GitHub Actions. When keepcode runs as that executable, it
can replace itself: the new file is downloaded next to the current one, its
checksum is verified, and only then the two are swapped. Windows allows
renaming a running program, so the old file is renamed and removed on the next
start.

When keepcode was installed with pip or pipx there is nothing to replace, so
keepcode only says which command updates it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from keepcode import __version__

REPOSITORY = "masterrr3470/keepcode"
LATEST_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
WINDOWS_ASSET = "keepcode-windows-x64.exe"
CHECKSUMS_ASSET = "SHA256SUMS.txt"
CHUNK = 256 * 1024


class UpdateError(RuntimeError):
    pass


@dataclass
class Release:
    version: str
    url: str
    assets: dict[str, str] = field(default_factory=dict)

    @property
    def has_windows_build(self) -> bool:
        return WINDOWS_ASSET in self.assets and CHECKSUMS_ASSET in self.assets


def parse_version(text: str) -> tuple[int, ...] | None:
    """'v1.2.3' -> (1, 2, 3). Pre-releases such as 1.2.0-beta are ignored."""
    text = text.strip().lstrip("vV")
    if not text or "-" in text or "+" in text:
        return None
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return None


def is_newer(candidate: str, current: str = __version__) -> bool:
    new, old = parse_version(candidate), parse_version(current)
    return new is not None and old is not None and new > old


def is_frozen() -> bool:
    """True when keepcode runs as the standalone executable."""
    return bool(getattr(sys, "frozen", False))


def executable() -> Path:
    return Path(sys.executable).resolve()


def _get(url: str, timeout: float):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"keepcode/{__version__}"},
    )
    return urllib.request.urlopen(request, timeout=timeout)


def latest_release(timeout: float = 3.0, opener=_get) -> Release | None:
    """The newest published release, or None when it cannot be checked (offline, rate limit)."""
    if opener is _get and os.environ.get("KEEPCODE_OFFLINE"):
        return None
    try:
        with opener(LATEST_URL, timeout) as response:
            data = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    tag = str(data.get("tag_name", ""))
    if parse_version(tag) is None:
        return None
    assets = {
        str(asset.get("name")): str(asset.get("browser_download_url"))
        for asset in data.get("assets", [])
        if isinstance(asset, dict) and asset.get("name") and asset.get("browser_download_url")
    }
    return Release(tag.lstrip("vV"), str(data.get("html_url", "")), assets)


def available_update(timeout: float = 3.0, opener=_get) -> Release | None:
    release = latest_release(timeout, opener)
    if release is None or not is_newer(release.version):
        return None
    if is_frozen() and not release.has_windows_build:
        return None  # the build is still being published
    return release


def update_command() -> str:
    """How to update an installation that keepcode cannot replace by itself."""
    if "pipx" in str(Path(sys.executable)).lower():
        return "pipx upgrade keepcode"
    return f"pip install --upgrade git+https://github.com/{REPOSITORY}"


def parse_checksums(text: str) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and len(parts[0]) == 64:
            sums[parts[1].lstrip("*")] = parts[0].lower()
    return sums


def install(release: Release, target: Path | None = None, opener=_get, on_progress=None) -> Path:
    """Download the new executable, verify it and swap it in. Returns its path."""
    target = target or executable()
    if not release.has_windows_build:
        raise UpdateError("This release has no Windows build yet")
    try:
        with opener(release.assets[CHECKSUMS_ASSET], 20) as response:
            expected = parse_checksums(response.read().decode("utf-8", "replace")).get(WINDOWS_ASSET)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise UpdateError(f"Could not download the checksums: {exc}") from exc
    if not expected:
        raise UpdateError("The release has no checksum for the Windows build")

    new_file = target.with_name(target.name + ".new")
    digest = hashlib.sha256()
    try:
        with opener(release.assets[WINDOWS_ASSET], 60) as response, open(new_file, "wb") as handle:
            total = int(response.headers.get("Content-Length") or 0) if hasattr(response, "headers") else 0
            done = 0
            while True:
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(done, total)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        with contextlib.suppress(OSError):
            new_file.unlink()
        raise UpdateError(f"Download failed: {exc}") from exc

    if digest.hexdigest() != expected:
        with contextlib.suppress(OSError):
            new_file.unlink()
        raise UpdateError("The download does not match its checksum, so it was discarded")

    old_file = target.with_name(target.name + ".old")
    with contextlib.suppress(OSError):
        old_file.unlink()
    try:
        os.replace(target, old_file)  # a running program can be renamed on Windows
        os.replace(new_file, target)
    except OSError as exc:
        with contextlib.suppress(OSError):
            if not target.exists() and old_file.exists():
                os.replace(old_file, target)
        with contextlib.suppress(OSError):
            new_file.unlink()
        raise UpdateError(f"Could not replace {target.name}: {exc}") from exc
    return target


def remove_leftovers(target: Path | None = None) -> None:
    """Delete the previous version kept by the last update."""
    if not is_frozen() and target is None:
        return
    target = target or executable()
    for suffix in (".old", ".new"):
        with contextlib.suppress(OSError):
            target.with_name(target.name + suffix).unlink()


def restart(target: Path, args: list[str]) -> int:
    """Run the updated program in this same window and return its exit code."""
    env = dict(os.environ, KEEPCODE_JUST_UPDATED=__version__)
    return subprocess.call([str(target), *args], env=env)
