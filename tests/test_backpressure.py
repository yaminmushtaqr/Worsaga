"""Wire-level tests: 429/503 backpressure and bounded, typed responses.

The coordinator these tests drive is registered explicitly with an
injected clock, so a "wait" is a recorded number and nothing sleeps.
"""

import email.message
import json
import threading
import urllib.error
import urllib.parse

import pytest
from unittest.mock import patch

from worsaga import ratelimit
from worsaga.client import (
    MAX_RESPONSE_BYTES,
    DownloadError,
    MoodleClient,
    MoodleRateLimitedError,
    MoodleRequestError,
    MoodleResponseError,
    MoodleWriteAttemptError,
)
from worsaga.config import MoodleConfig

SITE = "https://moodle.example.edu"


class FakeClock:
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


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def coordinator(clock):
    """A coordinator for SITE with no pacing gap and a fake clock."""
    made = ratelimit.OriginCoordinator(
        ratelimit.origin_of(SITE),
        min_gap=0.0,
        sleep_fn=clock.sleep,
        monotonic=clock.monotonic,
        now_fn=lambda: 1_700_000_000.0,
        rng=lambda: 1.0,
        load_state=False,
    )
    ratelimit.for_testing_register(made)
    return made


@pytest.fixture()
def client():
    cfg = MoodleConfig(url=SITE, token="fake-token", userid=1)
    return MoodleClient(config=cfg)


class _Response:
    def __init__(self, payload: bytes, headers: dict | None = None):
        self.payload = payload
        self.headers = headers or {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=None):
        return self.payload if size is None else self.payload[:size]


def _http_error(code: int, retry_after: str | None = None):
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        f"{SITE}/webservice/rest/server.php", code, "throttled", headers, None,
    )


class _Transport:
    """A urlopen stand-in that can answer, or refuse, per request."""

    def __init__(self, *, userid=1, courses=(), payload=b"{}", script=None):
        self.userid = userid
        self.courses = list(courses)
        self.payload = payload
        self.script = list(script or [])
        self.calls: list[str] = []

    def __call__(self, req, timeout=30):
        params = urllib.parse.parse_qs((req.data or b"").decode())
        wsfunction = (params.get("wsfunction") or [""])[0]
        self.calls.append(wsfunction)
        if wsfunction == "core_webservice_get_site_info":
            return _Response(json.dumps({"userid": self.userid}).encode())
        if wsfunction == "core_enrol_get_users_courses" and self.courses:
            return _Response(json.dumps(self.courses).encode())
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step
        return _Response(self.payload)


# ── Retry-After, both forms ────────────────────────────────────────


class TestRetryAfterHonoured:
    def test_delta_seconds_is_waited_out_then_the_call_succeeds(
        self, client, coordinator, clock,
    ):
        transport = _Transport(script=[
            _http_error(429, "30"),
            _Response(json.dumps([{"id": 1}]).encode()),
        ])
        with patch("urllib.request.urlopen", side_effect=transport):
            result = client.call("core_enrol_get_users_courses")
        assert result == [{"id": 1}]
        # One wait, exactly as long as the server asked for.
        assert clock.sleeps == [30.0]

    def test_http_date_is_waited_out(self, client, coordinator, clock):
        # now_fn is fixed at 1_700_000_000 -> 2023-11-14T22:13:20Z.
        transport = _Transport(script=[
            _http_error(429, "Tue, 14 Nov 2023 22:14:20 GMT"),
            _Response(b"[]"),
        ])
        with patch("urllib.request.urlopen", side_effect=transport):
            client.call("core_enrol_get_users_courses")
        assert clock.sleeps == [60.0]

    def test_wait_is_capped(self, client, coordinator, clock):
        transport = _Transport(script=[_http_error(503, "86400"), _Response(b"[]")])
        with patch("urllib.request.urlopen", side_effect=transport):
            client.call("core_enrol_get_users_courses")
        assert clock.sleeps == [ratelimit.MAX_RETRY_AFTER_SECONDS]

    def test_absent_header_uses_jittered_backoff(
        self, client, coordinator, clock,
    ):
        transport = _Transport(script=[_http_error(503), _Response(b"[]")])
        with patch("urllib.request.urlopen", side_effect=transport):
            client.call("core_enrol_get_users_courses")
        # rng is pinned to 1.0, so full jitter yields the whole first step.
        assert clock.sleeps == [ratelimit.BACKOFF_BASE_SECONDS]

    def test_no_tight_retry_every_retry_waits(self, client, coordinator, clock):
        transport = _Transport(script=[
            _http_error(429, "5"), _http_error(429, "7"), _Response(b"[]"),
        ])
        with patch("urllib.request.urlopen", side_effect=transport):
            client.call("core_enrol_get_users_courses")
        assert clock.sleeps == [5.0, 7.0]
        assert all(wait > 0 for wait in clock.sleeps)


