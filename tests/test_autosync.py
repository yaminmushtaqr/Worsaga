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
        with patch.object(autosync, "_run", return_value=_ok(_TASK_ROW)):
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


_TASK_ROW = '"\\WorsagaAutoSync","21/07/2026 15:00:00","Ready"'
_OTHER_ROW = '"\\SomeOtherTask","21/07/2026 15:00:00","Ready"'


class TestStatus:
    """Absence requires machine-readable evidence, never a bare exit code."""

    def test_windows_installed(self, windows):
        listing = _OTHER_ROW + "\n" + _TASK_ROW
        with patch.object(autosync, "_run", return_value=_ok(listing)) as run:
            result = autosync_status()
        assert result["state"] == "installed"
        assert result["installed"] is True
        assert result["method"] == "schtasks"
        assert run.call_args[0][0] == [
            "schtasks", "/Query", "/FO", "CSV", "/NH",
        ]

    def test_windows_absent_from_successful_listing(self, windows):
        with patch.object(autosync, "_run", return_value=_ok(_OTHER_ROW)):
            result = autosync_status()
        assert result["state"] == "absent"
        assert result["installed"] is False
        assert "error" not in result

    def test_windows_similar_name_is_not_a_match(self, windows):
        listing = '"\\WorsagaAutoSync2","x","Ready"'
        with patch.object(autosync, "_run", return_value=_ok(listing)):
            result = autosync_status()
        assert result["state"] == "absent"

    def test_windows_query_failure_is_unknown(self, windows):
        with patch.object(autosync, "_run",
                          return_value=_fail("ERROR: Access is denied.")):
            result = autosync_status()
        assert result["state"] == "unknown"
        assert result["installed"] is False
        assert "denied" in result["error"]

    def test_includes_local_record(self, windows):
        autosync._write_record({"interval_minutes": 30, "command": ["x"]})
        with patch.object(autosync, "_run", return_value=_ok(_TASK_ROW)):
            result = autosync_status()
        assert result["record"]["interval_minutes"] == 30

    def test_status_never_writes(self, windows, record_in_tmp):
        with patch.object(autosync, "_run", return_value=_ok()):
            autosync_status()
        assert not (record_in_tmp / "autosync.json").exists()

    def test_macos_loaded_without_plist_is_installed(self, macos, tmp_path,
                                                     monkeypatch):
        # A job can survive plist deletion; the listing is authoritative.
        monkeypatch.setattr(
            autosync, "_macos_plist_path",
            lambda: tmp_path / "com.worsaga.autosync.plist",
        )
        listing = "123\t0\tcom.worsaga.autosync"
        with patch.object(autosync, "_run", return_value=_ok(listing)):
            result = autosync_status()
        assert result["state"] == "installed"
        assert result["plist_exists"] is False

    def test_macos_absent_from_successful_listing(self, macos, tmp_path,
                                                  monkeypatch):
        monkeypatch.setattr(
            autosync, "_macos_plist_path",
            lambda: tmp_path / "com.worsaga.autosync.plist",
        )
        listing = "456\t0\tcom.apple.something"
        with patch.object(autosync, "_run", return_value=_ok(listing)):
            result = autosync_status()
        assert result["state"] == "absent"

    def test_macos_listing_failure_is_unknown(self, macos, tmp_path,
                                              monkeypatch):
        monkeypatch.setattr(
            autosync, "_macos_plist_path",
            lambda: tmp_path / "com.worsaga.autosync.plist",
        )
        with patch.object(autosync, "_run",
                          return_value=_fail("Operation not permitted")):
            result = autosync_status()
        assert result["state"] == "unknown"
        assert "not permitted" in result["error"]

    def _linux_show(self, load_state, active_state="inactive"):
        return _ok(f"LoadState={load_state}\nActiveState={active_state}")

    def test_linux_loaded_but_inactive_is_installed(self, linux, monkeypatch):
        # is-active would misclassify an enabled-but-inactive timer.
        monkeypatch.setattr(autosync.shutil, "which",
                            lambda name: "/usr/bin/systemctl")
        with patch.object(autosync, "_run",
                          return_value=self._linux_show("loaded", "inactive")):
            result = autosync_status()
        assert result["state"] == "installed"
        assert result["timer_active"] is False

    def test_linux_active_timer(self, linux, monkeypatch):
        monkeypatch.setattr(autosync.shutil, "which",
                            lambda name: "/usr/bin/systemctl")
        with patch.object(autosync, "_run",
                          return_value=self._linux_show("loaded", "active")):
            result = autosync_status()
        assert result["state"] == "installed"
        assert result["timer_active"] is True

    def test_linux_not_found_is_absent(self, linux, monkeypatch):
        monkeypatch.setattr(autosync.shutil, "which",
                            lambda name: "/usr/bin/systemctl")
        with patch.object(autosync, "_run",
                          return_value=self._linux_show("not-found")):
            result = autosync_status()
        assert result["state"] == "absent"

    def test_linux_show_failure_is_unknown(self, linux, monkeypatch):
        monkeypatch.setattr(autosync.shutil, "which",
                            lambda name: "/usr/bin/systemctl")
        with patch.object(autosync, "_run",
                          return_value=_fail("Failed to connect to bus")):
            result = autosync_status()
        assert result["state"] == "unknown"
        assert "bus" in result["error"]

    def test_linux_missing_systemctl_is_unknown(self, linux, monkeypatch):
        monkeypatch.setattr(autosync.shutil, "which", lambda name: None)
        result = autosync_status()
        assert result["state"] == "unknown"
        assert result["method"] == "none"
        assert "systemctl not found" in result["error"]


