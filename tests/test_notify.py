"""Tests for best-effort local desktop notifications."""

import base64
import subprocess
import sys
from unittest.mock import patch

from worsaga import notify
from worsaga.notify import notification_backend, send_notification


def _ok(args=()):
    return subprocess.CompletedProcess(args=list(args), returncode=0,
                                       stdout="", stderr="")


def _fail(stderr="boom"):
    return subprocess.CompletedProcess(args=[], returncode=1,
                                       stdout="", stderr=stderr)


class TestBackendDetection:
    def test_windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(notify.shutil, "which",
                            lambda name: "C:\\ps.exe" if name == "powershell" else None)
        assert notification_backend() == "windows-toast"

    def test_macos(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(notify.shutil, "which",
                            lambda name: "/usr/bin/osascript" if name == "osascript" else None)
        assert notification_backend() == "macos-notification"

    def test_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(notify.shutil, "which",
                            lambda name: "/usr/bin/notify-send" if name == "notify-send" else None)
        assert notification_backend() == "linux-notify-send"

    def test_nothing_available(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(notify.shutil, "which", lambda name: None)
        assert notification_backend() is None


class TestSendNotification:
    def test_no_backend(self):
        with patch.object(notify, "notification_backend", return_value=None):
            result = send_notification("t", "b")
        assert result["sent"] is False
        assert result["backend"] is None
        assert "no notification backend" in result["error"]

    def test_success(self):
        with patch.object(notify, "notification_backend",
                          return_value="linux-notify-send"), \
             patch.object(notify, "_run", return_value=_ok()) as run:
            result = send_notification("Title", "Body")
        assert result == {"sent": True, "backend": "linux-notify-send"}
        args = run.call_args[0][0]
        assert args[0] == "notify-send"
        assert args[-2:] == ["Title", "Body"]

    def test_nonzero_exit(self):
        with patch.object(notify, "notification_backend",
                          return_value="linux-notify-send"), \
             patch.object(notify, "_run", return_value=_fail("no dbus")):
            result = send_notification("t", "b")
        assert result["sent"] is False
        assert result["error"] == "no dbus"

    def test_timeout_is_not_fatal(self):
        with patch.object(notify, "notification_backend",
                          return_value="linux-notify-send"), \
             patch.object(notify, "_run",
                          side_effect=subprocess.TimeoutExpired("x", 10)):
            result = send_notification("t", "b")
        assert result["sent"] is False

    def test_oserror_is_not_fatal(self):
        with patch.object(notify, "notification_backend",
                          return_value="linux-notify-send"), \
             patch.object(notify, "_run", side_effect=OSError("gone")):
            result = send_notification("t", "b")
        assert result["sent"] is False
        assert result["error"] == "gone"


class TestWindowsToast:
    def _decode(self, run_args):
        encoded = run_args[run_args.index("-EncodedCommand") + 1]
        return base64.b64decode(encoded).decode("utf-16-le")

    def test_encoded_command_contains_escaped_content(self):
        with patch.object(notify, "notification_backend",
                          return_value="windows-toast"), \
             patch.object(notify, "_run", return_value=_ok()) as run:
            result = send_notification(
                "Worsaga: 2 changes", 'ECON101 & "PS3" <due>',
            )
        assert result["sent"] is True
        script = self._decode(run.call_args[0][0])
        assert "Worsaga: 2 changes" in script
        # Markup-significant characters arrive escaped, never literal.
        assert "&amp;" in script and "&lt;due&gt;" in script
        assert "&quot;PS3&quot;" in script
        assert "<due>" not in script

    def test_toast_injection_is_neutralized(self):
        with patch.object(notify, "notification_backend",
                          return_value="windows-toast"), \
             patch.object(notify, "_run", return_value=_ok()) as run:
            send_notification("</text></binding>'; Bad", "b")
        script = self._decode(run.call_args[0][0])
        assert "</text></binding>'" not in script
        assert "&lt;/text&gt;" in script
        assert "&apos;" in script


class TestMacosSender:
    def test_argv_transport(self):
        with patch.object(notify, "notification_backend",
                          return_value="macos-notification"), \
             patch.object(notify, "_run", return_value=_ok()) as run:
            send_notification('say "hi"', "it's done")
        args = run.call_args[0][0]
        assert args[0] == "osascript"
        # Content is argv data, never spliced into the script source.
        assert args[-2:] == ['say "hi"', "it's done"]
        for part in args[:-2]:
            assert "hi" not in part
