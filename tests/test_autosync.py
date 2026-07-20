"""Tests for auto-sync scheduler registration (install/status/remove).

All scheduler subprocess calls are mocked — the test suite never
touches the real Task Scheduler, launchd, or systemd.
"""

import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from worsaga import autosync
from worsaga.autosync import (
    DEFAULT_INTERVAL_MINUTES,
    MAX_INTERVAL_MINUTES,
    MIN_INTERVAL_MINUTES,
    WINDOWS_TASK_NAME,
    autosync_status,
    install_autosync,
    remove_autosync,
    sync_command,
)
from worsaga.cli import main


def _ok(stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=0,
                                       stdout=stdout, stderr="")


def _fail(stderr="denied"):
    return subprocess.CompletedProcess(args=[], returncode=1,
                                       stdout="", stderr=stderr)


@pytest.fixture(autouse=True)
def record_in_tmp(tmp_path, monkeypatch):
    """Keep the autosync.json record out of the real user data dir."""
    monkeypatch.setattr(
        autosync, "autosync_record_path",
        lambda: tmp_path / "autosync.json",
    )
    return tmp_path


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")


@pytest.fixture
def macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")


class TestSyncCommand:
    def test_prefers_entry_point(self):
        with patch.object(autosync.shutil, "which",
                          return_value="C:\\bin\\worsaga.exe"):
            assert sync_command() == ["C:\\bin\\worsaga.exe", "sync", "--quiet"]

    def test_falls_back_to_interpreter(self):
        with patch.object(autosync.shutil, "which", return_value=None):
            command = sync_command()
        assert command[0] == sys.executable
        assert command[1:] == ["-m", "worsaga.cli", "sync", "--quiet"]

    def test_never_contains_credentials(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_TOKEN", "supersecret")
        assert "supersecret" not in " ".join(sync_command())


class TestInstallWindows:
    def test_dry_run_executes_nothing(self, windows):
        def explode(args):
            raise AssertionError("dry run must not execute anything")

        with patch.object(autosync, "_run", explode):
            result = install_autosync(30, dry_run=True)
        assert result["dry_run"] is True
        assert result["installed"] is False
        run = result["actions"][0]["run"]
        assert run[:2] == ["schtasks", "/Create"]
        assert "/MO" in run and run[run.index("/MO") + 1] == "30"
        assert WINDOWS_TASK_NAME in run
        assert not autosync.autosync_record_path().exists()

    def test_real_install_writes_record(self, windows):
        with patch.object(autosync, "_run", return_value=_ok()):
            result = install_autosync(45)
        assert result["installed"] is True
        record = json.loads(
            autosync.autosync_record_path().read_text(encoding="utf-8")
        )
        assert record["interval_minutes"] == 45
        assert record["method"] == "schtasks"
        assert record["command"] == result["command"]

    def test_failed_install_reports_error(self, windows):
        with patch.object(autosync, "_run", return_value=_fail("access denied")):
            result = install_autosync(30)
        assert result["installed"] is False
        assert result["error"] == "access denied"
        assert not autosync.autosync_record_path().exists()

    def test_interval_clamped(self, windows):
        with patch.object(autosync, "_run", return_value=_ok()):
            low = install_autosync(1)
            high = install_autosync(999999)
        assert low["interval_minutes"] == MIN_INTERVAL_MINUTES
        assert high["interval_minutes"] == MAX_INTERVAL_MINUTES

    def test_command_paths_with_spaces_are_quoted(self, windows):
        with patch.object(autosync, "sync_command",
                          return_value=["C:\\My Tools\\worsaga.exe",
                                        "sync", "--quiet"]):
            result = install_autosync(30, dry_run=True)
        run = result["actions"][0]["run"]
        task_command = run[run.index("/TR") + 1]
        assert task_command.startswith('"C:\\My Tools\\worsaga.exe"')


class TestInstallMacos:
    def test_dry_run_plist_content(self, macos):
        with patch.object(autosync, "sync_command",
                          return_value=["/usr/local/bin/worsaga",
                                        "sync", "--quiet"]):
            result = install_autosync(30, dry_run=True)
        assert result["method"] == "launchd"
        write = result["actions"][0]
        assert write["write"].endswith("com.worsaga.autosync.plist")
        assert "<string>/usr/local/bin/worsaga</string>" in write["content"]
        assert "<integer>1800</integer>" in write["content"]

    def test_plist_content_is_escaped(self, macos):
        with patch.object(autosync, "sync_command",
                          return_value=["/tmp/<odd>&name", "sync", "--quiet"]):
            result = install_autosync(30, dry_run=True)
        content = result["actions"][0]["content"]
        assert "<odd>" not in content
        assert "&lt;odd&gt;&amp;name" in content


class TestInstallLinux:
    def test_no_systemd_is_structured_error(self, linux):
        with patch.object(autosync.shutil, "which", return_value=None):
            result = install_autosync(30)
        assert result["installed"] is False
        assert "cron" in result["error"]

    def test_dry_run_unit_content(self, linux):
        with patch.object(autosync.shutil, "which",
                          return_value="/usr/bin/systemctl"):
            result = install_autosync(20, dry_run=True)
        assert result["method"] == "systemd-user"
        writes = [a for a in result["actions"] if "write" in a]
        assert writes[0]["write"].endswith("worsaga-autosync.service")
        assert "ExecStart=" in writes[0]["content"]
        assert "OnUnitActiveSec=20min" in writes[1]["content"]
        runs = [a["run"] for a in result["actions"] if "run" in a]
        assert ["systemctl", "--user", "daemon-reload"] in runs


class TestStatus:
    def test_windows_installed(self, windows):
        with patch.object(autosync, "_run", return_value=_ok("Ready")):
            result = autosync_status()
        assert result["installed"] is True
        assert result["method"] == "schtasks"

    def test_windows_not_installed(self, windows):
        with patch.object(autosync, "_run", return_value=_fail("not found")):
            result = autosync_status()
        assert result["installed"] is False

    def test_includes_local_record(self, windows):
        autosync._write_record({"interval_minutes": 30, "command": ["x"]})
        with patch.object(autosync, "_run", return_value=_ok()):
            result = autosync_status()
        assert result["record"]["interval_minutes"] == 30

    def test_status_never_writes(self, windows, record_in_tmp):
        with patch.object(autosync, "_run", return_value=_ok()):
            autosync_status()
        assert not (record_in_tmp / "autosync.json").exists()

    def test_macos_missing_plist(self, macos, tmp_path, monkeypatch):
        monkeypatch.setattr(
            autosync, "_macos_plist_path",
            lambda: tmp_path / "com.worsaga.autosync.plist",
        )
        result = autosync_status()
        assert result["installed"] is False


class TestRemove:
    def test_dry_run_executes_nothing(self, windows):
        with patch.object(autosync, "_run", return_value=_ok()) as run:
            with patch.object(autosync, "autosync_status",
                              return_value={"installed": True}):
                result = remove_autosync(dry_run=True)
        assert result["removed"] is False
        assert result["actions"][0]["run"][:2] == ["schtasks", "/Delete"]
        run.assert_not_called()

    def test_remove_deletes_record(self, windows):
        autosync._write_record({"interval_minutes": 30})
        with patch.object(autosync, "_run", return_value=_ok()):
            result = remove_autosync()
        assert result["removed"] is True
        assert result["was_installed"] is True
        assert not autosync.autosync_record_path().exists()

    def test_remove_when_not_installed(self, windows):
        with patch.object(autosync, "_run", return_value=_fail()) as run:
            result = remove_autosync()
        assert result["removed"] is True
        assert result["was_installed"] is False
        # Only the status query ran; no delete was attempted.
        assert all(
            call.args[0][:2] != ["schtasks", "/Delete"]
            for call in run.call_args_list
        )


class TestCliSurface:
    def test_status_json(self, capsys):
        fake = {"platform": "windows", "method": "schtasks",
                "installed": False, "record": None}
        with patch("worsaga.cli.autosync_status", return_value=fake):
            main(["--json", "auto-sync", "status"])
        payload = json.loads(capsys.readouterr().out)
        assert payload == fake

    def test_status_human(self, capsys):
        fake = {"platform": "windows", "method": "schtasks",
                "installed": True,
                "record": {"interval_minutes": 30,
                           "command": ["worsaga", "sync", "--quiet"]}}
        with patch("worsaga.cli.autosync_status", return_value=fake):
            main(["auto-sync", "status"])
        out = capsys.readouterr().out
        assert "installed (schtasks)" in out
        assert "30 min" in out

    def test_install_dry_run_human(self, capsys):
        fake = {"installed": False, "dry_run": True, "platform": "windows",
                "method": "schtasks", "interval_minutes": 30,
                "command": ["worsaga", "sync", "--quiet"],
                "actions": [{"run": ["schtasks", "/Create"]}]}
        with patch("worsaga.cli.install_autosync", return_value=fake) as install:
            main(["auto-sync", "install", "--interval", "30m", "--dry-run"])
        out = capsys.readouterr().out
        assert "dry run" in out
        assert "run:    schtasks /Create" in out
        assert install.call_args == ((30,), {"dry_run": True})

    def test_install_error_exits_nonzero(self, capsys):
        fake = {"installed": False, "dry_run": False, "platform": "windows",
                "method": "schtasks", "interval_minutes": 30,
                "command": [], "actions": [], "error": "access denied"}
        with patch("worsaga.cli.install_autosync", return_value=fake):
            with pytest.raises(SystemExit) as exc:
                main(["auto-sync", "install"])
        assert exc.value.code == 1
        assert "access denied" in capsys.readouterr().err

    def test_default_interval(self):
        fake = {"installed": False, "dry_run": True, "platform": "windows",
                "method": "schtasks",
                "interval_minutes": DEFAULT_INTERVAL_MINUTES,
                "command": [], "actions": []}
        with patch("worsaga.cli.install_autosync", return_value=fake) as install:
            main(["-q", "auto-sync", "install", "--dry-run"])
        assert install.call_args[0][0] == DEFAULT_INTERVAL_MINUTES


class TestMcpSurface:
    def test_get_autosync_status(self):
        from worsaga import mcp_server

        fake = {"platform": "windows", "method": "schtasks",
                "installed": False, "record": None}
        with patch.object(mcp_server, "_autosync_status", return_value=fake):
            assert mcp_server.get_autosync_status() == fake
