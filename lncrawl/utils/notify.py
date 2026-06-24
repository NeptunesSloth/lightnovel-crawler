"""Best-effort "your run finished" notifications.

Two independent channels, both fire-and-forget and never raise into the caller:

- ``desktop_notify`` — a native OS notification on the machine running the app
  (useful for the local/desktop build). Uses only tools that ship with each OS,
  so there is no extra dependency.
- ``webhook_notify`` — POSTs a short message to a Discord/Slack-style incoming
  webhook so a phone can get pinged while a long run completes unattended.
"""

import logging
import platform
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

_NOTIFY_TIMEOUT = 8.0


def desktop_notify(title: str, message: str) -> bool:
    """Show a native desktop notification. Returns True if it was dispatched.

    Best-effort: any failure (no GUI, missing tool, headless server) is swallowed
    and logged at debug level.
    """
    system = platform.system()
    try:
        if system == "Darwin":
            text = message.replace('"', "'")
            head = title.replace('"', "'")
            script = f'display notification "{text}" with title "{head}"'
            subprocess.run(
                ["osascript", "-e", script],
                timeout=_NOTIFY_TIMEOUT,
                check=False,
            )
            return True

        if system == "Linux":
            import shutil

            if not shutil.which("notify-send"):
                return False
            subprocess.run(
                ["notify-send", title, message],
                timeout=_NOTIFY_TIMEOUT,
                check=False,
            )
            return True

        if system == "Windows":
            return _windows_toast(title, message)
    except Exception as e:
        logger.debug(f"desktop_notify failed: {e}")
    return False


def _windows_toast(title: str, message: str) -> bool:
    """Show a Windows balloon notification via a stock PowerShell snippet.

    Uses ``System.Windows.Forms.NotifyIcon`` which is available on a default
    Windows install (no BurntToast module required). Launched detached so the
    short-lived balloon does not block the caller.
    """
    safe_title = title.replace("'", "''")
    safe_message = message.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        "$n.BalloonTipTitle = '" + safe_title + "';"
        "$n.BalloonTipText = '" + safe_message + "';"
        "$n.Visible = $true;"
        "$n.ShowBalloonTip(8000);"
        "Start-Sleep -Seconds 8;"
        "$n.Dispose()"
    )
    creationflags = 0
    creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-WindowStyle",
            "Hidden",
            "-Command",
            script,
        ],
        creationflags=creationflags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True


def webhook_notify(url: str, message: str) -> bool:
    """POST a short message to a Discord/Slack-style incoming webhook.

    Sends both ``content`` (Discord) and ``text`` (Slack) keys so a single call
    works with either; the other service ignores the key it does not use.
    Returns True on a 2xx response. Best-effort: failures are swallowed.
    """
    if not url:
        return False
    try:
        import requests

        resp = requests.post(
            url,
            json={"content": message, "text": message},
            timeout=_NOTIFY_TIMEOUT,
        )
        ok = 200 <= resp.status_code < 300
        if not ok:
            logger.debug(f"webhook_notify got HTTP {resp.status_code}")
        return ok
    except Exception as e:
        logger.debug(f"webhook_notify failed: {e}")
        return False


def notify_finished(title: str, message: str, webhook_url: Optional[str], desktop: bool) -> None:
    """Fire both configured finish-notification channels, best-effort."""
    if desktop:
        desktop_notify(title, message)
    if webhook_url:
        webhook_notify(webhook_url, message)
