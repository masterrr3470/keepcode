"""Open the keepcode window with pywebview.

On Windows pywebview uses Microsoft Edge WebView2, which comes with Windows 10
and 11, inside a normal application window. The page lives in web/ and talks to
Python through `Api`.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from importlib import resources
from pathlib import Path
from typing import Any

from keepcode.app.api import Api

BACKGROUND = "#0A1224"
SMOKE_TIMEOUT = 60


def _rgb(hex_color: str) -> int:
    """'#RRGGBB' as a Windows COLORREF (0x00BBGGRR)."""
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return r | (g << 8) | (b << 16)


def _own_window() -> int | None:
    """The visible top-level window of this process titled keepcode."""
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    found: list[int] = []
    pid = os.getpid()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _param):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(64)
            user32.GetWindowTextW(hwnd, title, 64)
            if title.value == "keepcode":
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(callback, 0)
    return found[0] if found else None


def style_title_bar(window: Any, dark: bool) -> None:
    """Match the Windows title bar to the page: dark navy, or the system default in light mode."""
    hwnd = _own_window()
    if hwnd is None:
        return
    dwm = ctypes.windll.dwmapi  # type: ignore[attr-defined]

    def set_attribute(attribute: int, value: int) -> None:
        data = ctypes.c_int(value)
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), attribute, ctypes.byref(data), ctypes.sizeof(data))

    default = 0xFFFFFFFF
    set_attribute(20, 1 if dark else 0)  # immersive dark mode (Windows 10 2004+, 11)
    set_attribute(19, 1 if dark else 0)  # the same attribute on older Windows 10 builds
    set_attribute(35, _rgb(BACKGROUND) if dark else default)  # caption colour (Windows 11)
    set_attribute(34, _rgb("#1D2C4A") if dark else default)  # border colour (Windows 11)
    set_attribute(36, _rgb("#C3D0E6") if dark else default)  # title text colour (Windows 11)


def web_root() -> Path:
    return Path(str(resources.files("keepcode.app") / "web"))


def icon_path() -> str | None:
    path = web_root() / "keepcode.ico"
    return str(path) if path.exists() else None


class AppUnavailable(RuntimeError):
    """The window cannot open here. The terminal version still works."""


def load_engine() -> Any:
    """Imports pywebview and makes sure it would use a modern engine."""
    try:
        import webview
    except ImportError as exc:
        raise AppUnavailable('the window needs pywebview: pip install "keepcode[app]"') from exc
    if sys.platform == "win32":
        try:
            from webview.platforms import winforms
        except Exception as exc:  # noqa: BLE001 - .NET or pythonnet problems surface here
            raise AppUnavailable(f"the window could not start ({exc})") from exc
        if winforms.renderer != "edgechromium":
            raise AppUnavailable(
                "Microsoft Edge WebView2 is not installed. Get it from "
                "https://developer.microsoft.com/microsoft-edge/webview2/"
            )
    return webview


def show_console() -> None:
    """Brings back the console window that the executable hides when it starts the window."""
    if sys.platform != "win32":
        return
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()  # type: ignore[attr-defined]
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE: shortcuts start it minimized


def run(smoke_test: bool = False) -> int:
    webview = load_engine()
    api = Api()
    window = webview.create_window(
        "keepcode",
        url=str(web_root() / "index.html"),
        js_api=api,
        width=1180,
        height=800,
        min_size=(920, 640),
        background_color=BACKGROUND,
        text_select=False,
    )
    api._attach(window)

    if smoke_test:
        api._smoke_mode = True
        api.exit_code = 1

        def watch() -> None:
            if api._smoke.wait(SMOKE_TIMEOUT):
                api.exit_code = 0
            window.destroy()

        threading.Thread(target=watch, daemon=True).start()

    try:
        webview.start(http_server=True, icon=icon_path(), debug=bool(os.environ.get("KEEPCODE_DEBUG")))
    except Exception as exc:  # noqa: BLE001 - the caller falls back to the terminal
        raise AppUnavailable(f"the window could not start ({exc})") from exc
    return api.exit_code
