"""Tests for sync outcome truthfulness, failure classes, and the circuit."""

import json
import urllib.error

import pytest
from unittest.mock import patch

from worsaga import syncstate
from worsaga.cache import CacheStore
from worsaga.client import (
    DownloadError,
    MoodleRateLimitedError,
    MoodleRequestError,
    MoodleWriteAttemptError,
)
from worsaga.sync import run_sync, sync_outcome
from worsaga.syncstate import (
    circuit_state,
    classify_failure,
    read_site_state,
    record_outcome,
    worst_failure_class,
)

SITE = "https://moodle.example.edu"


def _categories(**synced):
    return {
        name: {"synced": flag, "items": 0, "new": 0, "updated": 0,
               "adopted": 0, "baseline": False}
        for name, flag in synced.items()
    }


class _FakeClient:
    """A client whose per-category fetches can be told to fail."""

    base_url = SITE
    is_demo = False

    def __init__(self, *, courses=None, fail=None):
        self._courses = courses if courses is not None else [
            {"id": 101, "shortname": "ECON101", "fullname": "Economics"},
        ]
        self._fail = fail

    def get_courses(self):
        if isinstance(self._fail, BaseException):
            raise self._fail
        return list(self._courses)

    def enrolled_course_ids(self):
        return frozenset(int(c["id"]) for c in self._courses)

    @property
    def verified_userid(self):
        return 4242

    def get_assignments_by_courses(self, course_ids):
        return {"courses": []}

    def get_quizzes(self, course_ids=None):
        return {"quizzes": []}

    def get_course_contents(self, course_id):
        return []

    def get_user_grade_items(self, course_id):
        return {"usergrades": []}

    def get_forums_by_courses(self, course_ids):
        return {"forums": []}

    def get_forum_discussions(self, forum_id):
        return {"discussions": []}


# ── The outcome rule ───────────────────────────────────────────────


class TestSyncOutcome:
    def test_all_synced_is_success(self):
        assert sync_outcome(_categories(a=True, b=True)) == "success"

    def test_some_synced_is_partial(self):
        assert sync_outcome(_categories(a=True, b=False)) == "partial"

    def test_none_synced_is_failed(self):
        assert sync_outcome(_categories(a=False, b=False)) == "failed"

    def test_no_categories_at_all_is_failed(self):
        assert sync_outcome({}) == "failed"


