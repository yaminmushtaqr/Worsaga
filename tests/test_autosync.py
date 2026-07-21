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


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Keep status's last-sync lookup away from the real user cache."""
    monkeypatch.setenv("WORSAGA_CACHE_PATH", str(tmp_path / "cache.db"))


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
        # The post-install health check re-queried the scheduler.
        assert result["verified"] is True
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

    def test_install_verification_failure_warns(self, windows):
        def flaky(args):
            if args[:2] == ["schtasks", "/Create"]:
                return _ok()
            return _fail("task vanished")

        with patch.object(autosync, "_run", side_effect=flaky):
            result = install_autosync(30)
        assert result["installed"] is True
        assert result["verified"] is False
        assert "not report the job" in result["warning"]

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


class TestRemoveStrictness:
    """A failed unload/disable must never be reported as a removal."""

    @pytest.fixture
    def plist(self, macos, tmp_path, monkeypatch):
        path = tmp_path / "com.worsaga.autosync.plist"
        path.write_text("<plist/>", encoding="utf-8")
        monkeypatch.setattr(autosync, "_macos_plist_path", lambda: path)
        return path

    @pytest.fixture
    def units(self, linux, tmp_path, monkeypatch):
        unit_dir = tmp_path / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        (unit_dir / "worsaga-autosync.service").write_text("s")
        (unit_dir / "worsaga-autosync.timer").write_text("t")
        monkeypatch.setattr(autosync, "_linux_unit_dir", lambda: unit_dir)
        monkeypatch.setattr(autosync.shutil, "which",
                            lambda name: "/usr/bin/systemctl")
        return unit_dir

    def test_macos_failed_unload_keeps_everything(self, plist):
        autosync._write_record({"interval_minutes": 30})
        with patch.object(autosync, "autosync_status",
                          return_value={"installed": True}), \
             patch.object(autosync, "_run", return_value=_fail("busy")):
            result = remove_autosync()
        assert result["removed"] is False
        assert result["error"] == "busy"
        assert plist.exists()
        assert autosync.autosync_record_path().exists()

    def test_macos_unloaded_agent_skips_unload(self, plist):
        with patch.object(autosync, "autosync_status",
                          return_value={"installed": False}), \
             patch.object(autosync, "_run") as run:
            result = remove_autosync()
        assert result["removed"] is True
        assert not plist.exists()
        run.assert_not_called()

    def test_linux_failed_disable_keeps_everything(self, units):
        autosync._write_record({"interval_minutes": 30})
        with patch.object(autosync, "autosync_status",
                          return_value={"installed": True}), \
             patch.object(autosync, "_run", return_value=_fail("in use")):
            result = remove_autosync()
        assert result["removed"] is False
        assert result["error"] == "in use"
        assert (units / "worsaga-autosync.timer").exists()
        assert autosync.autosync_record_path().exists()

    def test_linux_daemon_reload_failure_warns(self, units):
        def flaky(args):
            if args[:3] == ["systemctl", "--user", "daemon-reload"]:
                return _fail("bus down")
            return _ok()

        with patch.object(autosync, "autosync_status",
                          return_value={"installed": True}), \
             patch.object(autosync, "_run", side_effect=flaky):
            result = remove_autosync()
        assert result["removed"] is True
        assert "daemon-reload" in result["warning"]
        assert not (units / "worsaga-autosync.timer").exists()

    def test_linux_no_systemctl_dry_run_keeps_record(self, linux, monkeypatch):
        monkeypatch.setattr(autosync.shutil, "which", lambda name: None)
        autosync._write_record({"interval_minutes": 30})
        result = remove_autosync(dry_run=True)
        assert result["removed"] is False
        assert autosync.autosync_record_path().exists()
        assert any("delete" in action for action in result["actions"])

    def test_linux_no_systemctl_real_remove_deletes_record(
        self, linux, monkeypatch,
    ):
        monkeypatch.setattr(autosync.shutil, "which", lambda name: None)
        autosync._write_record({"interval_minutes": 30})
        result = remove_autosync()
        assert result["removed"] is True
        assert not autosync.autosync_record_path().exists()


class TestLastSyncReporting:
    def test_status_includes_cache_last_sync(self, windows, tmp_path,
                                             monkeypatch):
        from worsaga.demo import DemoMoodleClient
        from worsaga.sync import run_sync

        cache = tmp_path / "cache.db"
        monkeypatch.setenv("WORSAGA_CACHE_PATH", str(cache))
        monkeypatch.setenv("WORSAGA_DEMO", "1")
        run_sync(DemoMoodleClient(), cache_path=cache)

        with patch.object(autosync, "_run", return_value=_ok()):
            result = autosync_status()
        assert result["last_sync_at"] > 0

    def test_status_without_site_omits_last_sync(self, windows, monkeypatch):
        monkeypatch.delenv("WORSAGA_DEMO", raising=False)
        with patch.object(autosync, "_run", return_value=_ok()), \
             patch("worsaga.config.MoodleConfig.load",
                   side_effect=RuntimeError("no config")):
            result = autosync_status()
        assert "last_sync_at" not in result


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
