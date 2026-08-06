"""CLI and MCP behaviour for sync outcomes, rate limits, and the lock.

These are the surfaces an unattended caller actually branches on: the
exit code of ``worsaga sync``, the ``outcome`` field on the MCP payload,
and the one-line messages a human reads on stderr.
"""

import json

import pytest
from unittest.mock import patch

from worsaga import cli as cli_module
from worsaga import syncstate
from worsaga.cli import main
from worsaga.client import MoodleRateLimitedError

SITE = "https://moodle.example.edu"


def _result(outcome, **extra):
    payload = {
        "site": SITE,
        "synced_at": 1_800_000_000,
        "outcome": outcome,
        "categories": {
            name: {"synced": outcome in ("success", "partial"), "items": 0,
                   "new": 0, "updated": 0, "adopted": 0, "baseline": False}
            for name in ("deadlines", "files", "grades", "forums")
        },
        "changes": [],
        "warnings": [],
        "cache_path": "/tmp/cache.db",
    }
    payload.update(extra)
    return payload


class TestCliSyncExitCode:
    def test_success_exits_zero(self, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("WORSAGA_CACHE_PATH", str(tmp_path / "cache.db"))
        main(["--demo", "sync", "-q"])
        assert "Synced" in capsys.readouterr().out

    def test_total_failure_exits_one(self, capsys):
        failed = _result("failed", warnings=["courses: network down"],
                         failure_class="network")
        with patch.object(cli_module, "run_sync", return_value=failed):
            with pytest.raises(SystemExit) as exc:
                main(["--demo", "sync", "-q"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "the sync failed" in err
        assert "network down" in err

    def test_partial_still_exits_zero(self, capsys):
        partial = _result("partial", warnings=["forums: unavailable"])
        partial["categories"]["forums"]["synced"] = False
        with patch.object(cli_module, "run_sync", return_value=partial):
            main(["--demo", "sync"])
        captured = capsys.readouterr()
        assert "Synced" in captured.out
        # Partial output keeps reporting its warnings exactly as before.
        assert "forums: unavailable" in captured.err

    def test_skipped_exits_zero_with_a_clear_line(self, capsys):
        skipped = _result(
            "skipped", warnings=["another Worsaga sync is already running"],
            skipped_reason="sync_in_progress",
        )
        with patch.object(cli_module, "run_sync", return_value=skipped):
            main(["--demo", "sync"])
        out = capsys.readouterr().out
        assert "Sync skipped" in out
        assert "already running" in out

    def test_json_mode_carries_the_outcome_and_the_exit_code(self, capsys):
        failed = _result("failed", warnings=["courses: network down"])
        with patch.object(cli_module, "run_sync", return_value=failed):
            with pytest.raises(SystemExit) as exc:
                main(["--demo", "--json", "sync"])
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        # Machine callers get both signals and they agree.
        assert payload["outcome"] == "failed"

    def test_circuit_refusal_explains_itself(self, capsys):
        blocked = _result(
            "failed",
            warnings=["circuit open: fix credentials then run 'worsaga sync' "
                      "manually (3 consecutive authentication failures; no "
                      "request was made)"],
            circuit_open=True,
            failure_class="auth",
        )
        with patch.object(cli_module, "run_sync", return_value=blocked):
            with pytest.raises(SystemExit) as exc:
                main(["--demo", "sync"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "circuit open" in err
        assert "Fix the credentials" in err

    def test_service_disabled_circuit_does_not_blame_the_credentials(
        self, capsys,
    ):
        """A site with web services off is not a credentials problem.

        Telling the user to fix a token that was never rejected sends
        them to reset something that will not help, and a manual sync is
        not the way out of this one either: it would fail identically.
        """
        blocked = _result(
            "failed",
            warnings=["circuit open: this site has not enabled web-service "
                      "access, so unattended syncing has stopped (3 "
                      "consecutive failures; no request was made)"],
            circuit_open=True,
            failure_class="service_disabled",
        )
        with patch.object(cli_module, "run_sync", return_value=blocked):
            with pytest.raises(SystemExit) as exc:
                main(["--demo", "sync", "--unattended"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "has not enabled web-service access" in err
        assert "institution's decision" in err
        # The word never appears: not as advice, not as a diagnosis.
        assert "credential" not in err.lower()
        # And no suggestion that running it by hand would clear it.
        assert "yourself" not in err.lower()

    def test_unattended_flag_is_forwarded(self):
        seen = {}

        def _fake(client, **kwargs):
            seen.update(kwargs)
            return _result("success")

        with patch.object(cli_module, "run_sync", _fake):
            main(["--demo", "sync", "-q", "--unattended"])
        assert seen["unattended"] is True

    def test_foreground_sync_is_not_unattended(self):
        seen = {}

        def _fake(client, **kwargs):
            seen.update(kwargs)
            return _result("success")

        with patch.object(cli_module, "run_sync", _fake):
            main(["--demo", "sync", "-q"])
        assert seen["unattended"] is False


class TestCliRateLimitMessage:
    def test_top_level_handler_prints_one_plain_line(self, capsys):
        with patch.object(
            cli_module, "run_sync",
            side_effect=MoodleRateLimitedError("Moodle answered HTTP 429"),
        ):
            with pytest.raises(SystemExit) as exc:
                main(["--demo", "sync", "-q"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "the site is rate-limiting requests; try again later." in err
        # No suggestion to check credentials or configuration.
        assert "token" not in err.lower()


class TestCliWatchBackoff:
    def test_a_failed_cycle_prints_the_backoff_line(self, capsys):
        cycle = _result("failed", warnings=["courses: network down"])
        cycle.update({"cycle": 1, "ok": False, "consecutive_failures": 2,
                      "next_cycle_in": 1200, "backoff": True})

        def _fake_watch(client, **kwargs):
            kwargs["on_cycle"](cycle)
            return {"cycles": 1, "changes_total": 0, "failures": 1,
                    "skipped": 0, "consecutive_failures": 2,
                    "interval_seconds": 600}

        with patch.object(cli_module, "run_watch", _fake_watch):
            main(["--demo", "watch", "--cycles", "1", "--no-notify"])
        err = capsys.readouterr().err
        assert "sync failed: courses: network down" in err
        assert "Backing off: next cycle in 1200s (2 consecutive failures)." in err

    def test_a_skipped_cycle_says_so(self, capsys):
        cycle = _result("skipped", warnings=["another sync is running"])
        cycle.update({"cycle": 1, "ok": True, "consecutive_failures": 0})

        def _fake_watch(client, **kwargs):
            kwargs["on_cycle"](cycle)
            return {"cycles": 1, "changes_total": 0, "failures": 0,
                    "skipped": 1, "consecutive_failures": 0,
                    "interval_seconds": 600}

        with patch.object(cli_module, "run_watch", _fake_watch):
            main(["--demo", "watch", "--cycles", "1", "--no-notify"])
        captured = capsys.readouterr()
        assert "skipped (another sync is already running)" in captured.out
        assert "1 skipped" in captured.err

    def test_quiet_suppresses_the_backoff_line(self, capsys):
        cycle = _result("failed")
        cycle.update({"cycle": 1, "ok": False, "consecutive_failures": 1,
                      "next_cycle_in": 1200, "backoff": True})

        def _fake_watch(client, **kwargs):
            kwargs["on_cycle"](cycle)
            return {"cycles": 1, "changes_total": 0, "failures": 1,
                    "skipped": 0, "consecutive_failures": 1,
                    "interval_seconds": 600}

        with patch.object(cli_module, "run_watch", _fake_watch):
            main(["--demo", "watch", "--cycles", "1", "--no-notify", "-q"])
        assert "Backing off" not in capsys.readouterr().err

    def test_demo_watch_reports_a_real_outcome(self, capsys, tmp_path,
                                               monkeypatch):
        monkeypatch.setenv("WORSAGA_CACHE_PATH", str(tmp_path / "cache.db"))
        with patch("time.sleep"):
            main(["--demo", "--json", "watch", "--cycles", "1", "--no-notify"])
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["outcome"] == "success"
        assert payload["ok"] is True


class TestAutoSyncStatusSurfacesOutcomes:
    def test_status_reports_the_failure_streak_and_circuit(
        self, capsys, monkeypatch,
    ):
        from worsaga import autosync

        syncstate.record_outcome(SITE, "failed", failure_class="auth", now=10)
        syncstate.record_outcome(SITE, "failed", failure_class="auth", now=20)
        status = {
            "platform": "linux", "installed": True, "state": "installed",
            "method": "systemd-user", "record": None, "record_stale": False,
            "sync_state": syncstate.read_site_state(SITE),
        }
        with patch.object(autosync, "autosync_status", return_value=status):
            with patch.object(cli_module, "autosync_status", return_value=status):
                main(["auto-sync", "status"])
        captured = capsys.readouterr()
        assert "last outcome: failed" in captured.out
        assert "consecutive failures: 2 (auth)" in captured.out
        assert "Scheduled syncs are paused" in captured.err

    def test_status_attaches_state_from_the_record_reader(self, tmp_path):
        from worsaga.autosync import _attach_last_sync

        syncstate.record_outcome(
            "https://moodle.test.invalid", "failed",
            failure_class="network", now=10,
        )
        result = {}
        _attach_last_sync(result)
        # The conftest fake site is what a client built here reports.
        assert result["sync_state"]["consecutive_failures"] == 1
        assert result["sync_state"]["failure_class"] == "network"


class TestMcpSyncNow:
    def test_payload_carries_the_outcome(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WORSAGA_DEMO", "1")
        monkeypatch.setenv("WORSAGA_CACHE_PATH", str(tmp_path / "cache.db"))
        from worsaga import mcp_server

        monkeypatch.setattr(mcp_server, "_client", None, raising=False)
        result = mcp_server.sync_now()
        assert result["outcome"] == "success"

    def test_a_busy_site_returns_a_structured_error(self, monkeypatch, tmp_path):
        from worsaga import mcp_server

        busy = {
            "site": SITE, "outcome": "skipped",
            "warnings": ["another Worsaga sync is already running for this "
                         "site (pid 4242, started 3s ago)"],
            "skipped_reason": "sync_in_progress",
            "categories": {}, "changes": [], "synced_at": 1,
        }
        with patch.object(mcp_server, "_run_sync", return_value=busy):
            with patch.object(mcp_server, "_get_client", return_value=object()):
                result = mcp_server.sync_now()
        assert result["error_code"] == "sync_in_progress"
        assert "already running" in result["error"]
        assert result["site"] == SITE

    def test_new_error_codes_are_documented(self):
        from worsaga.mcp_server import ERROR_CODES

        assert "sync_in_progress" in ERROR_CODES
        assert "rate_limited" in ERROR_CODES
        # The vocabulary stays a small closed set with no duplicates.
        assert len(ERROR_CODES) == len(set(ERROR_CODES))


class _RateLimitedClient:
    """A client that meets HTTP 429 on every call it is asked to make."""

    base_url = SITE
    is_demo = False

    def __getattr__(self, name):
        def _fail(*args, **kwargs):
            raise MoodleRateLimitedError(
                "Moodle answered HTTP 429 (rate limited) and Worsaga gave up. "
                "The site is asking for fewer requests; try again later.",
                status=429,
            )
        return _fail


class TestMcpToolsReportRateLimiting:
    """``rate_limited`` is advertised, so every tool has to be able to say it.

    Before the shared registration wrapper only the two tools that
    happened to catch it could; the rest surfaced a FastMCP ``isError``
    string, which an agent cannot branch on.
    """

    @pytest.fixture()
    def limited(self):
        from worsaga import mcp_server

        with patch.object(
            mcp_server, "_get_client", return_value=_RateLimitedClient(),
        ):
            yield mcp_server

    def test_a_discovery_tool(self, limited):
        result = limited.list_courses()
        # Declared -> list[...], so the error arrives as a one-item list,
        # exactly as get_changes already did.
        assert isinstance(result, list)
        assert result[0]["error_code"] == "rate_limited"
        assert "try again later" in result[0]["error"]

    def test_a_course_scoped_tool(self, limited):
        result = limited.get_grades(101)
        assert isinstance(result, list)
        assert result[0]["error_code"] == "rate_limited"

    def test_a_dict_returning_tool(self, limited):
        result = limited.get_grade_summary(101)
        assert isinstance(result, dict)
        assert result["error_code"] == "rate_limited"

    def test_sync_now(self, limited):
        with patch.object(
            limited, "_run_sync",
            side_effect=MoodleRateLimitedError("slow down", status=429),
        ):
            result = limited.sync_now()
        assert result["error_code"] == "rate_limited"

    def test_no_tool_leaks_a_traceback(self, limited):
        checks = [
            lambda: limited.list_courses(),
            lambda: limited.get_deadlines(),
            lambda: limited.get_grades(101),
            lambda: limited.get_notifications(),
            lambda: limited.get_messages(),
            lambda: limited.get_latest_updates(),
        ]
        for call in checks:
            result = call()
            payload = result[0] if isinstance(result, list) else result
            assert payload["error_code"] == "rate_limited"
            assert "Traceback" not in payload["error"]

    def test_the_message_stays_token_free(self, limited):
        result = limited.list_courses()
        assert "fake-test-token-not-a-real-credential" not in result[0]["error"]

    def test_every_tool_is_registered_through_the_wrapper(self):
        import re
        from pathlib import Path

        from worsaga import mcp_server as server

        source = Path("src/worsaga/mcp_server.py").read_text(encoding="utf-8")
        # A tool added with a bare @mcp.tool() would silently opt out of
        # the whole contract — rate-limit shaping, redaction, and the
        # capability profile — so the shape is asserted, not assumed.
        assert "@mcp.tool()" not in source
        assert len(re.findall(r"^@tool\($", source, re.M)) == 26
        assert len(server.ALL_TOOLS) == 26