class TestRunSyncOutcome:
    def test_successful_run_reports_success(self, tmp_path):
        result = run_sync(_FakeClient(), cache_path=tmp_path / "cache.db")
        assert result["outcome"] == "success"
        assert "failure_class" not in result

    def test_zero_categories_is_a_failed_run(self, tmp_path):
        client = _FakeClient(fail=OSError("network down"))
        result = run_sync(client, cache_path=tmp_path / "cache.db")
        assert result["outcome"] == "failed"
        assert result["failure_class"] == "network"
        # The defect this fixes: an empty change list from a run that
        # fetched nothing used to be indistinguishable from a clean run.
        assert result["changes"] == []
        assert all(
            not stats["synced"] for stats in result["categories"].values()
        )

    def test_partial_run_reports_partial(self, tmp_path):
        client = _FakeClient()
        with patch.object(
            type(client), "get_forums_by_courses",
            side_effect=OSError("forums unavailable"),
        ):
            result = run_sync(client, cache_path=tmp_path / "cache.db")
        assert result["outcome"] == "partial"
        assert any("forums" in w for w in result["warnings"])

    def test_failed_run_does_not_advance_the_last_sync_time(self, tmp_path):
        cache_path = tmp_path / "cache.db"
        run_sync(_FakeClient(), cache_path=cache_path, now=1_700_000_000)
        with CacheStore(cache_path) as cache:
            after_success = cache.last_sync_at(SITE)

        run_sync(
            _FakeClient(fail=OSError("network down")),
            cache_path=cache_path, now=1_700_009_999,
        )
        with CacheStore(cache_path) as cache:
            after_failure = cache.last_sync_at(SITE)
        # "Last synced 3 seconds ago" must never describe a run that
        # fetched nothing at all.
        assert after_failure == after_success

    def test_every_gradebook_failing_is_not_a_synced_category(self, tmp_path):
        rejected = MoodleRequestError("Invalid token", errorcode="invalidtoken")

        class _NoGradebooks(_FakeClient):
            def get_user_grade_items(self, course_id):
                raise rejected

        result = run_sync(_NoGradebooks(), cache_path=tmp_path / "cache.db")
        # collect_grades tolerates per-course failures, so it returned an
        # empty list happily. An empty list is only ever "no grades" when
        # some course could actually have supplied them.
        assert result["categories"]["grades"]["synced"] is False
        assert any("no gradebook could be read" in w for w in result["warnings"])

    def test_every_gradebook_failing_on_auth_opens_the_circuit(self, tmp_path):
        rejected = MoodleRequestError("Invalid token", errorcode="invalidtoken")

        class _NoGradebooks(_FakeClient):
            def get_user_grade_items(self, course_id):
                raise rejected

            def get_course_contents(self, course_id):
                raise rejected

            def get_forums_by_courses(self, course_ids):
                raise rejected

            def get_assignments_by_courses(self, course_ids):
                raise rejected

            def get_quizzes(self, course_ids=None):
                raise rejected

        result = run_sync(_NoGradebooks(), cache_path=tmp_path / "cache.db")
        assert result["outcome"] == "failed"
        assert result["failure_class"] == "auth"
        assert circuit_state(SITE) is not None

    def test_one_failing_gradebook_of_three_is_still_partial_coverage(
        self, tmp_path,
    ):
        class _OneBadGradebook(_FakeClient):
            def __init__(self):
                super().__init__(courses=[
                    {"id": 101, "shortname": "ECON101"},
                    {"id": 102, "shortname": "CS210"},
                    {"id": 103, "shortname": "PSY110"},
                ])

            def get_user_grade_items(self, course_id):
                if course_id == 102:
                    raise RuntimeError("Cannot view grades")
                return {"usergrades": []}

        result = run_sync(_OneBadGradebook(), cache_path=tmp_path / "cache.db")
        # Unchanged behaviour: partial gradebook coverage is still a
        # synced category with a warning naming the course.
        assert result["categories"]["grades"]["synced"] is True
        assert result["outcome"] == "success"
        assert any("CS210" in w for w in result["warnings"])

    def test_no_enrolled_courses_at_all_is_still_a_success(self, tmp_path):
        result = run_sync(
            _FakeClient(courses=[]), cache_path=tmp_path / "cache.db",
        )
        # A genuinely empty Moodle is an empty sync, not a failed one.
        assert result["outcome"] == "success"
        assert result["categories"]["grades"]["synced"] is True

    def test_files_and_forums_fan_outs_are_strict(self, tmp_path):
        class _OneBadCourse(_FakeClient):
            def __init__(self):
                super().__init__(courses=[
                    {"id": 101, "shortname": "ECON101"},
                    {"id": 102, "shortname": "CS210"},
                ])

            def get_course_contents(self, course_id):
                if course_id == 102:
                    raise RuntimeError("no access")
                return []

        result = run_sync(_OneBadCourse(), cache_path=tmp_path / "cache.db")
        # No swallowed-every-unit edge to guard here: one failed course
        # propagates and the whole category is skipped.
        assert result["categories"]["files"]["synced"] is False
        assert result["outcome"] == "partial"

    def test_principal_suppressed_run_is_failed(self, tmp_path):
        cache_path = tmp_path / "cache.db"
        # A cache already bound to an account, and a run that verified no
        # identity of its own because nothing could be fetched.
        with CacheStore(cache_path) as cache:
            cache.begin_immediate()
            assert cache.bind_principal(SITE, 111)
            cache.commit()

        class _Unverified(_FakeClient):
            @property
            def verified_userid(self):
                return None

        result = run_sync(
            _Unverified(fail=OSError("network down")), cache_path=cache_path,
        )
        # This is the run that used to look identical to a clean sync
        # with nothing new: no rows written, no changes, and (before)
        # no way at all for a caller to tell.
        assert result["outcome"] == "failed"
        assert any("local cache" in w for w in result["warnings"])
        assert result["changes"] == []


# ── Failure classification ─────────────────────────────────────────