class TestExhaustion:
    def test_gives_up_after_max_attempts(self, client, coordinator, clock):
        transport = _Transport(script=[_http_error(429, "1")] * 5)
        with patch("urllib.request.urlopen", side_effect=transport):
            with pytest.raises(MoodleRateLimitedError) as exc:
                client.call("core_enrol_get_users_courses")
        assert exc.value.status == 429
        assert "rate limited" in str(exc.value)
        # Attempts, not retries: three requests went out, two waits happened.
        assert transport.calls.count("core_enrol_get_users_courses") == 3
        assert len(clock.sleeps) == 2

    def test_error_message_is_actionable_and_token_free(
        self, client, coordinator,
    ):
        transport = _Transport(script=[_http_error(429, "1")] * 5)
        with patch("urllib.request.urlopen", side_effect=transport):
            with pytest.raises(MoodleRateLimitedError) as exc:
                client.call("core_enrol_get_users_courses")
        message = str(exc.value)
        assert "try again later" in message
        assert "fake-token" not in message

    def test_shared_budget_stops_lockstep_retries(
        self, client, coordinator, clock,
    ):
        # Spend the origin's whole retry budget elsewhere first: the next
        # request must fail fast rather than retrying on its own account.
        for _ in range(ratelimit.RETRY_BUDGET):
            coordinator.take_retry_budget()
        transport = _Transport(script=[_http_error(429, "1")] * 5)
        with patch("urllib.request.urlopen", side_effect=transport):
            with pytest.raises(MoodleRateLimitedError) as exc:
                client.call("core_enrol_get_users_courses")
        assert "shared retry budget" in str(exc.value)
        assert transport.calls.count("core_enrol_get_users_courses") == 1

    def test_a_refusal_still_slows_the_whole_origin(
        self, client, coordinator, clock,
    ):
        for _ in range(ratelimit.RETRY_BUDGET):
            coordinator.take_retry_budget()
        transport = _Transport(script=[_http_error(429, "45")] * 5)
        with patch("urllib.request.urlopen", side_effect=transport):
            with pytest.raises(MoodleRateLimitedError):
                client.call("core_enrol_get_users_courses")
        # Even a request that gave up leaves the cooldown behind for
        # every other caller on this origin.
        assert coordinator.cooldown_remaining() == 45.0


