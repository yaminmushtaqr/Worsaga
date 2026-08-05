"""Tests for the per-origin rate coordinator and server backpressure.

Every timing dependency of :class:`~worsaga.ratelimit.OriginCoordinator`
is injected, so nothing here sleeps for real: sleeps are recorded, and the
"clock" is whatever the test hands over.
"""

import json
import threading

import pytest

from worsaga import ratelimit
from worsaga.ratelimit import (
    BACKOFF_CAP_SECONDS,
    DEFAULT_MAX_IN_FLIGHT,
    DEFAULT_MIN_GAP_SECONDS,
    MAX_ATTEMPTS,
    MAX_RETRY_AFTER_SECONDS,
    RETRY_BUDGET,
    OriginCoordinator,
    backoff_delay,
    coordinator_for,
    origin_of,
    parse_retry_after,
    resolve_max_in_flight,
    resolve_min_gap_seconds,
)

ORIGIN = "https://moodle.example.edu"


class FakeClock:
    """A monotonic clock that only moves when something sleeps."""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps: list[float] = []
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
            self.now += seconds


def _coordinator(clock=None, **kwargs) -> OriginCoordinator:
    clock = clock or FakeClock()
    kwargs.setdefault("sleep_fn", clock.sleep)
    kwargs.setdefault("monotonic", clock.monotonic)
    kwargs.setdefault("now_fn", lambda: 1_700_000_000.0)
    kwargs.setdefault("rng", lambda: 0.5)
    kwargs.setdefault("load_state", False)
    coordinator = OriginCoordinator(ORIGIN, **kwargs)
    coordinator.clock = clock  # type: ignore[attr-defined]
    return coordinator


# ── Origin identity ────────────────────────────────────────────────


class TestOrigin:
    @pytest.mark.parametrize("url,expected", [
        ("https://moodle.example.edu", "https://moodle.example.edu"),
        ("https://moodle.example.edu/moodle", "https://moodle.example.edu"),
        ("https://moodle.example.edu:443/x", "https://moodle.example.edu"),
        ("https://MOODLE.Example.EDU", "https://moodle.example.edu"),
        ("http://localhost:8080/m", "http://localhost:8080"),
        ("http://localhost:80", "http://localhost"),
    ])
    def test_origin_normalisation(self, url, expected):
        assert origin_of(url) == expected

    def test_paths_on_one_host_share_a_coordinator(self):
        first = coordinator_for("https://moodle.example.edu/moodle")
        second = coordinator_for("https://moodle.example.edu/other")
        # One server, one politeness budget - regardless of which client
        # object or which sub-path made the call.
        assert first is second

    def test_different_hosts_get_different_coordinators(self):
        assert coordinator_for("https://a.example.edu") is not coordinator_for(
            "https://b.example.edu"
        )


# ── Configuration is one-way ───────────────────────────────────────