class TestRemove:
    def test_dry_run_executes_nothing(self, windows):
        with patch.object(autosync, "_run", return_value=_ok()) as run:
            with patch.object(autosync, "autosync_status",
                              return_value={"installed": True,
                                            "state": "installed"}):
                result = remove_autosync(dry_run=True)
        assert result["removed"] is False
        assert result["actions"][0]["run"][:2] == ["schtasks", "/Delete"]
        run.assert_not_called()

    def test_remove_deletes_record(self, windows):
        autosync._write_record({"interval_minutes": 30})

        def by_command(args):
            if args[:2] == ["schtasks", "/Query"]:
                return _ok(_TASK_ROW)
            return _ok()

        with patch.object(autosync, "_run", side_effect=by_command):
            result = remove_autosync()
        assert result["removed"] is True
        assert result["was_installed"] is True
        assert not autosync.autosync_record_path().exists()

    def test_remove_when_absent_skips_delete(self, windows):
        # Authoritative absence: the listing succeeded without the task.
        with patch.object(autosync, "_run",
                          return_value=_ok(_OTHER_ROW)) as run:
            result = remove_autosync()
        assert result["removed"] is True
        assert result["was_installed"] is False
        assert all(
            call.args[0][:2] != ["schtasks", "/Delete"]
            for call in run.call_args_list
        )

    def test_remove_on_query_failure_aborts(self, windows):
        # Access-denied style nonzero exits are unknown, not absence.
        autosync._write_record({"interval_minutes": 30})
        with patch.object(autosync, "_run",
                          return_value=_fail("ERROR: Access is denied.")):
            result = remove_autosync()
        assert result["removed"] is False
        assert "cannot determine scheduler state" in result["error"]
        assert autosync.autosync_record_path().exists()


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
                          return_value={"installed": True,
                                        "state": "installed"}), \
             patch.object(autosync, "_run", return_value=_fail("busy")):
            result = remove_autosync()
        assert result["removed"] is False
        assert result["error"] == "busy"
        assert plist.exists()
        assert autosync.autosync_record_path().exists()

    def test_macos_unloaded_agent_skips_unload(self, plist):
        with patch.object(autosync, "autosync_status",
                          return_value={"installed": False,
                                        "state": "absent"}), \
             patch.object(autosync, "_run") as run:
            result = remove_autosync()
        assert result["removed"] is True
        assert not plist.exists()
        run.assert_not_called()

    def test_macos_loaded_without_plist_removes_by_label(
        self, macos, tmp_path, monkeypatch,
    ):
        # The plist is gone but the job is still loaded: stop by label.
        missing = tmp_path / "com.worsaga.autosync.plist"
        monkeypatch.setattr(autosync, "_macos_plist_path", lambda: missing)
        with patch.object(autosync, "autosync_status",
                          return_value={"installed": True,
                                        "state": "installed"}), \
             patch.object(autosync, "_run", return_value=_ok()) as run:
            result = remove_autosync()
        assert result["removed"] is True
        assert run.call_args[0][0] == [
            "launchctl", "remove", "com.worsaga.autosync",
        ]

    def test_linux_failed_disable_keeps_everything(self, units):
        autosync._write_record({"interval_minutes": 30})
        with patch.object(autosync, "autosync_status",
                          return_value={"installed": True,
                                        "state": "installed"}), \
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
                          return_value={"installed": True,
                                        "state": "installed"}), \
             patch.object(autosync, "_run", side_effect=flaky):
            result = remove_autosync()
        assert result["removed"] is True
        assert "daemon-reload" in result["warning"]
        assert not (units / "worsaga-autosync.timer").exists()

    def test_unknown_scheduler_state_aborts_macos_removal(self, plist):
        autosync._write_record({"interval_minutes": 30})
        unknown = {"installed": False, "state": "unknown",
                   "method": "launchd",
                   "error": "scheduler unavailable", "record": None}
        with patch.object(autosync, "autosync_status",
                          return_value=unknown), \
             patch.object(autosync, "_run") as run:
            result = remove_autosync()
        assert result["removed"] is False
        assert "cannot determine scheduler state" in result["error"]
        assert plist.exists()
        assert autosync.autosync_record_path().exists()
        run.assert_not_called()

    def test_unknown_scheduler_state_aborts_windows_removal(self, windows):
        autosync._write_record({"interval_minutes": 30})
        unknown = {"installed": False, "state": "unknown",
                   "method": "schtasks",
                   "error": "timed out", "record": None}
        with patch.object(autosync, "autosync_status",
                          return_value=unknown), \
             patch.object(autosync, "_run") as run:
            result = remove_autosync()
        assert result["removed"] is False
        assert "timed out" in result["error"]
        assert autosync.autosync_record_path().exists()
        run.assert_not_called()

    def test_unknown_state_dry_run_still_previews(self, windows):
        unknown = {"installed": False, "state": "unknown",
                   "method": "schtasks",
                   "error": "timed out", "record": None}
        with patch.object(autosync, "autosync_status",
                          return_value=unknown):
            result = remove_autosync(dry_run=True)
        assert result["removed"] is False
        assert "error" not in result
        assert result["actions"]

    def test_linux_no_systemctl_dry_run_keeps_record(self, linux, tmp_path,
                                                     monkeypatch):
        monkeypatch.setattr(autosync, "_linux_unit_dir",
                            lambda: tmp_path / "systemd" / "user")
        monkeypatch.setattr(autosync.shutil, "which", lambda name: None)
        autosync._write_record({"interval_minutes": 30})
        result = remove_autosync(dry_run=True)
        assert result["removed"] is False
        assert autosync.autosync_record_path().exists()
        assert any("delete" in action for action in result["actions"])

    def test_linux_no_systemctl_real_remove_deletes_record(
        self, linux, tmp_path, monkeypatch,
    ):
        # No unit files on disk: proven nothing schedulable remains, so
        # the record-only removal proceeds.
        monkeypatch.setattr(autosync, "_linux_unit_dir",
                            lambda: tmp_path / "systemd" / "user")
        monkeypatch.setattr(autosync.shutil, "which", lambda name: None)
        autosync._write_record({"interval_minutes": 30})
        result = remove_autosync()
        assert result["removed"] is True
        assert not autosync.autosync_record_path().exists()

    def test_linux_no_systemctl_with_unit_files_aborts(
        self, linux, tmp_path, monkeypatch,
    ):
        # Unit files exist but systemctl is gone: the timer may still be
        # registered, so fail closed.
        unit_dir = tmp_path / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        (unit_dir / "worsaga-autosync.timer").write_text("t")
        monkeypatch.setattr(autosync, "_linux_unit_dir", lambda: unit_dir)
        monkeypatch.setattr(autosync.shutil, "which", lambda name: None)
        autosync._write_record({"interval_minutes": 30})
        result = remove_autosync()
        assert result["removed"] is False
        assert "unit files" in result["error"]
        assert autosync.autosync_record_path().exists()
        assert (unit_dir / "worsaga-autosync.timer").exists()


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

    def test_status_never_creates_the_cache(self, windows, tmp_path,
                                            monkeypatch):
        cache = tmp_path / "brand-new" / "cache.db"
        monkeypatch.setenv("WORSAGA_CACHE_PATH", str(cache))
        monkeypatch.setenv("WORSAGA_DEMO", "1")
        with patch.object(autosync, "_run", return_value=_ok()):
            result = autosync_status()
        assert "last_sync_at" not in result
        assert not cache.exists()
        assert not cache.parent.exists()


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

    def test_status_unknown_human(self, capsys):
        fake = {"platform": "windows", "method": "schtasks",
                "installed": False, "state": "unknown", "record": None,
                "error": "ERROR: Access is denied."}
        with patch("worsaga.cli.autosync_status", return_value=fake):
            main(["auto-sync", "status"])
        captured = capsys.readouterr()
        assert "state unknown (schtasks)" in captured.out
        assert "denied" in captured.err

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
