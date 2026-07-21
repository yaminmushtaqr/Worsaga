"""Tests for watch mode (local sync loop with notifications)."""

import json

import pytest
from unittest.mock import patch

from worsaga import watch as watch_mod
from worsaga.cli import main
from worsaga.client import MoodleWriteAttemptError
from worsaga.demo import DemoMoodleClient
from worsaga.watch import (
    MIN_WATCH_INTERVAL,
    notification_text,
    run_watch,
)


def _change(kind="new_deadline", title="Problem Set 3", course="ECON101"):
    return {"kind": kind, "title": title, "course_shortname": course}


class TestNotificationText:
    def test_single_change(self):
        title, body = notification_text([_change()])
        assert title == "Worsaga: 1 change"
        assert body == "ECON101: new deadline - Problem Set 3"

    def test_many_changes_truncated(self):
        changes = [_change(title=f"Item {i}") for i in range(5)]
        title, body = notification_text(changes)
        assert title == "Worsaga: 5 changes"
        lines = body.split("\n")
        assert len(lines) == 4
        assert lines[-1] == "...and 2 more"

    def test_untitled_change(self):
        _, body = notification_text([{"kind": "new_file"}])
        assert "(untitled)" in body


class TestRunWatch:
    def _fake_sync(self, changes_per_cycle):
        results = iter(changes_per_cycle)

        def fake(client, **kwargs):
            return {
                "site": "https://moodle.example.com",
                "synced_at": 1_800_000_000,
                "categories": {},
                "changes": next(results),
                "warnings": [],
            }
        return fake

    def test_cycles_and_sleeps(self):
        sleeps = []
        with patch.object(watch_mod, "run_sync", self._fake_sync([[], [], []])):
            summary = run_watch(
                object(), interval_seconds=120, max_cycles=3,
                sleep_fn=sleeps.append,
            )
        assert summary["cycles"] == 3
        # No sleep after the final cycle.
        assert sleeps == [120, 120]

    def test_interval_clamped_to_minimum(self):
        sleeps = []
        with patch.object(watch_mod, "run_sync", self._fake_sync([[], []])):
            summary = run_watch(
                object(), interval_seconds=1, max_cycles=2,
                sleep_fn=sleeps.append,
            )
        assert summary["interval_seconds"] == MIN_WATCH_INTERVAL
        assert sleeps == [MIN_WATCH_INTERVAL]

    def test_notifies_only_on_changes(self):
        sent = []

        def fake_notify(title, body):
            sent.append((title, body))
            return {"sent": True, "backend": "test"}

        with patch.object(
            watch_mod, "run_sync",
            self._fake_sync([[], [_change()], []]),
        ):
            summary = run_watch(
                object(), max_cycles=3, sleep_fn=lambda s: None,
                notify_fn=fake_notify,
            )
        assert summary["changes_total"] == 1
        assert len(sent) == 1
        assert sent[0][0] == "Worsaga: 1 change"

    def test_notify_disabled(self):
        def explode(title, body):
            raise AssertionError("must not notify")

        with patch.object(watch_mod, "run_sync", self._fake_sync([[_change()]])):
            run_watch(
                object(), max_cycles=1, notify=False,
                sleep_fn=lambda s: None, notify_fn=explode,
            )

    def test_failed_cycle_continues(self):
        calls = {"n": 0}

        def flaky(client, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("network down")
            return {"synced_at": 1, "changes": [], "warnings": [],
                    "categories": {}, "site": "s"}

        seen = []
        with patch.object(watch_mod, "run_sync", flaky):
            summary = run_watch(
                object(), max_cycles=2, sleep_fn=lambda s: None,
                on_cycle=seen.append,
            )
        assert summary["failures"] == 1
        assert summary["cycles"] == 2
        assert seen[0]["ok"] is False
        assert "network down" in seen[0]["error"]
        # Failed cycles still carry a display timestamp.
        assert seen[0]["synced_at"] > 0
        assert seen[1]["ok"] is True

    def test_zero_cycles_runs_nothing(self):
        def explode(client, **kwargs):
            raise AssertionError("must not sync")

        with patch.object(watch_mod, "run_sync", explode):
            summary = run_watch(
                object(), max_cycles=0, sleep_fn=lambda s: None,
            )
        assert summary["cycles"] == 0
        assert summary["changes_total"] == 0

    def test_write_attempt_error_propagates(self):
        def bad(client, **kwargs):
            raise MoodleWriteAttemptError("blocked write")

        with patch.object(watch_mod, "run_sync", bad):
            with pytest.raises(MoodleWriteAttemptError):
                run_watch(object(), max_cycles=1, sleep_fn=lambda s: None)


class TestCliSurface:
    @pytest.fixture(autouse=True)
    def _cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORSAGA_CACHE_PATH", str(tmp_path / "cache.db"))

    @pytest.fixture(autouse=True)
    def _no_sleep(self):
        # run_watch resolves time.sleep at call time, so this keeps
        # multi-cycle CLI tests instant.
        with patch("time.sleep"):
            yield

    def test_watch_one_cycle_demo(self, capsys):
        main(["--demo", "watch", "--cycles", "1", "--no-notify", "-q"])
        out = capsys.readouterr().out
        assert "cycle 1:" in out

    def test_watch_json_is_ndjson(self, capsys):
        main(["--demo", "--json", "watch", "--cycles", "2", "--no-notify",
              "--interval", "1m"])
        lines = [
            line for line in capsys.readouterr().out.splitlines() if line
        ]
        # Stream contract: exactly one compact JSON object per line.
        assert len(lines) == 2
        docs = [json.loads(line) for line in lines]
        assert docs[0]["cycle"] == 1
        assert docs[1]["cycle"] == 2

    def test_watch_yaml_uses_document_separators(self, capsys):
        yaml = pytest.importorskip("yaml")
        main(["--demo", "--yaml", "watch", "--cycles", "2", "--no-notify"])
        out = capsys.readouterr().out
        assert out.count("---") == 2
        docs = [doc for doc in yaml.safe_load_all(out) if doc is not None]
        assert [doc["cycle"] for doc in docs] == [1, 2]

    def test_watch_zero_cycles_cli(self, capsys):
        main(["--demo", "watch", "--cycles", "0", "--no-notify"])
        captured = capsys.readouterr()
        assert "cycle 1:" not in captured.out
        assert "0 cycle(s)" in captured.err

    def test_announced_interval_matches_clamp(self, capsys):
        main(["--demo", "watch", "--cycles", "1", "--no-notify",
              "--interval", "1s"])
        err = capsys.readouterr().err
        assert "every 60s" in err

    def test_watch_reports_summary(self, capsys):
        main(["--demo", "watch", "--cycles", "1", "--no-notify"])
        err = capsys.readouterr().err
        assert "Watch finished: 1 cycle(s)" in err

    def test_demo_watch_makes_no_network_calls(self, capsys):
        # The demo test-suite guard (test_demo.py) blocks sockets; here
        # we simply exercise two full cycles offline.
        client = DemoMoodleClient()
        assert client.get_courses()
        main(["--demo", "watch", "--cycles", "2", "--no-notify",
              "--interval", "60", "-q"])
        assert "cycle 2:" in capsys.readouterr().out