class TestClassifyFailure:
    def test_rate_limit(self):
        assert classify_failure(MoodleRateLimitedError("x")) == "rate_limited"

    def test_auth_errorcode(self):
        exc = MoodleRequestError("nope", errorcode="invalidtoken")
        assert classify_failure(exc) == "auth"

    def test_service_disabled_is_auth(self):
        exc = MoodleRequestError("off", errorcode="servicenotavailable")
        assert classify_failure(exc) == "auth"

    def test_http_401_is_auth(self):
        exc = urllib.error.HTTPError("u", 401, "no", None, None)
        assert classify_failure(exc) == "auth"

    def test_http_429_is_rate_limited(self):
        exc = urllib.error.HTTPError("u", 429, "slow", None, None)
        assert classify_failure(exc) == "rate_limited"

    def test_url_error_is_network(self):
        assert classify_failure(urllib.error.URLError("down")) == "network"

    def test_timeout_is_network(self):
        assert classify_failure(TimeoutError()) == "network"

    def test_download_error_codes_map_through(self):
        assert classify_failure(DownloadError("auth", "x")) == "auth"
        assert classify_failure(DownloadError("network", "x")) == "network"
        assert classify_failure(DownloadError("rate_limited", "x")) == "rate_limited"
        assert classify_failure(DownloadError("oversize", "x")) == "other"

    def test_unknown_is_other(self):
        assert classify_failure(ValueError("what")) == "other"
        assert classify_failure(None) == "other"

    def test_worst_class_prefers_the_actionable_one(self):
        assert worst_failure_class(["network", "auth"]) == "auth"
        assert worst_failure_class(["other", "rate_limited"]) == "rate_limited"
        assert worst_failure_class(["other", "network"]) == "network"
        assert worst_failure_class(["other"]) == "other"
        assert worst_failure_class([]) == "other"


# ── The recorded history ───────────────────────────────────────────


class TestRecordOutcome:
    def test_success_resets_the_streak(self):
        record_outcome(SITE, "failed", failure_class="network", now=10)
        record_outcome(SITE, "failed", failure_class="network", now=20)
        assert read_site_state(SITE)["consecutive_failures"] == 2
        state = record_outcome(SITE, "success", now=30)
        assert state["consecutive_failures"] == 0
        assert state["circuit_open"] is False
        assert state["last_success_at"] == 30

    def test_partial_also_resets(self):
        record_outcome(SITE, "failed", failure_class="auth", now=10)
        state = record_outcome(SITE, "partial", now=20)
        assert state["consecutive_failures"] == 0
        assert state["circuit_open"] is False

    def test_skipped_changes_neither_counter(self):
        record_outcome(SITE, "failed", failure_class="network", now=10)
        state = record_outcome(SITE, "skipped", now=20)
        # Another process was syncing: that says nothing about this site.
        assert state["consecutive_failures"] == 1
        assert state["last_outcome"] == "skipped"
        assert "last_success_at" not in state

    def test_auth_failure_opens_the_circuit(self):
        state = record_outcome(SITE, "failed", failure_class="auth", now=10)
        assert state["circuit_open"] is True
        assert circuit_state(SITE) is not None

    def test_network_failure_does_not_open_the_circuit(self):
        record_outcome(SITE, "failed", failure_class="network", now=10)
        # A flaky network is temporary; retrying it is right.
        assert circuit_state(SITE) is None

    def test_rate_limit_does_not_open_the_circuit(self):
        record_outcome(SITE, "failed", failure_class="rate_limited", now=10)
        assert circuit_state(SITE) is None

    def test_state_is_per_site(self):
        record_outcome(SITE, "failed", failure_class="auth", now=10)
        assert circuit_state("https://other.example.edu") is None

    def test_state_survives_a_fresh_read(self):
        record_outcome(SITE, "failed", failure_class="auth", now=10)
        # A different process reads the same file with no shared memory.
        written = json.loads(
            syncstate.sync_state_path().read_text(encoding="utf-8")
        )
        assert written["sites"][SITE]["circuit_open"] is True
        assert written["version"] == syncstate.SYNC_STATE_VERSION

    @pytest.mark.parametrize("body", ["", "{bad", "[]", '{"sites": 4}'])
    def test_corrupt_state_reads_as_absent(self, body):
        path = syncstate.sync_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        assert read_site_state(SITE) == {}
        assert circuit_state(SITE) is None

    def _write_raw(self, entry):
        path = syncstate.sync_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 1, "sites": {SITE: entry}}), encoding="utf-8",
        )

    @pytest.mark.parametrize("entry", [
        {"consecutive_failures": "corrupt", "circuit_open": True,
         "failure_class": "auth"},
        {"consecutive_failures": None, "failure_class": "nonsense",
         "circuit_open": "yes"},
        {"consecutive_failures": [], "last_outcome": 7,
         "last_run_at": "when"},
        {"last_outcome": "unknown-outcome"},
        "not even a dict",
        42,
    ])
    def test_corrupt_fields_never_crash_a_read(self, entry):
        # This file is plain JSON in a directory the user owns, so it will
        # be hand-edited. An unattended watch loop must degrade, not die.
        self._write_raw(entry)
        state = read_site_state(SITE)
        assert isinstance(state, dict)
        assert isinstance(state.get("consecutive_failures", 0), int)
        circuit_state(SITE)  # must not raise either

    def test_a_corrupt_counter_does_not_crash_the_circuit_message(self):
        self._write_raw({
            "consecutive_failures": "corrupt", "circuit_open": True,
            "failure_class": "auth",
        })
        entry = circuit_state(SITE)
        assert entry is not None
        assert "0 consecutive authentication failures" in syncstate.circuit_message(
            entry
        )

    def test_a_corrupt_failure_class_closes_the_circuit(self):
        self._write_raw({"circuit_open": True, "failure_class": "gibberish"})
        # Fail open: the worst case is one attempted sync, whereas a
        # wrongly-open circuit silently stops syncing forever.
        assert circuit_state(SITE) is None

    def test_a_corrupt_counter_recovers_on_the_next_write(self):
        self._write_raw({"consecutive_failures": "corrupt"})
        state = record_outcome(SITE, "failed", failure_class="network", now=99)
        assert state["consecutive_failures"] == 1


