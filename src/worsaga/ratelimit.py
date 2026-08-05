"""Per-origin request pacing, in-flight limits, and server backpressure.

Every request Worsaga puts on the wire — web-service calls and file
downloads alike — passes through the coordinator for its Moodle origin.
One coordinator exists per origin per process (see
:func:`coordinator_for`), shared by every :class:`worsaga.client.MoodleClient`
instance and every worker thread, so the politeness ceiling is a property
of the *site* rather than of whichever object happened to make the call.

Two mechanisms, both applied at the network layer:

- **A minimum gap between request starts** (:data:`DEFAULT_MIN_GAP_SECONDS`,
  250 ms). Each caller reserves the next start slot under a lock and then
  releases the lock *before* sleeping toward it, so N waiting threads sleep
  in parallel toward N successive slots instead of convoying behind one
  another.
- **A two-in-flight semaphore** (:data:`DEFAULT_MAX_IN_FLIGHT`). The
  metadata fan-outs still run four worker threads, because most of their
  wall time is parsing and cache work; the wire simply never carries more
  than two concurrent requests to one site. Ecosystem guidance for
  well-behaved HTTP clients has been "about two connections per origin"
  since HTTP/1.1, and Moodle core applies no server-side rate limiting to
  web-service calls, so this ceiling is the only thing standing between a
  large enrolment and a self-inflicted load spike on someone else's server.

**Backpressure.** A ``429`` or ``503`` response sets a *cooldown* on the
origin (:meth:`OriginCoordinator.note_backpressure`): ``Retry-After`` is
honoured in both its delta-seconds and HTTP-date forms, floored at zero so
a skewed clock cannot produce a negative wait and capped at
:data:`MAX_RETRY_AFTER_SECONDS`; without the header the delay is
exponential backoff with full jitter. The cooldown is held by the
coordinator, so *every* request to that origin waits it out rather than
piling in behind the one that was refused, and it is written to a small
state file (:func:`backpressure_state_path`) so a ``watch`` process and a
CLI invocation running side by side back off together. Retries are bounded
twice over: at most :data:`MAX_ATTEMPTS` attempts for any one request, and
a shared per-origin budget of :data:`RETRY_BUDGET` retries per rolling
:data:`RETRY_BUDGET_WINDOW_SECONDS` window so four workers meeting the same
limit cannot retry in lockstep. There is no tight retry anywhere.

**Configuration is one-way.** Both knobs may only be moved in the polite
direction:

- ``WORSAGA_MIN_REQUEST_GAP_MS`` may *raise* the gap above 250 ms (up to
  :data:`MAX_MIN_GAP_SECONDS`, so a mistyped value cannot freeze the
  client); a smaller value is ignored.
- ``WORSAGA_MAX_IN_FLIGHT`` may *lower* the concurrent-request ceiling to
  1; a larger value is ignored.

Unparseable values fall back to the defaults. There is deliberately no way
to switch the limiter off: a site administrator's server is not the place
to discover that an escape hatch was left open.

``WORSAGA_STATE_DIR`` relocates the backpressure state file (used by
tests). Demo mode never reaches this module at all —
:class:`worsaga.demo.DemoMoodleClient` makes no requests.
"""

from __future__ import annotations

import contextlib
import email.utils
import json
import logging
import math
import os
import random
import threading
import time
import urllib.parse
from collections import deque
from pathlib import Path
from typing import Any, Callable

from worsaga.config import default_state_dir
from worsaga.secureio import write_private_file

logger = logging.getLogger(__name__)

#: Minimum wall time between the *starts* of two requests to one origin.
DEFAULT_MIN_GAP_SECONDS = 0.25

#: Ceiling on a raised gap. Purely a guard against a mistyped
#: ``WORSAGA_MIN_REQUEST_GAP_MS``; nobody needs a minute between requests.
MAX_MIN_GAP_SECONDS = 60.0

#: Concurrent requests allowed on the wire per origin.
DEFAULT_MAX_IN_FLIGHT = 2

#: HTTP statuses that mean "you are asking too often / I am overloaded".
BACKPRESSURE_STATUSES = frozenset({429, 503})

#: Attempts (not retries) for any single request before it gives up.
MAX_ATTEMPTS = 3

#: Longest ``Retry-After`` (or persisted cooldown) Worsaga will sit out.
#: A server asking for an hour gets two minutes and a clear failure —
#: an interactive command must not appear to hang indefinitely.
MAX_RETRY_AFTER_SECONDS = 120.0

#: Full-jitter exponential backoff used when ``Retry-After`` is absent.
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_FACTOR = 2.0
BACKOFF_CAP_SECONDS = 60.0

