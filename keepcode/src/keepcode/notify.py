"""Desktop notifications without extra dependencies."""

from __future__ import annotations

import base64
import shutil
import subprocess
import sys
from xml.sax.saxutils import escape

# Windows shows toasts on behalf of a registered app; PowerShell's own ID is always present.
POWERSHELL_APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"

_TOAST_SCRIPT = """
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('{xml}')
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app}').Show($toast)
"""


def _run(command: list[str]) -> bool:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def windows_script(title: str, message: str) -> str:
    xml = (
        "<toast><visual><binding template='ToastGeneric'>"
        f"<text>{escape(title)}</text><text>{escape(message)}</text>"
        "</binding></visual></toast>"
    )
    # Inside a single-quoted PowerShell string, a quote is written twice.
    return _TOAST_SCRIPT.format(xml=xml.replace("'", "''"), app=POWERSHELL_APP_ID)


def notify(title: str, message: str) -> bool:
    """Show a notification. Returns False when the platform could not show it."""
    if sys.platform == "win32":
        encoded = base64.b64encode(windows_script(title, message).encode("utf-16-le")).decode()
        return _run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded])
    if sys.platform == "darwin":

        def quote(text: str) -> str:
            return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

        return _run(["osascript", "-e", f"display notification {quote(message)} with title {quote(title)}"])
    if shutil.which("notify-send"):
        return _run(["notify-send", "--app-name=keepcode", title, message])
    return False
