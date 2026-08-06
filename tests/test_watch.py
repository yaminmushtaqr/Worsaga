"""Tests for watch mode (local sync loop with notifications)."""

import json
import subprocess

import pytest
from unittest.mock import patch

from worsaga import notify as notify_mod
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
    """The default body is counts and course codes; titles are opt-in."""

    def test_single_change(self):
        title, body = notification_text([_change()])
        assert title == "Worsaga: 1 change"
        assert body == "1 in ECON101"

    def test_default_body_omits_change_titles(self):
        changes = [
            _change(title="Problem Set 3", course="ECON101"),
            _change(kind="grade_updated", title="Midterm: 72%", course="ECON101"),
            _change(kind="new_file", title="Lecture 4 slides", course="CS210"),
        ]
        title, body = notification_text(changes)
        assert title == "Worsaga: 3 changes"
        assert body == "2 in ECON101, 1 in CS210"
        for leaked in ("Problem Set 3", "Midterm", "72%", "Lecture 4 slides"):
            assert leaked not in body

    def test_counts_are_deterministic_and_capped(self):
        changes = (
            [_change(course="ECON101")] * 4
            + [_change(course="CS210")] * 3
            + [_change(course="PSY110")] * 2
            + [_change(course="STAT120")]
        )
        _, body = notification_text(changes)
        assert body == "4 in ECON101, 3 in CS210, 2 in PSY110, 1 in other courses"

    def test_change_without_course_is_bucketed_not_titled(self):
        _, body = notification_text([{"kind": "new_file", "title": "Secret"}])
        assert body == "1 in (no course)"
        assert "Secret" not in body

    def test_no_course_bucket_sorts_last_not_alphabetically(self):
        # It is a leftover, not a course. Sorting it by name would let a
        # parenthesis push it ahead of real course codes on a count tie.
        changes = (
            [_change(course="ECON101")] * 2
            + [{"kind": "new_file"}]
            + [_change(course="CS210")]
        )
        _, body = notification_text(changes)
        assert body == "2 in ECON101, 1 in CS210, 1 in (no course)"

    def test_no_course_bucket_sorts_last_even_when_it_is_the_biggest(self):
        changes = [{"kind": "new_file"}] * 5 + [_change(course="ECON101")]
        _, body = notification_text(changes)
        assert body == "1 in ECON101, 5 in (no course)"

    def test_details_restores_titles(self):
        title, body = notification_text([_change()], details=True)
        assert title == "Worsaga: 1 change"
        assert body == "ECON101: new deadline - Problem Set 3"

    def test_details_many_changes_truncated(self):
        changes = [_change(title=f"Item {i}") for i in range(5)]
        title, body = notification_text(changes, details=True)
        assert title == "Worsaga: 5 changes"
        lines = body.split("\n")
        assert len(lines) == 4
        assert lines[-1] == "...and 2 more"

    def test_details_untitled_change(self):
        _, body = notification_text([{"kind": "new_file"}], details=True)
        assert "(untitled)" in body

    def test_a_hidden_no_course_bucket_is_not_called_a_course(self):
        """"(no course)" is a leftover, not a course.

        It sorts last, so it is usually what falls off the end -- and
        summing it into "N in other courses" attributes those changes to
        courses they were never associated with. A bare count says only
        what is true.
        """
        changes = (
            [_change(course="ECON101")] * 4
            + [_change(course="CS210")] * 3
            + [_change(course="PSY110")] * 2
            + [_change(course="STAT120")]
            + [{"kind": "new_file"}]
        )
        _, body = notification_text(changes)
        assert body == "4 in ECON101, 3 in CS210, 2 in PSY110, 2 more"

    def test_a_hidden_tail_of_real_courses_still_names_them_as_courses(self):
        # The contrast: when everything hidden really is a course, the
        # more informative label is the accurate one and stays.
        changes = (
            [_change(course="ECON101")] * 4
            + [_change(course="CS210")] * 3
            + [_change(course="PSY110")] * 2
            + [_change(course="STAT120")]
        )
        _, body = notification_text(changes)
        assert body.endswith("1 in other courses")


