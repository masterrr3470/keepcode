"""Build keepcode.exe with PyInstaller and check that it runs.

Used by GitHub Actions on Windows; works the same on a Windows PC:

    python -m pip install -e . pyinstaller
    python packaging/build.py

The executable is a console program whose console hides itself when it was
opened with a double-click (`--hide-console hide-early`). keepcode then opens
its window, and the same file still works in a terminal and in the daily check.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "keepcode-windows-x64"
DIST = ROOT / "dist"


def run(*args: str) -> str:
    print("$", " ".join(args), flush=True)
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        print(result.stdout, result.stderr, sep="\n")
        raise SystemExit(f"Command failed with exit code {result.returncode}")
    return result.stdout


def build() -> Path:
    run(sys.executable, "packaging/make_version_file.py")
    run(
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--console",
        "--hide-console", "hide-early",
        "--name", NAME,
        "--icon", "packaging/keepcode.ico",
        "--version-file", "build/version_info.txt",
        # keyring finds its backends through package metadata
        "--copy-metadata", "keyring",
        "--hidden-import", "keyring.backends.Windows",
        "--hidden-import", "keyring.backends.chainer",
        "--hidden-import", "keyring.backends.fail",
        "--exclude-module", "pkg_resources",
        "--exclude-module", "setuptools",
        "--collect-submodules", "keepcode",
        # the app window: its page, fonts and icon, and pywebview on WebView2
        "--collect-data", "keepcode",
        "--hidden-import", "webview.platforms.winforms",
        "--hidden-import", "webview.platforms.edgechromium",
        "--hidden-import", "clr",
        "packaging/keepcode_entry.py",
    )  # fmt: skip
    exe = DIST / f"{NAME}.exe"
    if not exe.exists():
        raise SystemExit(f"{exe} was not created")
    return exe


def smoke_test(exe: Path) -> None:
    """The executable must start, find this repository and talk JSON."""
    version = run(str(exe), "--version").strip()
    print(version)
    data = json.loads(run(str(exe), "scan", "--json", "--path", str(ROOT)))
    if not data["projects"]:
        raise SystemExit("The executable found no projects in the repository")
    run(str(exe), "doctor")
    print("Smoke test passed:", version, "found", len(data["projects"]), "project(s)")


def window_test(exe: Path) -> None:
    """Opens the window, waits for the page to reach Python, and closes it.

    A build machine without a desktop or WebView2 cannot show it, so a failure is
    a warning: double-clicking falls back to the terminal version anyway.
    """
    print("$", exe.name, "app --smoke-test", flush=True)
    try:
        command = [str(exe), "app", "--smoke-test"]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120)
        ok, detail = result.returncode == 0, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        ok, detail = False, "timed out"
    if ok:
        print("Window test passed")
        return
    message = f"The app window did not open on this machine: {detail or 'no output'}"
    if os.environ.get("KEEPCODE_REQUIRE_WINDOW") == "1":
        raise SystemExit(message)
    prefix = "::warning::" if os.environ.get("GITHUB_ACTIONS") else "Warning: "
    print(prefix + message.replace("\n", " "))


def checksums(exe: Path) -> None:
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    (DIST / "SHA256SUMS.txt").write_text(f"{digest}  {exe.name}\n", encoding="utf-8")
    print(f"{digest}  {exe.name}  ({exe.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    executable = build()
    smoke_test(executable)
    window_test(executable)
    checksums(executable)