class TestPolitenessOnlyConfiguration:
    def test_defaults(self):
        assert DEFAULT_MIN_GAP_SECONDS == 0.25
        assert DEFAULT_MAX_IN_FLIGHT == 2
        assert resolve_min_gap_seconds() == DEFAULT_MIN_GAP_SECONDS
        assert resolve_max_in_flight() == DEFAULT_MAX_IN_FLIGHT

    def test_gap_may_be_raised_only(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_MIN_REQUEST_GAP_MS", "1000")
        assert resolve_min_gap_seconds() == 1.0
        # A smaller value is ignored: the knob does not turn that way.
        monkeypatch.setenv("WORSAGA_MIN_REQUEST_GAP_MS", "10")
        assert resolve_min_gap_seconds() == DEFAULT_MIN_GAP_SECONDS
        monkeypatch.setenv("WORSAGA_MIN_REQUEST_GAP_MS", "0")
        assert resolve_min_gap_seconds() == DEFAULT_MIN_GAP_SECONDS

    def test_gap_has_a_sanity_ceiling(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_MIN_REQUEST_GAP_MS", "999999999")
        assert resolve_min_gap_seconds() == ratelimit.MAX_MIN_GAP_SECONDS

    def test_in_flight_may_be_lowered_only(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_MAX_IN_FLIGHT", "1")
        assert resolve_max_in_flight() == 1
        monkeypatch.setenv("WORSAGA_MAX_IN_FLIGHT", "16")
        assert resolve_max_in_flight() == DEFAULT_MAX_IN_FLIGHT

    @pytest.mark.parametrize("value", ["0", "-1", "-999"])
    def test_non_positive_in_flight_falls_back_to_the_default(
        self, monkeypatch, value,
    ):
        monkeypatch.setenv("WORSAGA_MAX_IN_FLIGHT", value)
        # Refused rather than quietly read as 1: it means nothing, and a
        # user who wrote it meant something else.
        assert resolve_max_in_flight() == DEFAULT_MAX_IN_FLIGHT

    def test_unparseable_values_fall_back(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_MIN_REQUEST_GAP_MS", "soon")
        monkeypatch.setenv("WORSAGA_MAX_IN_FLIGHT", "many")
        assert resolve_min_gap_seconds() == DEFAULT_MIN_GAP_SECONDS
        assert resolve_max_in_flight() == DEFAULT_MAX_IN_FLIGHT

    @pytest.mark.parametrize("value", [
        "nan", "NaN", "-nan", "inf", "Infinity", "-inf", "1e999", "-1e999",
        "0", "-250", "-0.0", "garbage", "", "  ",
    ])
    def test_no_value_can_disable_the_gap(self, monkeypatch, value):
        # float('nan') parses fine and then makes every max() in the
        # scheduler return its other operand - a silent off switch.
        monkeypatch.setenv("WORSAGA_MIN_REQUEST_GAP_MS", value)
        gap = resolve_min_gap_seconds()
        assert gap == DEFAULT_MIN_GAP_SECONDS
        assert gap > 0

    @pytest.mark.parametrize("value", [
        "nan", "inf", "-inf", "1e999", "2.5", "garbage",
    ])
    def test_no_value_can_disable_the_in_flight_cap(self, monkeypatch, value):
        monkeypatch.setenv("WORSAGA_MAX_IN_FLIGHT", value)
        assert resolve_max_in_flight() == DEFAULT_MAX_IN_FLIGHT

    def test_a_refused_setting_is_reported(self, monkeypatch, caplog):
        monkeypatch.setenv("WORSAGA_MIN_REQUEST_GAP_MS", "nan")
        with caplog.at_level("WARNING", logger="worsaga.ratelimit"):
            resolve_min_gap_seconds()
        # A knob that silently does nothing is worse than no knob.
        assert "WORSAGA_MIN_REQUEST_GAP_MS" in caplog.text
        assert "nan" in caplog.text

    def test_a_nan_gap_cannot_poison_the_scheduler(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_MIN_REQUEST_GAP_MS", "nan")
        clock = FakeClock()
        coordinator = OriginCoordinator(
            ORIGIN, sleep_fn=clock.sleep, monotonic=clock.monotonic,
            load_state=False,
        )
        starts = []
        for _ in range(3):
            with coordinator.request_slot() as start:
                starts.append(start)
        assert [round(b - a, 6) for a, b in zip(starts, starts[1:])] == [
            DEFAULT_MIN_GAP_SECONDS, DEFAULT_MIN_GAP_SECONDS,
        ]

    def test_there_is_no_off_switch(self):
        # Nothing in the module reads an env var that could disable it.
        source = (ratelimit.__doc__ or "") + str(ratelimit.__file__)
        assert "WORSAGA_RATELIMIT_OFF" not in source
        assert resolve_min_gap_seconds() > 0


# ── Pacing ─────────────────────────────────────────────────────────


class TestMinimumGap:
    def test_first_request_does_not_wait(self):
        coordinator = _coordinator(min_gap=0.25)
        with coordinator.request_slot():
            pass
        assert coordinator.clock.sleeps == []

    def test_sequential_starts_are_one_gap_apart(self):
        clock = FakeClock()
        coordinator = _coordinator(clock, min_gap=0.25)
        starts = []
        for _ in range(5):
            with coordinator.request_slot() as start:
                starts.append(start)
        gaps = [round(b - a, 6) for a, b in zip(starts, starts[1:])]
        assert gaps == [0.25, 0.25, 0.25, 0.25]
        assert coordinator.clock.sleeps == [0.25] * 4

    def test_an_idle_origin_pays_nothing(self):
        clock = FakeClock()
        coordinator = _coordinator(clock, min_gap=0.25)
        with coordinator.request_slot():
            pass
        clock.sleep(10)  # a long parse/cache phase between requests
        with coordinator.request_slot():
            pass
        # The second request was already more than a gap late.
        assert clock.sleeps == [10]


class TestConcurrentPacing:
    def test_threads_never_exceed_the_in_flight_cap_or_the_gap(self):
        """N threads, deterministic timeline, no real sleeping.

        The barrier is what makes the concurrency assertion mean
        something: workers only make progress in pairs, so if the
        semaphore allowed three at once the peak counter would see it.
        """
        workers = 6
        coordinator = _coordinator(min_gap=0.25, max_in_flight=2)
        paired = threading.Barrier(2, timeout=10)
        state_lock = threading.Lock()
        inside = 0
        peak = 0
        starts: list[float] = []

        def worker() -> None:
            nonlocal inside, peak
            with coordinator.request_slot() as start:
                with state_lock:
                    inside += 1
                    peak = max(peak, inside)
                    starts.append(start)
                paired.wait()
                with state_lock:
                    inside -= 1

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert all(not thread.is_alive() for thread in threads)
        # Exactly two on the wire: the pairs proved two are allowed, the
        # peak proves a third never was.
        assert peak == 2
        # Every reserved start is at least one gap after the previous one,
        # no matter which thread claimed it.
        ordered = sorted(starts)
        assert len(ordered) == workers
        for earlier, later in zip(ordered, ordered[1:]):
            assert round(later - earlier, 6) >= 0.25

    def test_waiting_threads_do_not_convoy(self):
        """Reservations are handed out under the lock, then slept outside.

        Six threads waiting on one origin sleep toward six successive
        slots at the same time; a convoying implementation would make each
        wait for the whole queue ahead of it.
        """
        coordinator = _coordinator(min_gap=0.25, max_in_flight=6)
        started = threading.Barrier(6, timeout=10)
        reserved: list[float] = []
        lock = threading.Lock()

        def worker() -> None:
            started.wait()
            with coordinator.request_slot() as start:
                with lock:
                    reserved.append(start)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        ordered = sorted(reserved)
        # Six distinct successive slots, one gap apart - not six threads
        # each waiting behind the whole queue.
        assert len(set(ordered)) == 6
        assert round(ordered[-1] - ordered[0], 6) == pytest.approx(0.25 * 5)


# ── Retry-After ────────────────────────────────────────────────────


class TestParseRetryAfter:
    def test_delta_seconds(self):
        assert parse_retry_after("30", now_ts=1000.0) == 30.0

    def test_http_date(self):
        # 1970-01-01T00:01:00Z is 60 seconds after the epoch.
        value = "Thu, 01 Jan 1970 00:01:00 GMT"
        assert parse_retry_after(value, now_ts=0.0) == 60.0

    def test_http_date_in_the_past_is_floored_at_zero(self):
        # Clock skew must never produce a negative wait.
        value = "Thu, 01 Jan 1970 00:00:10 GMT"
        assert parse_retry_after(value, now_ts=9999.0) == 0.0

    def test_negative_delta_is_floored_at_zero(self):
        assert parse_retry_after("-5", now_ts=1000.0) == 0.0

    @pytest.mark.parametrize("value", [
        "", None, "soon", "tomorrow-ish", "1e999", "3 0", "30s",
    ])
    def test_unusable_values_return_none(self, value):
        assert parse_retry_after(value, now_ts=1000.0) is None

    @pytest.mark.parametrize("value", [
        "9" * 400,                       # a 400-digit delta
        "-" + "9" * 400,
        "  30  ",                        # whitespace padded
        "Mon, 01 Jan 0001 00:00:00 GMT",  # absurdly far in the past
        "Fri, 31 Dec 9999 23:59:59 GMT",  # absurdly far in the future
    ])
    def test_no_header_value_can_raise(self, value):
        # float(int("9"*400)) raises OverflowError; the clamp happens on
        # the integer, before any conversion, so nothing escapes to the
        # CLI as a traceback.
        parsed = parse_retry_after(value, now_ts=1_700_000_000.0)
        if parsed is not None:
            assert 0.0 <= parsed <= float(ratelimit._MAX_SANE_DELTA_SECONDS)

    def test_an_absurd_delta_still_ends_up_capped(self):
        coordinator = _coordinator()
        delay, source = coordinator.note_backpressure(retry_after="9" * 400)
        assert source == "retry-after"
        assert delay == MAX_RETRY_AFTER_SECONDS

    def test_an_unparseable_header_falls_back_to_backoff(self):
        coordinator = _coordinator(rng=lambda: 1.0)
        delay, source = coordinator.note_backpressure(
            retry_after="1e999", attempt=1,
        )
        assert source == "backoff"
        assert delay == ratelimit.BACKOFF_BASE_SECONDS

    def test_honoured_wait_is_capped(self):
        coordinator = _coordinator()
        delay, source = coordinator.note_backpressure(retry_after="86400")
        assert source == "retry-after"
        assert delay == MAX_RETRY_AFTER_SECONDS

    def test_both_forms_reach_the_cooldown(self):
        for value in ("45", "Thu, 01 Jan 1970 00:00:45 GMT"):
            coordinator = _coordinator(now_fn=lambda: 0.0)
            delay, source = coordinator.note_backpressure(retry_after=value)
            assert source == "retry-after"
            assert delay == 45.0
            assert coordinator.cooldown_remaining() == 45.0


class TestBackoff:
    def test_full_jitter_is_uniform_over_the_capped_window(self):
        assert backoff_delay(1, rng=lambda: 1.0) == 1.0
        assert backoff_delay(1, rng=lambda: 0.0) == 0.0
        assert backoff_delay(2, rng=lambda: 1.0) == 2.0
        assert backoff_delay(3, rng=lambda: 1.0) == 4.0
        assert backoff_delay(3, rng=lambda: 0.25) == 1.0

    def test_backoff_is_capped(self):
        assert backoff_delay(50, rng=lambda: 1.0) == BACKOFF_CAP_SECONDS

    def test_absent_retry_after_uses_jittered_backoff(self):
        coordinator = _coordinator(rng=lambda: 1.0)
        delay, source = coordinator.note_backpressure(attempt=2)
        assert source == "backoff"
        assert delay == 2.0


# ── Cooldowns hold the whole origin ────────────────────────────────


class TestCooldown:
    def test_other_requests_wait_out_the_cooldown(self):
        clock = FakeClock()
        coordinator = _coordinator(clock, min_gap=0.25)
        coordinator.note_backpressure(retry_after="30")
        assert coordinator.cooldown_remaining() == 30.0
        with coordinator.request_slot():
            pass
        # The next request to this origin sat out the server's wait
        # instead of piling in behind the one that was refused.
        assert clock.sleeps[0] == 30.0
        assert coordinator.cooldown_remaining() == 0.0

    def test_cooldown_only_ever_moves_later(self):
        coordinator = _coordinator()
        coordinator.note_backpressure(retry_after="60")
        coordinator.note_backpressure(retry_after="5")
        assert coordinator.cooldown_remaining() == 60.0


class TestRetryBudget:
    def test_budget_is_shared_and_bounded(self):
        coordinator = _coordinator()
        assert all(coordinator.take_retry_budget() for _ in range(RETRY_BUDGET))
        # The (budget + 1)th retry across the whole origin is refused, so
        # four workers meeting one limit cannot retry in lockstep.
        assert coordinator.take_retry_budget() is False

    def test_budget_recovers_after_the_window(self):
        clock = FakeClock()
        coordinator = _coordinator(clock)
        for _ in range(RETRY_BUDGET):
            coordinator.take_retry_budget()
        assert coordinator.take_retry_budget() is False
        clock.sleep(ratelimit.RETRY_BUDGET_WINDOW_SECONDS + 1)
        assert coordinator.take_retry_budget() is True


# ── Cross-process persistence ──────────────────────────────────────


class TestPersistedCooldown:
    def test_cooldown_is_written_for_other_processes(self, tmp_path):
        coordinator = _coordinator(now_fn=lambda: 1_700_000_000.0)
        coordinator.note_backpressure(retry_after="60")
        state = json.loads(
            ratelimit.backpressure_state_path().read_text(encoding="utf-8")
        )
        entry = state["origins"][ORIGIN]
        assert entry["until"] == 1_700_000_060.0
        assert entry["source"] == "retry-after"
        assert state["version"] == ratelimit.BACKPRESSURE_STATE_VERSION

    def test_a_fresh_coordinator_honours_a_recorded_cooldown(self):
        # Simulates a second process: one coordinator records, another
        # (built from scratch) reads it back and starts out in cooldown.
        writer = _coordinator(now_fn=lambda: 1_700_000_000.0)
        writer.note_backpressure(retry_after="60")

        clock = FakeClock()
        reader = OriginCoordinator(
            ORIGIN,
            min_gap=0.25,
            sleep_fn=clock.sleep,
            monotonic=clock.monotonic,
            now_fn=lambda: 1_700_000_010.0,
            rng=lambda: 0.5,
        )
        assert reader.cooldown_remaining() == 50.0
        with reader.request_slot():
            pass
        assert clock.sleeps[0] == 50.0

    def test_expired_state_is_ignored(self):
        writer = _coordinator(now_fn=lambda: 1_700_000_000.0)
        writer.note_backpressure(retry_after="10")
        reader = OriginCoordinator(
            ORIGIN, now_fn=lambda: 1_700_009_999.0, sleep_fn=lambda s: None,
        )
        assert reader.cooldown_remaining() == 0.0

    def test_a_hostile_far_future_cooldown_is_capped(self):
        ratelimit.backpressure_state_path().parent.mkdir(
            parents=True, exist_ok=True
        )
        ratelimit.backpressure_state_path().write_text(
            json.dumps({"version": 1, "origins": {
                ORIGIN: {"until": 9_999_999_999.0, "source": "retry-after"},
            }}),
            encoding="utf-8",
        )
        clock = FakeClock()
        reader = OriginCoordinator(
            ORIGIN,
            now_fn=lambda: 1_700_000_000.0,
            monotonic=clock.monotonic,
            sleep_fn=clock.sleep,
        )
        # A corrupt or malicious record cannot freeze the client for years.
        assert reader.cooldown_remaining() == MAX_RETRY_AFTER_SECONDS

    @pytest.mark.parametrize("body", ["", "{not json", "[]", '{"origins": 3}'])
    def test_corrupt_state_is_ignored(self, body):
        path = ratelimit.backpressure_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        reader = OriginCoordinator(ORIGIN, sleep_fn=lambda s: None)
        assert reader.cooldown_remaining() == 0.0

    def test_missing_state_is_ignored(self):
        assert not ratelimit.backpressure_state_path().exists()
        reader = OriginCoordinator(ORIGIN, sleep_fn=lambda s: None)
        assert reader.cooldown_remaining() == 0.0

    def test_a_shorter_wait_never_shortens_a_stored_one(self):
        # Two processes: A is told to wait 100s, B is told 5s a moment
        # later. B's write must not release A - and every reader - early.
        writer = _coordinator(now_fn=lambda: 1_700_000_000.0)
        writer.note_backpressure(retry_after="100")
        stored = json.loads(
            ratelimit.backpressure_state_path().read_text(encoding="utf-8")
        )["origins"][ORIGIN]
        assert stored["until"] == 1_700_000_100.0

        other = OriginCoordinator(
            ORIGIN, sleep_fn=lambda s: None, now_fn=lambda: 1_700_000_010.0,
        )
        other.note_backpressure(retry_after="5")
        after = json.loads(
            ratelimit.backpressure_state_path().read_text(encoding="utf-8")
        )["origins"][ORIGIN]
        assert after["until"] == 1_700_000_100.0
        assert after["source"] == "retry-after"

    def test_a_longer_wait_does_replace_a_stored_one(self):
        writer = _coordinator(now_fn=lambda: 1_700_000_000.0)
        writer.note_backpressure(retry_after="5")
        other = OriginCoordinator(
            ORIGIN, sleep_fn=lambda s: None, now_fn=lambda: 1_700_000_001.0,
        )
        other.note_backpressure(retry_after="90")
        after = json.loads(
            ratelimit.backpressure_state_path().read_text(encoding="utf-8")
        )["origins"][ORIGIN]
        assert after["until"] == 1_700_000_091.0

    def test_an_expired_stored_deadline_does_not_win(self):
        writer = _coordinator(now_fn=lambda: 1_700_000_000.0)
        writer.note_backpressure(retry_after="30")
        # Long after that wait ended, a new short one is what applies.
        later = OriginCoordinator(
            ORIGIN, sleep_fn=lambda s: None, now_fn=lambda: 1_700_009_999.0,
        )
        later.note_backpressure(retry_after="10")
        after = json.loads(
            ratelimit.backpressure_state_path().read_text(encoding="utf-8")
        )["origins"][ORIGIN]
        assert after["until"] == 1_700_010_009.0

    def test_a_live_coordinator_picks_up_a_new_cooldown_on_acquire(self):
        # A long-lived watch loop must learn about a cooldown a CLI
        # process wrote after it started, not only at construction.
        clock = FakeClock()
        live = OriginCoordinator(
            ORIGIN, min_gap=0.0, sleep_fn=clock.sleep,
            monotonic=clock.monotonic, now_fn=lambda: 1_700_000_000.0,
        )
        assert live.cooldown_remaining() == 0.0

        writer = OriginCoordinator(
            ORIGIN, sleep_fn=lambda s: None, now_fn=lambda: 1_700_000_000.0,
            load_state=False,
        )
        writer.note_backpressure(retry_after="45")

        with live.request_slot():
            pass
        assert clock.sleeps == [45.0]

    def test_the_refresh_is_rate_limited(self, monkeypatch):
        clock = FakeClock()
        live = OriginCoordinator(
            ORIGIN, min_gap=0.0, sleep_fn=clock.sleep,
            monotonic=clock.monotonic, now_fn=lambda: 1_700_000_000.0,
        )
        stats = {"n": 0}
        real = live._state_fingerprint

        def counting():
            stats["n"] += 1
            return real()

        monkeypatch.setattr(live, "_state_fingerprint", counting)
        for _ in range(20):
            with live.request_slot():
                pass
        # A four-worker fan-out must not stat the state file per request.
        assert stats["n"] == 1

    def test_an_unchanged_file_is_not_reparsed(self, monkeypatch):
        clock = FakeClock()
        live = OriginCoordinator(
            ORIGIN, min_gap=0.0, sleep_fn=clock.sleep,
            monotonic=clock.monotonic, now_fn=lambda: 1_700_000_000.0,
        )
        parses = {"n": 0}
        real_read = ratelimit._read_state

        def counting(path):
            parses["n"] += 1
            return real_read(path)

        monkeypatch.setattr(ratelimit, "_read_state", counting)
        for _ in range(5):
            clock.sleep(ratelimit.STATE_REFRESH_SECONDS + 0.1)
            with live.request_slot():
                pass
        # The mtime/size stamp is unchanged, so nothing is re-parsed.
        assert parses["n"] == 0

    def test_expired_entries_are_pruned_on_write(self):
        path = ratelimit.backpressure_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 1, "origins": {
                "https://old.example.edu": {"until": 1.0, "source": "backoff"},
                "https://live.example.edu": {
                    "until": 1_700_000_500.0, "source": "retry-after",
                },
            }}),
            encoding="utf-8",
        )
        coordinator = _coordinator(now_fn=lambda: 1_700_000_000.0)
        coordinator.note_backpressure(retry_after="30")
        origins = json.loads(path.read_text(encoding="utf-8"))["origins"]
        assert "https://old.example.edu" not in origins
        assert "https://live.example.edu" in origins
        assert ORIGIN in origins

    def test_state_file_is_owner_only(self):
        import os
        import stat

        coordinator = _coordinator()
        coordinator.note_backpressure(retry_after="30")
        path = ratelimit.backpressure_state_path()
        if os.name != "posix":
            pytest.skip("POSIX mode bits only")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


class TestRegistry:
    def test_reset_clears_state_between_uses(self):
        first = coordinator_for(ORIGIN)
        first.note_backpressure(retry_after="60")
        ratelimit.for_testing_reset(sleep_fn=lambda s: None, load_state=False)
        second = coordinator_for(ORIGIN)
        assert second is not first
        assert second.cooldown_remaining() == 0.0

    def test_registered_coordinator_is_returned(self):
        coordinator = _coordinator()
        ratelimit.for_testing_register(coordinator)
        assert coordinator_for(f"{ORIGIN}/moodle") is coordinator


def test_max_attempts_is_small():
    # Three attempts total, not three retries per worker per request.
    assert MAX_ATTEMPTS == 3