class TestNotificationRedaction:
    """Detailed mode reproduces author-written titles, so the boundary
    redacts.

    A discussion title is free text somebody typed, and it can contain a
    link with a credential in it. That text leaves the process as
    subprocess argv, where the CLI's redacting stdout wrapper cannot
    reach it, so `send_notification` redacts it instead.
    """

    TITLE = "See https://site.example/x?token=abcdef1234567890"

    def _changes(self):
        return [_change(
            kind="new_discussion", title=self.TITLE, course="CS210",
        )]

    def _sync(self, changes):
        def fake(client, **kwargs):
            return {
                "site": "https://moodle.example.com",
                "synced_at": 1_800_000_000,
                "categories": {},
                "changes": changes,
                "warnings": [],
            }
        return fake

    def test_a_token_in_a_title_never_reaches_the_notifier(self):
        argv_seen = []

        def fake_run(args):
            argv_seen.append(list(args))
            return subprocess.CompletedProcess(
                args=list(args), returncode=0, stdout="", stderr="",
            )

        with patch.object(watch_mod, "run_sync", self._sync(self._changes())), \
             patch.object(notify_mod, "notification_backend",
                          return_value="linux-notify-send"), \
             patch.object(notify_mod, "_run", fake_run):
            summary = run_watch(
                object(), interval_seconds=600, max_cycles=1,
                notify_details=True, sleep_fn=lambda _seconds: None,
            )

        assert summary["changes_total"] == 1
        assert argv_seen, "the notification backend was never reached"
        joined = " ".join(argv_seen[0])
        assert "abcdef1234567890" not in joined
        assert "token=***" in joined
        # The rest of the title survives: stripping the credential is
        # the point, not suppressing what the user asked to see.
        assert "See https://site.example/x" in joined

    def test_the_default_body_carries_neither_the_title_nor_a_url(self):
        _title, body = notification_text(self._changes())
        assert body == "1 in CS210"
        for leaked in ("token", "http", "site.example", "abcdef"):
            assert leaked not in body


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
                object(), interval_seconds=600, max_cycles=3,
                sleep_fn=sleeps.append,
            )
        assert summary["cycles"] == 3
        # No sleep after the final cycle.
        assert sleeps == [600, 600]

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

    def _capture_notification(self, **kwargs) -> tuple[str, str]:
        """Run one cycle with one change and return the notification sent."""
        sent = []

        def fake_notify(title, body):
            sent.append((title, body))
            return {"sent": True, "backend": "test"}

        with patch.object(watch_mod, "run_sync", self._fake_sync([[_change()]])):
            run_watch(
                object(), max_cycles=1, sleep_fn=lambda s: None,
                notify_fn=fake_notify, **kwargs,
            )
        assert len(sent) == 1
        return sent[0]

    def test_notification_body_omits_titles_by_default(self):
        _title, body = self._capture_notification()
        assert body == "1 in ECON101"
        assert "Problem Set 3" not in body

    def test_notify_details_restores_titles(self):
        _title, body = self._capture_notification(notify_details=True)
        assert body == "ECON101: new deadline - Problem Set 3"

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

    def test_cycles_are_marked_unattended(self):
        seen = {}

        def fake(client, **kwargs):
            seen.update(kwargs)
            return {"outcome": "success", "changes": [], "warnings": [],
                    "categories": {}, "synced_at": 1, "site": "s"}

        with patch.object(watch_mod, "run_sync", fake):
            run_watch(object(), max_cycles=1, sleep_fn=lambda s: None)
        # Watch cycles honour the credential circuit breaker; a
        # foreground sync does not, because it is the reset path.
        assert seen["unattended"] is True


def _outcome_sync(outcomes):
    """A run_sync stand-in returning the given outcomes in turn."""
    values = iter(outcomes)

    def fake(client, **kwargs):
        outcome = next(values)
        return {
            "site": "https://moodle.example.com",
            "synced_at": 1_800_000_000,
            "categories": {},
            "changes": [],
            "warnings": ["nothing could be fetched"],
            "outcome": outcome,
        }
    return fake