#: Retries shared by every request to one origin, per rolling window.
RETRY_BUDGET = 8
RETRY_BUDGET_WINDOW_SECONDS = 60.0

#: On-disk format version of the backpressure state file.
BACKPRESSURE_STATE_VERSION = 1

#: How often a coordinator will look at the backpressure file again while
#: it believes it is clear to go. Small enough that a long-lived ``watch``
#: notices another process's cooldown promptly, large enough that a
#: four-worker fan-out does not stat the file once per request.
STATE_REFRESH_SECONDS = 1.0

MIN_GAP_ENV = "WORSAGA_MIN_REQUEST_GAP_MS"
MAX_IN_FLIGHT_ENV = "WORSAGA_MAX_IN_FLIGHT"


def origin_of(url: str) -> str:
    """Return the canonical ``scheme://host[:port]`` origin of *url*.

    The path is dropped: a Moodle at ``/moodle`` and one at the site root
    on the same host are one server and share one coordinator. An explicit
    default port is normalised away so the same site never gets two
    coordinators.
    """
    parts = urllib.parse.urlsplit(str(url or "").strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    default_port = 443 if scheme == "https" else 80
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != default_port:
        netloc = f"{netloc}:{port}"
    return f"{scheme}://{netloc}"


def _ignore_setting(env_name: str, raw: str, reason: str) -> None:
    """Warn, once per read, that an env override was not usable.

    A politeness knob that silently does nothing is worse than no knob:
    someone who typed ``WORSAGA_MIN_REQUEST_GAP_MS=NaN`` believes they
    slowed Worsaga down. Say plainly that the default is in force.
    """
    logger.warning(
        "Ignoring %s=%r: %s. Using the default instead.",
        env_name, raw, reason,
    )


def resolve_min_gap_seconds() -> float:
    """Return the configured minimum request gap, in seconds.

    ``WORSAGA_MIN_REQUEST_GAP_MS`` may only raise it (and no higher than
    :data:`MAX_MIN_GAP_SECONDS`); anything smaller than the default, and
    anything unparseable, leaves the default in place.

    Non-finite values are refused explicitly. ``float("nan")`` parses
    happily and then makes every ``max()`` in the scheduler return its
    other operand, which would turn the gap — and the cooldown, which is
    compared the same way — into no limiter at all. That is exactly the
    off switch this module does not have.
    """
    raw = os.environ.get(MIN_GAP_ENV, "").strip()
    if not raw:
        return DEFAULT_MIN_GAP_SECONDS
    try:
        requested_ms = float(raw)
    except (ValueError, OverflowError):
        _ignore_setting(MIN_GAP_ENV, raw, "not a number")
        return DEFAULT_MIN_GAP_SECONDS
    if not math.isfinite(requested_ms) or requested_ms <= 0:
        _ignore_setting(
            MIN_GAP_ENV, raw,
            "not a positive, finite number of milliseconds",
        )
        return DEFAULT_MIN_GAP_SECONDS
    requested = requested_ms / 1000.0
    if requested <= DEFAULT_MIN_GAP_SECONDS:
        return DEFAULT_MIN_GAP_SECONDS
    return min(requested, MAX_MIN_GAP_SECONDS)


def resolve_max_in_flight() -> int:
    """Return the configured per-origin in-flight ceiling.

    ``WORSAGA_MAX_IN_FLIGHT`` may only lower it (never below 1); anything
    larger than the default, and anything unparseable or non-positive,
    leaves the default in place. ``0`` is refused rather than quietly read
    as ``1``: it means nothing, and a user who wrote it meant something
    else.
    """
    raw = os.environ.get(MAX_IN_FLIGHT_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_IN_FLIGHT
    try:
        requested = int(raw)
    except (ValueError, OverflowError):
        _ignore_setting(MAX_IN_FLIGHT_ENV, raw, "not a whole number")
        return DEFAULT_MAX_IN_FLIGHT
    if requested < 1:
        _ignore_setting(
            MAX_IN_FLIGHT_ENV, raw, "not a positive number of requests",
        )
        return DEFAULT_MAX_IN_FLIGHT
    return min(requested, DEFAULT_MAX_IN_FLIGHT)


#: Largest delta-seconds worth converting to a float at all (about 31
#: years). ``Retry-After`` is capped at :data:`MAX_RETRY_AFTER_SECONDS`
#: anyway, so clamping the *integer* first costs nothing and keeps a
#: 400-digit header from raising ``OverflowError`` on the way to the cap.
_MAX_SANE_DELTA_SECONDS = 10 ** 9


def parse_retry_after(value: Any, *, now_ts: float) -> float | None:
    """Return the ``Retry-After`` wait in seconds, or ``None`` if unusable.

    Both RFC 9110 forms are accepted: delta-seconds (``Retry-After: 30``)
    and an HTTP-date (``Retry-After: Wed, 21 Oct 2026 07:28:00 GMT``). The
    result is floored at zero, so a server whose clock runs behind the
    client's — or a date already in the past — yields "no wait" rather
    than a negative one. Callers apply :data:`MAX_RETRY_AFTER_SECONDS`.

    Nothing a server can put in this header may raise: an absurd value is
    clamped, an unparseable one returns ``None``, and the caller falls
    back to its own jittered backoff.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        seconds = int(text)
    except ValueError:
        pass
    else:
        # Clamped as an int, before any float conversion can overflow.
        return float(max(0, min(seconds, _MAX_SANE_DELTA_SECONDS)))
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if when is None:
        return None
    try:
        delta = when.timestamp() - now_ts
    except (OSError, OverflowError, ValueError):
        return None
    if not math.isfinite(delta):
        return None
    return max(0.0, min(delta, float(_MAX_SANE_DELTA_SECONDS)))


def backoff_delay(attempt: int, *, rng: Callable[[], float]) -> float:
    """Return a full-jitter exponential backoff delay for *attempt* (1-based).

    Full jitter — a uniform draw from ``[0, capped)`` rather than the
    capped value itself — is what stops several workers that met the same
    limit from retrying in step with each other.
    """
    exponent = max(0, int(attempt) - 1)
    capped = min(
        BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (BACKOFF_FACTOR ** exponent)
    )
    return capped * max(0.0, min(1.0, rng()))


def backpressure_state_path() -> Path:
    """Path of the cross-process backpressure record."""
    return default_state_dir() / "backpressure.json"


def _read_state(path: Path) -> dict[str, Any]:
    """Return the persisted backpressure state, or an empty one.

    Missing, unreadable, or malformed state is simply absent state: the
    file is an optimisation for cooperating processes, never a source of
    truth worth failing a command over.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(state, dict):
        return {}
    origins = state.get("origins")
    if not isinstance(origins, dict):
        return {}
    return {"origins": origins}


class OriginCoordinator:
    """Paces and bounds every request Worsaga makes to one origin.

    Instances are normally obtained from :func:`coordinator_for`, which
    keeps one per origin for the life of the process. Every timing
    dependency is injectable so tests run instantly and deterministically.
    """

    def __init__(
        self,
        origin: str,
        *,
        min_gap: float | None = None,
        max_in_flight: int | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
        rng: Callable[[], float] | None = None,
        now_fn: Callable[[], float] | None = None,
        state_path: Path | None = None,
        load_state: bool = True,
    ):
        self.origin = origin
        self.min_gap = (
            resolve_min_gap_seconds() if min_gap is None else max(0.0, min_gap)
        )
        self.max_in_flight = (
            resolve_max_in_flight() if max_in_flight is None
            else max(1, int(max_in_flight))
        )
        self._sleep = sleep_fn or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._rng = rng or random.random
        self._now = now_fn or time.time
        self._state_path = state_path
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(self.max_in_flight)
        self._next_start = 0.0
        self._cooldown_until = 0.0
        self._retries: deque[float] = deque()
        # Re-read bookkeeping: never before this monotonic instant, and
        # never when the file has not changed since the last look.
        self._next_state_check = 0.0
        self._state_stamp: tuple[int, int] | None = None
        self._reads_state = bool(load_state)
        if load_state:
            self._adopt_persisted_cooldown()

    # ── Pacing ─────────────────────────────────────────────────────

    def cooldown_remaining(self) -> float:
        """Seconds this origin is still under a server-requested cooldown."""
        with self._lock:
            return max(0.0, self._cooldown_until - self._monotonic())

    def _reserve(self) -> tuple[float, float]:
        """Claim the next start slot; return ``(start, wait)``.

        The lock is held only for the arithmetic. The caller sleeps toward
        its reserved start *outside* the lock, so two waiting threads sleep
        concurrently toward two successive slots instead of convoying.
        """
        with self._lock:
            now = self._monotonic()
            start = max(now, self._next_start, self._cooldown_until)
            self._next_start = start + self.min_gap
            return start, start - now

    def acquire(self) -> float:
        """Wait for permission to issue one request; return its start time.

        Order matters: the in-flight slot is taken first, so a thread that
        cannot go on the wire yet is not holding a gap reservation while it
        waits. Any cooldown another process recorded is picked up, the
        active cooldown is then sat out in full — that is what stops a
        fan-out from piling in behind the one request the server already
        refused — and only then is the pacing slot claimed.
        """
        self._slots.acquire()
        try:
            if self.cooldown_remaining() <= 0:
                # Only when this process believes it is clear to go: a
                # coordinator already cooling down has nothing to learn,
                # and this is the one place that touches the filesystem
                # on the request path.
                self._maybe_refresh_cooldown()
            remaining = self.cooldown_remaining()
            if remaining > 0:
                self._sleep(remaining)
            start, wait = self._reserve()
            if wait > 0:
                self._sleep(wait)
            return start
        except BaseException:
            self._slots.release()
            raise

    def release(self) -> None:
        """Give back the in-flight slot taken by :meth:`acquire`."""
        self._slots.release()

    @contextlib.contextmanager
    def request_slot(self):
        """Context manager wrapping :meth:`acquire` / :meth:`release`."""
        start = self.acquire()
        try:
            yield start
        finally:
            self.release()

    # ── Backpressure ───────────────────────────────────────────────

    def note_backpressure(
        self,
        *,
        retry_after: Any = None,
        attempt: int = 1,
    ) -> tuple[float, str]:
        """Record a 429/503 and return ``(delay_seconds, source)``.

        ``source`` is ``"retry-after"`` when the server named a wait and
        ``"backoff"`` when Worsaga chose one. The delay becomes a cooldown
        on the whole origin — not just on the refused request — and is
        persisted for other Worsaga processes. The cooldown only ever moves
        later, so a short backoff cannot shorten a long ``Retry-After``
        another response already set.
        """
        now_ts = self._now()
        delay: float | None = None
        source = "backoff"
        if retry_after is not None:
            parsed = parse_retry_after(retry_after, now_ts=now_ts)
            if parsed is not None:
                delay = min(parsed, MAX_RETRY_AFTER_SECONDS)
                source = "retry-after"
        if delay is None:
            delay = backoff_delay(attempt, rng=self._rng)

        with self._lock:
            until = self._monotonic() + delay
            if until > self._cooldown_until:
                self._cooldown_until = until
        self._persist_cooldown(now_ts + delay, source)
        return delay, source

    def take_retry_budget(self) -> bool:
        """Consume one shared retry for this origin; False when exhausted.

        The budget is what keeps a fan-out from turning one server limit
        into ``workers x MAX_ATTEMPTS`` extra requests: once
        :data:`RETRY_BUDGET` retries have been spent inside the rolling
        window, further requests fail fast instead of retrying.
        """
        with self._lock:
            now = self._monotonic()
            cutoff = now - RETRY_BUDGET_WINDOW_SECONDS
            while self._retries and self._retries[0] <= cutoff:
                self._retries.popleft()
            if len(self._retries) >= RETRY_BUDGET:
                return False
            self._retries.append(now)
            return True

    # ── Cross-process cooldown ─────────────────────────────────────

    def _path(self) -> Path:
        return self._state_path or backpressure_state_path()

    def _state_fingerprint(self) -> tuple[int, int] | None:
        """Return ``(mtime_ns, size)`` for the state file, or None if absent."""
        try:
            info = self._path().stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def _maybe_refresh_cooldown(self) -> None:
        """Pick up a cooldown another process recorded since the last look.

        A ``watch`` loop lives for days; without this it would read the
        backpressure file once at startup and never learn that a ``worsaga
        sync`` run in another terminal was just told to back off.

        Two guards keep this off the hot path. It runs at most once per
        :data:`STATE_REFRESH_SECONDS` of monotonic time, so a fan-out
        cannot stat the file per request; and when it does run it compares
        ``(mtime_ns, size)`` first, so an unchanged file costs one ``stat``
        and no parse. Both are deliberately cheap rather than exact: a
        cooldown noticed a second late is a second of ordinary pacing, and
        the coordinator that *set* it is correct immediately.
        """
        if not self._reads_state:
            return
        with self._lock:
            now = self._monotonic()
            if now < self._next_state_check:
                return
            self._next_state_check = now + STATE_REFRESH_SECONDS
        stamp = self._state_fingerprint()
        if stamp is None or stamp == self._state_stamp:
            self._state_stamp = stamp
            return
        self._adopt_persisted_cooldown()

    def _adopt_persisted_cooldown(self) -> None:
        """Honour a still-future cooldown written by another process."""
        self._state_stamp = self._state_fingerprint()
        entry = _read_state(self._path()).get("origins", {}).get(self.origin)
        if not isinstance(entry, dict):
            return
        until = _entry_until(entry)
        remaining = until - self._now()
        if remaining <= 0:
            return
        capped = min(remaining, MAX_RETRY_AFTER_SECONDS)
        with self._lock:
            self._cooldown_until = max(
                self._cooldown_until, self._monotonic() + capped
            )
        logger.info(
            "Honouring a %.1fs cooldown for %s recorded by another Worsaga "
            "process (%s).",
            capped, self.origin, entry.get("source", "unknown"),
        )

    def _persist_cooldown(self, until_ts: float, source: str) -> None:
        """Write this origin's cooldown for other processes to find.

        The stored deadline only ever moves later. Without that, a second
        response carrying a short ``Retry-After`` — or a jittered backoff
        — would overwrite a long wait another process is already serving,
        and every reader would come back early. The comparison happens
        inside the same read-modify-write as the prune, so it is against
        the bytes actually on disk.

        Best effort in every direction: expired entries are dropped on the
        way through, and any failure to read or write is logged at debug
        level and otherwise ignored. Two processes writing at the same
        instant can still lose one update — the file is a courtesy, not a
        lease.
        """
        path = self._path()
        try:
            origins = dict(_read_state(path).get("origins", {}))
            now_ts = self._now()
            stored = origins.get(self.origin)
            if isinstance(stored, dict):
                stored_until = _entry_until(stored)
                if stored_until > until_ts and stored_until > now_ts:
                    until_ts = stored_until
                    source = str(stored.get("source") or source)
            origins = {
                key: value
                for key, value in origins.items()
                if isinstance(value, dict)
                and _entry_until(value) > now_ts
                and key != self.origin
            }
            origins[self.origin] = {
                "until": round(float(until_ts), 3),
                "source": source,
                "recorded_at": round(float(now_ts), 3),
            }
            write_private_file(
                path,
                json.dumps(
                    {
                        "version": BACKPRESSURE_STATE_VERSION,
                        "origins": origins,
                    },
                    indent=2,
                    sort_keys=True,
                ) + "\n",
            )
        except (OSError, ValueError, TypeError) as exc:
            logger.debug("could not record backpressure state: %s", exc)
        finally:
            # Our own write must not look like somebody else's change on
            # the next refresh.
            self._state_stamp = self._state_fingerprint()


def _entry_until(entry: dict[str, Any]) -> float:
    try:
        return float(entry.get("until", 0))
    except (TypeError, ValueError):
        return 0.0


# ── Process-wide registry ──────────────────────────────────────────

_registry: dict[str, OriginCoordinator] = {}
_registry_lock = threading.Lock()
_overrides: dict[str, Any] = {}


def coordinator_for(url: str) -> OriginCoordinator:
    """Return the process-wide coordinator for *url*'s origin.

    Every client instance and every worker thread that talks to one site
    shares one coordinator, which is the whole point: politeness has to be
    a property of the origin, not of the object that happens to hold the
    connection.
    """
    origin = origin_of(url)
    with _registry_lock:
        coordinator = _registry.get(origin)
        if coordinator is None:
            coordinator = OriginCoordinator(origin, **_overrides)
            _registry[origin] = coordinator
        return coordinator


def for_testing_reset(**overrides: Any) -> None:
    """Drop every cached coordinator and set constructor overrides.

    Module-level state and a test suite are a bad combination: without a
    reset between tests, one test's cooldown or spent retry budget leaks
    into the next. Keyword arguments (``sleep_fn``, ``monotonic``, ``rng``,
    ``now_fn``, ``min_gap``, ...) are applied to every coordinator created
    afterwards, which is how the suite keeps real pacing sleeps out of
    otherwise-instant tests.
    """
    with _registry_lock:
        _registry.clear()
        _overrides.clear()
        _overrides.update(overrides)


def for_testing_register(coordinator: OriginCoordinator) -> None:
    """Install *coordinator* as the one for its origin."""
    with _registry_lock:
        _registry[coordinator.origin] = coordinator


__all__ = [
    "BACKOFF_CAP_SECONDS",
    "BACKPRESSURE_STATUSES",
    "DEFAULT_MAX_IN_FLIGHT",
    "DEFAULT_MIN_GAP_SECONDS",
    "MAX_ATTEMPTS",
    "MAX_RETRY_AFTER_SECONDS",
    "RETRY_BUDGET",
    "OriginCoordinator",
    "backoff_delay",
    "backpressure_state_path",
    "coordinator_for",
    "for_testing_register",
    "for_testing_reset",
    "origin_of",
    "parse_retry_after",
    "resolve_max_in_flight",
    "resolve_min_gap_seconds",
]
