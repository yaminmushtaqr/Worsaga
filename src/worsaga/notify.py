"""Best-effort local desktop notifications.

Sends a platform-native notification where practical and degrades
gracefully everywhere else:

- **Windows** — a toast via PowerShell and the WinRT
  ``ToastNotificationManager`` API (no third-party packages). The
  script is passed with ``-EncodedCommand`` so title/body content can
  never break out of the script, and the XML payload is escaped.
- **macOS** — ``osascript`` ``display notification``; the title and
  body travel as argv items, never spliced into the AppleScript source.
- **Linux** — ``notify-send`` when present.

Every send returns a structured result (``{"sent", "backend", ...}``)
instead of raising: a missing or broken notification stack must never
take down a sync loop. Callers are expected to print their own console
fallback when ``sent`` is false. Notification content is course
metadata only — callers must not put tokens or URLs in it, and all
subprocess calls run without a shell, with a timeout, and with the API
token stripped from the child's environment.

All operations are local. Nothing is sent over the network.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import sys
from xml.sax.saxutils import escape

from worsaga.secureio import child_env

#: Seconds a notification subprocess may run before being abandoned.
NOTIFY_TIMEOUT = 10

#: An AppUserModelID that exists on stock Windows installs; toasts must
#: be raised under a registered app id, and PowerShell's is universal.
_WINDOWS_APP_ID = (
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
    "\\WindowsPowerShell\\v1.0\\powershell.exe"
)

_WINDOWS_TOAST_TEMPLATE = """
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>{title}</text><text>{body}</text></binding></visual></toast>')
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app_id}').Show($toast)
"""


def notification_backend() -> str | None:
    """Return the notification backend for this platform, or None."""
    if sys.platform == "win32":
        if shutil.which("powershell"):
            return "windows-toast"
        return None
    if sys.platform == "darwin":
        if shutil.which("osascript"):
            return "macos-notification"
        return None
    if shutil.which("notify-send"):
        return "linux-notify-send"
    return None


def _run(args: list[str]) -> subprocess.CompletedProcess:
    # env=: the notifier needs the ordinary environment (PATH, DISPLAY,
    # DBUS_SESSION_BUS_ADDRESS, SystemRoot) but never the API token.
    return subprocess.run(
        args, capture_output=True, text=True, timeout=NOTIFY_TIMEOUT,
        env=child_env(),
    )


def _send_windows(title: str, body: str) -> subprocess.CompletedProcess:
    script = _WINDOWS_TOAST_TEMPLATE.format(
        # XML-escape (with quote escaping) so content stays inert both
        # in the toast XML and inside the single-quoted PS string.
        title=escape(title, {"'": "&apos;", '"': "&quot;"}),
        body=escape(body, {"'": "&apos;", '"': "&quot;"}),
        app_id=_WINDOWS_APP_ID,
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return _run([
        "powershell", "-NoProfile", "-NonInteractive",
        "-EncodedCommand", encoded,
    ])


def _send_macos(title: str, body: str) -> subprocess.CompletedProcess:
    # Title/body are argv items read inside the script — no splicing.
    return _run([
        "osascript",
        "-e", "on run argv",
        "-e", "display notification (item 2 of argv)"
              " with title (item 1 of argv)",
        "-e", "end run",
        title, body,
    ])


def _send_linux(title: str, body: str) -> subprocess.CompletedProcess:
    return _run(["notify-send", "--app-name=Worsaga", "--", title, body])


_SENDERS = {
    "windows-toast": _send_windows,
    "macos-notification": _send_macos,
    "linux-notify-send": _send_linux,
}


def send_notification(title: str, body: str) -> dict:
    """Send a local desktop notification, best effort.

    Returns ``{"sent": bool, "backend": str | None, "error": str}``
    (``error`` only when something failed). Never raises.
    """
    backend = notification_backend()
    if backend is None:
        return {
            "sent": False,
            "backend": None,
            "error": "no notification backend available on this platform",
        }
    try:
        proc = _SENDERS[backend](str(title), str(body))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"sent": False, "backend": backend, "error": str(exc)}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return {
            "sent": False,
            "backend": backend,
            "error": detail or f"exit code {proc.returncode}",
        }
    return {"sent": True, "backend": backend}


__all__ = ["NOTIFY_TIMEOUT", "notification_backend", "send_notification"]