class TestOutcomeDrivenCycles:
    def test_a_failed_outcome_is_not_an_ok_cycle(self):
        seen = []
        with patch.object(watch_mod, "run_sync", _outcome_sync(["failed"])):
            summary = run_watch(
                object(), max_cycles=1, sleep_fn=lambda s: None,
                on_cycle=seen.append,
            )
        # The known defect: run_sync returned normally, so the cycle used
        # to be reported ok=True with an empty change list.
        assert seen[0]["ok"] is False
        assert seen[0]["outcome"] == "failed"
        assert summary["failures"] == 1

    def test_a_partial_outcome_is_an_ok_cycle(self):
        seen = []
        with patch.object(watch_mod, "run_sync", _outcome_sync(["partial"])):
            summary = run_watch(
                object(), max_cycles=1, sleep_fn=lambda s: None,
                on_cycle=seen.append,
            )
        assert seen[0]["ok"] is True
        assert summary["failures"] == 0

    def test_a_skipped_outcome_is_neither(self):
        seen = []
        with patch.object(watch_mod, "run_sync", _outcome_sync(["skipped"])):
            summary = run_watch(
                object(), max_cycles=1, sleep_fn=lambda s: None,
                on_cycle=seen.append,
            )
        assert seen[0]["ok"] is True
        assert summary["failures"] == 0
        assert summary["skipped"] == 1

    def test_a_skipped_cycle_does_not_reset_a_failure_streak(self):
        # Intended contract, not an oversight: a skipped cycle means
        # another process held the sync lock, which says nothing about
        # whether the site is reachable. Clearing the streak on it would
        # drop the backoff and resume hammering a site that is still
        # broken; counting it as a failure would blame this loop for
        # somebody else's lock. It does neither.
        seen = []
        with patch.object(
            watch_mod, "run_sync",
            _outcome_sync(["failed", "skipped", "failed"]),
        ):
            run_watch(
                object(), max_cycles=3, sleep_fn=lambda s: None,
                on_cycle=seen.append, rng=lambda: 0.5,
            )
        assert [c["consecutive_failures"] for c in seen] == [1, 1, 2]


