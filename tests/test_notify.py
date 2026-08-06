"""Tests for best-effort local desktop notifications."""

import base64
import os
import subprocess
import sys
from unittest.mock import patch

from worsaga import notify
from worsaga.notify import notification_backend, send_notification
from worsaga.redact import REDACTED, remember_secret


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


class TestSubprocessEnvironment:
    """Notifier children get the normal environment minus the token."""

    def _recorded_env(self, monkeypatch):
        recorded = {}

        def fake_run(args, **kwargs):
            recorded.update(kwargs)
            return _ok()

        monkeypatch.setattr(notify.subprocess, "run", fake_run)
        notify._run(["notify-send", "hello"])
        return recorded["env"]

    def test_token_is_stripped(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_TOKEN", "supersecret")
        env = self._recorded_env(monkeypatch)
        assert "WORSAGA_TOKEN" not in env
        assert "supersecret" not in "".join(env.values())

    def test_ordinary_environment_survives(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_TOKEN", "supersecret")
        env = self._recorded_env(monkeypatch)
        # notify-send needs DISPLAY/DBUS; the toast needs SystemRoot.
        for name in ("PATH", "SYSTEMROOT", "DISPLAY",
                     "DBUS_SESSION_BUS_ADDRESS"):
            if name in os.environ:
                assert env[name] == os.environ[name]


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


class TestRedactionAtTheNotificationBoundary:
    """The title and body are redacted here, not at the call sites.

    Notification text does not leave the process through stdout: it
    leaves as subprocess argv, or escaped into a PowerShell payload. The
    CLI's redacting stream wrappers never see it, so redacting at each
    place a notification is composed would be a rule to remember rather
    than a boundary that holds. This is the one function every
    notification goes through.
    """

    SECRET = "s3cret-token-value-1234"

    def _argv(self, title, body):
        with patch.object(notify, "notification_backend",
                          return_value="linux-notify-send"), \
             patch.object(notify, "_run", return_value=_ok()) as run:
            result = send_notification(title, body)
        assert result["sent"] is True
        return run.call_args[0][0]

    def test_a_registered_secret_is_stripped_from_both_strings(self):
        remember_secret(self.SECRET)
        title, body = self._argv(
            f"Worsaga: {self.SECRET}",
            f"ECON101: new file - {self.SECRET}",
        )[-2:]
        assert self.SECRET not in title
        assert self.SECRET not in body
        assert REDACTED in title
        assert REDACTED in body

    def test_a_token_parameter_goes_whatever_its_value(self):
        # Not a configured secret: a link Moodle minted, arriving inside
        # somebody's discussion title.
        body = self._argv(
            "Worsaga: 1 change",
            "CS210: new discussion - "
            "See https://site.example/x?token=abcdef1234567890",
        )[-1]
        assert "abcdef1234567890" not in body
        # The parameter name survives; only its value is taken, so the
        # reader can still see what kind of link it was.
        assert f"token={REDACTED}" in body

    def test_the_windows_toast_payload_is_redacted_too(self):
        # This backend never puts content in argv at all: it escapes it
        # into a PowerShell script. Redacting per-backend would have
        # missed one of the three.
        remember_secret(self.SECRET)
        with patch.object(notify, "notification_backend",
                          return_value="windows-toast"), \
             patch.object(notify, "_run", return_value=_ok()) as run:
            send_notification("Worsaga: 1 change", self.SECRET)
        args = run.call_args[0][0]
        script = base64.b64decode(
            args[args.index("-EncodedCommand") + 1]
        ).decode("utf-16-le")
        assert self.SECRET not in script
        assert REDACTED in script

    def test_ordinary_text_is_untouched(self):
        # Redaction that mangled a normal notification would be worse
        # than none: nobody would leave it on.
        title, body = self._argv(
            "Worsaga: 3 changes", "2 in ECON101, 1 in CS210",
        )[-2:]
        assert title == "Worsaga: 3 changes"
        assert body == "2 in ECON101, 1 in CS210"


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