class TestStateIsSerialised:
    def test_a_skipped_run_cannot_resurrect_a_cleared_circuit(self):
        record_outcome(SITE, "failed", failure_class="auth", now=10)
        assert circuit_state(SITE) is not None
        record_outcome(SITE, "success", now=20)
        # The lock-losing run writes its outcome last, merging onto bytes
        # it must re-read rather than a snapshot from before the clear.
        record_outcome(SITE, "skipped", now=30)
        assert circuit_state(SITE) is None
        assert read_site_state(SITE)["consecutive_failures"] == 0

    def test_the_read_happens_inside_the_critical_section(self, monkeypatch):
        seen = []
        real_read = syncstate._read_all

        def _tracking(path=None):
            seen.append(syncstate.state_lock_path().exists())
            return real_read(path)

        monkeypatch.setattr(syncstate, "_read_all", _tracking)
        record_outcome(SITE, "failed", failure_class="auth", now=10)
        # A read taken before the lock is the lost-update bug itself.
        assert seen and all(seen)

    def test_the_state_lock_is_released(self):
        record_outcome(SITE, "failed", failure_class="auth", now=10)
        assert not syncstate.state_lock_path().exists()

    def test_a_held_state_lock_never_blocks_a_write(self, monkeypatch):
        from worsaga.synclock import SyncLock

        holder = SyncLock(
            "worsaga-syncstate", syncstate.sync_state_path(),
            path=syncstate.state_lock_path(),
            ttl_seconds=syncstate.STATE_LOCK_TTL_SECONDS,
        )
        assert holder.acquire() is True
        monkeypatch.setattr(syncstate, "_LOCK_ATTEMPTS", 1)
        try:
            # Advisory in the strictest sense: state this small is never
            # worth failing a sync over.
            state = record_outcome(SITE, "failed", failure_class="auth", now=1)
        finally:
            holder.release()
        assert state["circuit_open"] is True
        assert read_site_state(SITE)["circuit_open"] is True

    def test_concurrent_writes_to_two_sites_both_survive(self):
        import threading

        other = "https://moodle.other.example.edu"
        start = threading.Barrier(2, timeout=10)

        def _write(site, outcome, failure_class):
            start.wait()
            for _ in range(20):
                record_outcome(site, outcome, failure_class=failure_class)

        threads = [
            threading.Thread(target=_write, args=(SITE, "failed", "auth")),
            threading.Thread(target=_write, args=(other, "failed", "network")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert all(not thread.is_alive() for thread in threads)

        # Neither site erased the other.
        assert read_site_state(SITE)["consecutive_failures"] == 20
        assert read_site_state(other)["consecutive_failures"] == 20
        assert read_site_state(SITE)["failure_class"] == "auth"
        assert read_site_state(other)["failure_class"] == "network"

    def test_an_unwritable_state_file_never_fails_a_sync(self, monkeypatch):
        def _refuse(*args, **kwargs):
            raise OSError("read-only volume")

        monkeypatch.setattr(syncstate, "write_private_file", _refuse)
        # Best effort in both directions: the state is an aid, never a
        # precondition.
        assert record_outcome(SITE, "failed", failure_class="auth")["circuit_open"]


# ── The circuit in run_sync ────────────────────────────────────────


class TestCircuitBreaker:
    def test_unattended_run_stops_before_the_network(self, tmp_path):
        record_outcome(SITE, "failed", failure_class="auth", now=10)

        class _Exploding(_FakeClient):
            def get_courses(self):
                raise AssertionError("no request may be made")

        result = run_sync(
            _Exploding(), cache_path=tmp_path / "cache.db", unattended=True,
        )
        assert result["outcome"] == "failed"
        assert result["circuit_open"] is True
        assert "circuit open" in result["warnings"][0]
        assert "worsaga sync" in result["warnings"][0]

    def test_a_refused_run_creates_no_cache(self, tmp_path):
        record_outcome(SITE, "failed", failure_class="auth", now=10)
        cache_path = tmp_path / "cache.db"
        run_sync(_FakeClient(), cache_path=cache_path, unattended=True)
        # A run that is deliberately not happening must not bring the
        # store into existence as a side effect.
        assert not cache_path.exists()

    def test_foreground_run_always_attempts(self, tmp_path):
        record_outcome(SITE, "failed", failure_class="auth", now=10)
        result = run_sync(_FakeClient(), cache_path=tmp_path / "cache.db")
        assert result["outcome"] == "success"

    def test_a_successful_manual_sync_closes_the_circuit(self, tmp_path):
        record_outcome(SITE, "failed", failure_class="auth", now=10)
        assert circuit_state(SITE) is not None
        run_sync(_FakeClient(), cache_path=tmp_path / "cache.db")
        assert circuit_state(SITE) is None
        # And the unattended run works again afterwards.
        result = run_sync(
            _FakeClient(), cache_path=tmp_path / "cache.db", unattended=True,
        )
        assert result["outcome"] == "success"

    def test_repeated_auth_failures_open_it_through_run_sync(self, tmp_path):
        rejected = MoodleRequestError("bad token", errorcode="invalidtoken")
        result = run_sync(
            _FakeClient(fail=rejected), cache_path=tmp_path / "cache.db",
        )
        assert result["failure_class"] == "auth"
        assert circuit_state(SITE) is not None

    def test_demo_runs_leave_no_state_behind(self, tmp_path):
        class _Demo(_FakeClient):
            is_demo = True

        run_sync(_Demo(), cache_path=tmp_path / "cache.db")
        assert not syncstate.sync_state_path().exists()


# ── The lock in run_sync ───────────────────────────────────────────


class TestRunSyncLock:
    def test_a_second_run_is_skipped_not_duplicated(self, tmp_path):
        from worsaga.synclock import SyncLock

        cache_path = tmp_path / "cache.db"
        holder = SyncLock(SITE, cache_path)
        assert holder.acquire()
        try:
            class _Exploding(_FakeClient):
                def get_courses(self):
                    raise AssertionError("must not fetch while locked")

            result = run_sync(_Exploding(), cache_path=cache_path)
        finally:
            holder.release()

        assert result["outcome"] == "skipped"
        assert result["skipped_reason"] == "sync_in_progress"
        assert "already running" in result["warnings"][0]

    def test_a_skipped_run_does_not_disturb_the_failure_streak(self, tmp_path):
        from worsaga.synclock import SyncLock

        record_outcome(SITE, "failed", failure_class="network", now=10)
        cache_path = tmp_path / "cache.db"
        holder = SyncLock(SITE, cache_path)
        holder.acquire()
        try:
            run_sync(_FakeClient(), cache_path=cache_path)
        finally:
            holder.release()
        assert read_site_state(SITE)["consecutive_failures"] == 1

    def test_the_lock_is_released_after_a_normal_run(self, tmp_path):
        from worsaga.synclock import lock_path

        cache_path = tmp_path / "cache.db"
        run_sync(_FakeClient(), cache_path=cache_path)
        assert not lock_path(SITE, cache_path).exists()

    def test_the_lock_is_released_when_the_run_raises(self, tmp_path):
        from worsaga.synclock import lock_path

        cache_path = tmp_path / "cache.db"

        class _Unsafe(_FakeClient):
            def get_courses(self):
                raise MoodleWriteAttemptError("blocked")

        with pytest.raises(MoodleWriteAttemptError):
            run_sync(_Unsafe(), cache_path=cache_path)
        # A lock left behind by an exception would block every later sync
        # for the whole TTL.
        assert not lock_path(SITE, cache_path).exists()

    def test_demo_runs_take_no_lock(self, tmp_path):
        from worsaga.synclock import lock_path

        class _Demo(_FakeClient):
            is_demo = True

        cache_path = tmp_path / "cache.db"
        run_sync(_Demo(), cache_path=cache_path)
        assert not lock_path(SITE, cache_path).exists()