class TestBackoff:
    def test_no_failures_keeps_the_base_interval(self):
        assert watch_mod.backoff_seconds(600, 0, rng=lambda: 0.5) == 600

    def test_interval_doubles_per_consecutive_failure(self):
        # rng 0.5 is the middle of the jitter band: no adjustment.
        assert watch_mod.backoff_seconds(600, 1, rng=lambda: 0.5) == 1200
        assert watch_mod.backoff_seconds(600, 2, rng=lambda: 0.5) == 2400
        assert watch_mod.backoff_seconds(600, 3, rng=lambda: 0.5) == 3600

    def test_capped_at_eight_intervals_or_an_hour(self):
        # 300s base: eight intervals (2400s) binds before the hour cap.
        assert watch_mod.backoff_seconds(300, 20, rng=lambda: 0.5) == 2400
        # 900s base: the hour cap binds before eight intervals (7200s).
        assert watch_mod.backoff_seconds(900, 20, rng=lambda: 0.5) == 3600
        assert watch_mod.backoff_seconds(900, 20, rng=lambda: 1.0) <= 3600 * 1.1

    def test_jitter_is_ten_percent_either_way(self):
        assert watch_mod.backoff_seconds(600, 1, rng=lambda: 0.0) == 1080
        assert watch_mod.backoff_seconds(600, 1, rng=lambda: 1.0) == 1320

    def test_never_shorter_than_the_base_interval(self):
        assert watch_mod.backoff_seconds(600, 1, rng=lambda: 0.0) >= 600

    def test_the_loop_backs_off_then_resets(self):
        sleeps = []
        with patch.object(
            watch_mod, "run_sync",
            _outcome_sync(["failed", "failed", "success", "failed"]),
        ):
            run_watch(
                object(), interval_seconds=600, max_cycles=4,
                sleep_fn=sleeps.append, rng=lambda: 0.5,
            )
        # 2x, 4x, back to the base interval after the success.
        assert sleeps == [1200, 2400, 600]

    def test_the_backoff_is_visible_on_the_cycle_result(self):
        seen = []
        with patch.object(watch_mod, "run_sync", _outcome_sync(["failed", "failed"])):
            run_watch(
                object(), interval_seconds=600, max_cycles=2,
                sleep_fn=lambda s: None, on_cycle=seen.append,
                rng=lambda: 0.5,
            )
        assert seen[0]["backoff"] is True
        assert seen[0]["next_cycle_in"] == 1200
        assert seen[0]["consecutive_failures"] == 1
        # The last cycle has no next one to describe.
        assert "next_cycle_in" not in seen[1]

    def test_a_raised_cycle_also_backs_off(self):
        sleeps = []

        def boom(client, **kwargs):
            raise OSError("network down")

        with patch.object(watch_mod, "run_sync", boom):
            summary = run_watch(
                object(), interval_seconds=600, max_cycles=2,
                sleep_fn=sleeps.append, rng=lambda: 0.5,
            )
        assert sleeps == [1200]
        assert summary["failures"] == 2
        assert summary["consecutive_failures"] == 2


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
              "--interval", "5m"])
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

    def test_watch_yaml_missing_pyyaml_leaves_no_stray_separator(
        self, capsys,
    ):
        with patch("worsaga.cli.render_structured",
                   side_effect=RuntimeError("PyYAML required")):
            with pytest.raises(SystemExit) as exc:
                main(["--demo", "--yaml", "watch", "--cycles", "1",
                      "--no-notify"])
        assert exc.value.code == 1
        captured = capsys.readouterr()
        # Render-before-separator: a failed render emits nothing at all.
        assert "---" not in captured.out
        assert "PyYAML" in captured.err

    def test_watch_zero_cycles_cli(self, capsys):
        main(["--demo", "watch", "--cycles", "0", "--no-notify"])
        captured = capsys.readouterr()
        assert "cycle 1:" not in captured.out
        assert "0 cycle(s)" in captured.err

    def test_announced_interval_matches_clamp(self, capsys):
        main(["--demo", "watch", "--cycles", "1", "--no-notify",
              "--interval", "1s"])
        err = capsys.readouterr().err
        assert f"every {MIN_WATCH_INTERVAL}s" in err

    def test_below_floor_interval_warns(self, capsys):
        # Issue 1: a below-floor watch interval is clamped up, with a
        # one-line stderr warning stating requested vs applied value.
        main(["--demo", "watch", "--cycles", "1", "--no-notify",
              "--interval", "30s"])
        err = capsys.readouterr().err
        assert (
            "Warning: interval 30s is below the minimum for watch; "
            f"using {MIN_WATCH_INTERVAL}s."
        ) in err

    def test_announces_cycle_start_with_course_count(self, capsys):
        # Issue 2: a full-account sync can take minutes; watch announces the
        # cycle (with the course count) so it never looks hung.
        main(["--demo", "watch", "--cycles", "1", "--no-notify"])
        err = capsys.readouterr().err
        assert "Sync cycle started (4 courses)..." in err

    def test_progress_on_stderr_not_stdout(self, capsys):
        main(["--demo", "watch", "--cycles", "1", "--no-notify"])
        captured = capsys.readouterr()
        # Per-course progress is on stderr; stdout stays a clean data channel.
        assert "[" in captured.err and "]" in captured.err
        assert "files:" in captured.err
        assert "Sync cycle started" not in captured.out
        assert "files:" not in captured.out

    def test_quiet_suppresses_cycle_start_and_progress(self, capsys):
        main(["--demo", "watch", "--cycles", "1", "--no-notify", "-q"])
        captured = capsys.readouterr()
        assert "Sync cycle started" not in captured.err
        assert "files:" not in captured.err
        # The per-cycle result line still reaches stdout.
        assert "cycle 1:" in captured.out

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
              "--interval", "300", "-q"])
        assert "cycle 2:" in capsys.readouterr().out