class TestCooldownIsInstalledBeforeTheSlotIsFreed:
    """A queued worker must never be admitted before the cooldown exists.

    With ``max_in_flight = 1`` the second thread can only be admitted when
    the first releases its slot, so the recorded event order is exactly
    the question at issue: is the cooldown in place by then?
    """

    def test_a_queued_thread_cannot_start_before_the_cooldown(self, clock):
        one_at_a_time = ratelimit.OriginCoordinator(
            ratelimit.origin_of(SITE), min_gap=0.0, max_in_flight=1,
            sleep_fn=clock.sleep, monotonic=clock.monotonic,
            now_fn=lambda: 1_700_000_000.0, rng=lambda: 1.0,
            load_state=False,
        )
        ratelimit.for_testing_register(one_at_a_time)

        events: list[tuple] = []
        events_lock = threading.Lock()
        first_on_the_wire = threading.Event()
        second_is_queued = threading.Event()

        real_note = one_at_a_time.note_backpressure

        def _recording_note(**kwargs):
            result = real_note(**kwargs)
            with events_lock:
                events.append(("cooldown", result[0]))
            return result

        one_at_a_time.note_backpressure = _recording_note

        refused = {"done": False}

        def transport(req, timeout=30):
            name = threading.current_thread().name
            params = urllib.parse.parse_qs((req.data or b"").decode())
            wsfunction = (params.get("wsfunction") or [""])[0]
            with events_lock:
                events.append(("wire", name))
            if wsfunction == "core_webservice_get_site_info":
                return _Response(json.dumps({"userid": 1}).encode())
            if name == "first" and not refused["done"]:
                refused["done"] = True
                first_on_the_wire.set()
                # Hold the slot until the second worker is on its way, so
                # the release/cooldown window is as wide as it can get.
                second_is_queued.wait(timeout=10)
                raise _http_error(429, "30")
            return _Response(b"[]")

        cfg = MoodleConfig(url=SITE, token="fake-token", userid=1)
        first_client = MoodleClient(config=cfg)
        second_client = MoodleClient(config=cfg)

        with patch("urllib.request.urlopen", side_effect=transport):
            # Warm the identity check so the timeline is only the calls
            # under test.
            first_client.call("core_enrol_get_users_courses")
            second_client.call("core_enrol_get_users_courses")
            with events_lock:
                events.clear()

            def _first():
                first_client.call("core_enrol_get_users_courses")

            def _second():
                first_on_the_wire.wait(timeout=10)
                second_is_queued.set()
                second_client.call("core_enrol_get_users_courses")

            threads = [
                threading.Thread(target=_first, name="first"),
                threading.Thread(target=_second, name="second"),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
            assert all(not thread.is_alive() for thread in threads)

        kinds = [kind for kind, _ in events]
        assert kinds[0] == "wire"          # the refused request
        assert kinds[1] == "cooldown"      # installed before the slot is freed
        # Nothing else reached the wire in between.
        assert ("wire", "second") not in events[:2]
        # And the second worker really did wait the server's 30s out.
        assert 30.0 in clock.sleeps


class TestOtherStatusesAreUntouched:
    def test_403_is_not_retried(self, client, coordinator):
        transport = _Transport(script=[_http_error(403), _Response(b"[]")])
        with patch("urllib.request.urlopen", side_effect=transport):
            with pytest.raises(urllib.error.HTTPError):
                client.call("core_enrol_get_users_courses")
        assert transport.calls.count("core_enrol_get_users_courses") == 1


# ── The limiter covers downloads too ───────────────────────────────


class TestDownloadsAreLimited:
    def _url(self):
        return f"{SITE}/pluginfile.php/1/mod_resource/content/notes.pdf"

    def test_download_takes_an_in_flight_slot(self, client, coordinator, clock):
        seen = []
        original = coordinator.acquire

        def _record():
            start = original()
            seen.append(start)
            return start

        coordinator.acquire = _record  # type: ignore[method-assign]
        with patch("urllib.request.urlopen",
                   return_value=_Response(b"PDF", {"Content-Length": "3"})):
            assert client.download_file(self._url()) == b"PDF"
        # A bulk download is exactly the traffic a site notices, so it is
        # paced with everything else rather than beside the limiter.
        assert len(seen) == 1

    def test_download_is_paced_by_the_min_gap(self, client, clock):
        paced = ratelimit.OriginCoordinator(
            ratelimit.origin_of(SITE), min_gap=0.25,
            sleep_fn=clock.sleep, monotonic=clock.monotonic,
            rng=lambda: 1.0, load_state=False,
        )
        ratelimit.for_testing_register(paced)
        with patch("urllib.request.urlopen",
                   return_value=_Response(b"PDF", {"Content-Length": "3"})):
            client.download_file(self._url())
            client.download_file(self._url())
        assert clock.sleeps == [0.25]

    def test_download_honours_retry_after(self, client, coordinator, clock):
        transport = _Transport(script=[
            _http_error(429, "20"),
            _Response(b"PDF", {"Content-Length": "3"}),
        ])
        with patch("urllib.request.urlopen", side_effect=transport):
            assert client.download_file(self._url()) == b"PDF"
        assert clock.sleeps == [20.0]

    def test_exhausted_download_is_a_structured_download_error(
        self, client, coordinator,
    ):
        transport = _Transport(script=[_http_error(429, "1")] * 5)
        with patch("urllib.request.urlopen", side_effect=transport):
            with pytest.raises(DownloadError) as exc:
                client.download_file(self._url())
        # Callers of download_file branch on DownloadError.code, so a
        # rate limit has to arrive in that vocabulary.
        assert exc.value.code == "rate_limited"
        assert "notes.pdf" in str(exc.value)
        assert "fake-token" not in str(exc.value)

    def test_oversize_download_is_not_retried(self, client, coordinator):
        big = _Response(b"x" * 50, {"Content-Length": "999999999999"})
        with patch("urllib.request.urlopen", return_value=big):
            with pytest.raises(DownloadError) as exc:
                client.download_file(self._url())
        assert exc.value.code == "oversize"


# ── Bounded, typed responses ───────────────────────────────────────


class TestBoundedResponses:
    def test_normal_json_still_works(self, client, coordinator):
        transport = _Transport(payload=json.dumps({"ok": 1}).encode())
        with patch("urllib.request.urlopen", side_effect=transport):
            assert client.call("core_enrol_get_users_courses") == {"ok": 1}

    def test_missing_content_type_is_allowed(self, client, coordinator):
        transport = _Transport(script=[_Response(b'{"ok": 2}', {})])
        with patch("urllib.request.urlopen", side_effect=transport):
            assert client.call("core_enrol_get_users_courses") == {"ok": 2}

    def test_text_plain_is_allowed(self, client, coordinator):
        transport = _Transport(script=[_Response(
            b"[]", {"Content-Type": "text/plain; charset=utf-8"},
        )])
        with patch("urllib.request.urlopen", side_effect=transport):
            assert client.call("core_enrol_get_users_courses") == []

    def test_html_login_page_is_a_typed_failure(self, client, coordinator):
        page = b"<!doctype html><title>Sign in</title>"
        with patch("urllib.request.urlopen", return_value=_Response(
            page, {"Content-Type": "text/html; charset=utf-8"},
        )):
            with pytest.raises(MoodleResponseError) as exc:
                client.call("core_enrol_get_users_courses")
        message = str(exc.value)
        assert "text/html" in message
        # The body is never quoted: it can contain anything.
        assert "Sign in" not in message
        assert "doctype" not in message

    def test_invalid_json_is_a_typed_failure_not_a_decode_error(
        self, client, coordinator,
    ):
        with patch("urllib.request.urlopen", return_value=_Response(
            b"not json at all", {"Content-Type": "application/json"},
        )):
            with pytest.raises(MoodleResponseError) as exc:
                client.call("core_enrol_get_users_courses")
        assert not isinstance(exc.value, json.JSONDecodeError)
        assert "not valid JSON" in str(exc.value)
        assert "not json at all" not in str(exc.value)

    def test_oversize_response_is_refused_by_size_only(
        self, client, coordinator,
    ):
        huge = b"[" + b"0," * (MAX_RESPONSE_BYTES // 2) + b"0]"
        assert len(huge) > MAX_RESPONSE_BYTES
        with patch("urllib.request.urlopen", return_value=_Response(huge)):
            with pytest.raises(MoodleResponseError) as exc:
                client.call("core_enrol_get_users_courses")
        assert str(MAX_RESPONSE_BYTES) in str(exc.value)
        assert "0,0," not in str(exc.value)

    def test_response_cap_is_far_above_any_metadata_payload(self):
        assert MAX_RESPONSE_BYTES == 16 * 1024 * 1024

    def test_read_is_bounded_even_when_content_length_lies(
        self, client, coordinator,
    ):
        asked = {}

        class _Liar(_Response):
            def read(self, size=None):
                asked["size"] = size
                return b"[]"

        transport = _Transport(script=[_Liar(b"[]")])
        with patch("urllib.request.urlopen", side_effect=transport):
            client.call("core_enrol_get_users_courses")
        # cap + 1, so an unbounded body can never be fully allocated.
        assert asked["size"] == MAX_RESPONSE_BYTES + 1


# ── Error family behaviour ─────────────────────────────────────────


class TestErrorFamily:
    def test_rate_limit_is_not_a_write_attempt(self):
        assert not issubclass(MoodleRateLimitedError, MoodleWriteAttemptError)
        assert issubclass(MoodleRateLimitedError, MoodleRequestError)
        assert issubclass(MoodleRateLimitedError, RuntimeError)

    def test_response_error_is_not_a_write_attempt(self):
        assert not issubclass(MoodleResponseError, MoodleWriteAttemptError)
        assert issubclass(MoodleResponseError, MoodleRequestError)

    def test_rate_limit_degrades_to_a_digest_warning(self):
        from worsaga.digest import get_digest

        class _Limited:
            base_url = SITE

            def get_courses(self):
                raise MoodleRateLimitedError("slow down", status=429)

            def __getattr__(self, name):
                def _fail(*args, **kwargs):
                    raise MoodleRateLimitedError("slow down", status=429)
                return _fail

        digest = get_digest(_Limited())
        # Degraded like any other fetch failure - never an abort.
        assert digest["deadlines"] == []
        assert any("slow down" in w for w in digest["warnings"])

    def test_rate_limit_degrades_to_a_sync_warning(self, tmp_path):
        from worsaga.sync import run_sync

        class _Limited:
            base_url = SITE
            is_demo = True  # keep lock/state files out of this test

            def get_courses(self):
                raise MoodleRateLimitedError("slow down", status=429)

        result = run_sync(_Limited(), cache_path=tmp_path / "cache.db")
        assert result["outcome"] == "failed"
        assert result["failure_class"] == "rate_limited"
        assert any("slow down" in w for w in result["warnings"])

    def test_connection_check_reports_rate_limited_separately(self):
        from worsaga.doctor import ConnectionCheckError, fetch_site_info

        class _Limited:
            def site_info(self):
                raise MoodleRateLimitedError("slow down", status=429)

        with pytest.raises(ConnectionCheckError) as exc:
            fetch_site_info(_Limited())
        # Not "your token is bad" and not "the site is down".
        assert exc.value.code == "rate_limited"
