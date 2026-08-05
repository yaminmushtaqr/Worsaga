"""Per-site sync outcome history, and the credential circuit breaker.

A sync that fails is only interesting the first time. A sync that has
failed the same way for two days because the token was revoked is a
different thing entirely: nothing it does next will work, and every
unattended attempt is a pointless request against someone else's server —
often an authentication failure, which is exactly the kind a site's
security monitoring counts.

So each run records its outcome here:

- ``success`` / ``partial`` — the failure streak resets and any open
  circuit closes. **Any** successful sync closes it, which makes a manual
  ``worsaga sync`` the documented way back.
- ``failed`` — the streak grows and the failure is filed under one of four
  coarse classes (``auth``, ``network``, ``rate_limited``, ``other``).
- ``skipped`` — another process held the sync lock. Neither a success nor
  a failure: the counters are left exactly as they were.

An ``auth``-class failure opens the **circuit**: unattended runs (watch
cycles, the scheduled auto-sync) stop before touching the network and say
what to do instead. Foreground runs always attempt, because they are the
reset path — a user who has just fixed their token must be able to prove
it. Only ``auth`` opens the circuit; a network outage or a rate limit is
temporary and retrying it is the right behaviour.

The state lives in one small owner-only JSON file in
:func:`worsaga.config.default_state_dir` (``WORSAGA_STATE_DIR`` relocates
it). Losing or corrupting it costs a forgotten failure count and nothing
else, so every read failure is treated as "no history" and every field is
coerced on the way in — a hand-edited ``"consecutive_failures": "lots"``
must not crash an unattended run at three in the morning.

Updates are read-modify-write over the whole file, which two processes can
lose to each other: a run skipped because another sync held the lock
writes its outcome *outside* that lock, and could otherwise resurrect a
circuit the other run had just cleared. So a short-lived file lock (plus a
process-local mutex) serialises the mutation, and the read happens inside
it so the merge is against current bytes. Taking that lock is best effort
in the same way everything else here is: if it cannot be had quickly the
write proceeds anyway, because a contended state file must never turn a
successful sync into a failed command.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
import urllib.error
from pathlib import Path
from typing import Any

from worsaga.client import (
    DownloadError,
    MoodleRateLimitedError,
    MoodleRequestError,
    is_auth_error,
)
from worsaga.config import default_state_dir
from worsaga.secureio import write_private_file
from worsaga.synclock import SyncLock

logger = logging.getLogger(__name__)

#: On-disk format version.
SYNC_STATE_VERSION = 1

#: The complete outcome vocabulary. ``skipped`` is not a sync result so
#: much as the absence of one — another process was already syncing.
OUTCOMES = ("success", "partial", "failed", "skipped")

#: Coarse failure classes. Deliberately four: enough to decide whether to
#: keep trying, not so many that the decision needs a lookup table.
FAILURE_CLASSES = ("auth", "network", "rate_limited", "other")

#: The only class that stops unattended runs. Everything else is
#: temporary by nature and worth retrying on the next cycle.
CIRCUIT_CLASSES = frozenset({"auth"})

#: What to tell a user whose circuit is open.
CIRCUIT_REMEDY = (
    "circuit open: fix credentials then run 'worsaga sync' manually"
)


def classify_failure(exc: BaseException | None) -> str:
    """Return the coarse failure class for *exc*.

    ``rate_limited`` and ``auth`` are checked before the generic
    server-answer and transport cases, because both are subclasses of
    something broader and both change what the user should do next.
    """
    if exc is None:
        return "other"
    if isinstance(exc, MoodleRateLimitedError):
        return "rate_limited"
    if isinstance(exc, DownloadError):
        if exc.code == "rate_limited":
            return "rate_limited"
        if exc.code in ("auth", "network"):
            return exc.code
        return "other"
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return "auth"
        if exc.code in (429, 503):
            return "rate_limited"
        return "network"
    if isinstance(exc, MoodleRequestError):
        return "auth" if is_auth_error(exc) else "other"
    if isinstance(exc, urllib.error.URLError):
        return "network"
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "network"
    if is_auth_error(exc):
        return "auth"
    if isinstance(exc, OSError):
        return "network"
    return "other"


def worst_failure_class(classes: list[str]) -> str:
    """Return the class that should represent a run that failed several ways.

    Ordered by how much a user can do about it: a rejected token is worth
    surfacing over a flaky network, and both over an unclassified error.
    """
    for candidate in ("auth", "rate_limited", "network"):
        if candidate in classes:
            return candidate
    return "other"


#: How long a state-file lock may sit before it is assumed abandoned. The
#: critical section is a read, a dict update, and a write — milliseconds —
#: so anything older than this is a crashed process, not a slow one.
STATE_LOCK_TTL_SECONDS = 60

#: Attempts to take the state lock before giving up and writing anyway.
_LOCK_ATTEMPTS = 5
_LOCK_RETRY_SECONDS = 0.01

#: Serialises mutations inside this process. The file lock covers other
#: processes; this covers the fan-out threads and the watch loop, which
#: the file lock alone would not (a second attempt from the same process
#: would simply see its own lock file).
_process_lock = threading.Lock()


def sync_state_path() -> Path:
    """Path of the per-site sync outcome record."""
    return default_state_dir() / "syncstate.json"


def state_lock_path() -> Path:
    """Path of the short-lived lock guarding state mutations."""
    return sync_state_path().with_name("syncstate.lock")


def _read_all(path: Path | None = None) -> dict[str, Any]:
    """Return every site's recorded state; ``{}`` when there is none."""
    try:
        with open(path or sync_state_path(), encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(state, dict):
        return {}
    sites = state.get("sites")
    return sites if isinstance(sites, dict) else {}


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalise(entry: Any) -> dict[str, Any]:
    """Return *entry* with every field coerced to a usable shape.

    This file is plain JSON in a directory the user owns, so it will
    occasionally be hand-edited, truncated, or written by a future
    version. Nothing read from it may reach ``int()`` or a format string
    unchecked: an unattended watch loop that crashes on a corrupt counter
    is worse than one that forgets the counter.

    Unusable fields are dropped rather than guessed at, and a dropped
    ``failure_class`` closes the circuit — the recoverable direction,
    since the worst case is one sync attempt that did not need to happen.
    """
    if not isinstance(entry, dict):
        return {}
    clean: dict[str, Any] = {}

    outcome = entry.get("last_outcome")
    if isinstance(outcome, str) and outcome in OUTCOMES:
        clean["last_outcome"] = outcome

    failures = _as_int(entry.get("consecutive_failures"))
    if failures is not None:
        clean["consecutive_failures"] = max(0, failures)

    failure_class = entry.get("failure_class")
    if failure_class in FAILURE_CLASSES:
        clean["failure_class"] = failure_class
    elif "failure_class" in entry:
        clean["failure_class"] = None

    if "circuit_open" in entry:
        clean["circuit_open"] = bool(entry.get("circuit_open"))

    for field in ("last_run_at", "last_failure_at", "last_success_at"):
        stamp = _as_int(entry.get(field))
        if stamp is not None:
            clean[field] = stamp
    return clean


def read_site_state(site: str, *, path: Path | None = None) -> dict[str, Any]:
    """Return the recorded state for *site* (empty dict when unknown)."""
    return _normalise(_read_all(path).get(str(site)))


def circuit_state(site: str, *, path: Path | None = None) -> dict[str, Any] | None:
    """Return the site's state when its circuit is open, else ``None``."""
    entry = read_site_state(site, path=path)
    if entry.get("circuit_open") and entry.get("failure_class") in CIRCUIT_CLASSES:
        return entry
    return None


def circuit_message(entry: dict[str, Any]) -> str:
    """Return the one-line explanation for an open circuit."""
    failures = _as_int(entry.get("consecutive_failures")) or 0
    return (
        f"{CIRCUIT_REMEDY} ({failures} consecutive authentication failures; "
        "no request was made)"
    )


def record_outcome(
    site: str,
    outcome: str,
    *,
    failure_class: str | None = None,
    now: int | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Record one run's *outcome* for *site* and return the new state.

    The read and the write happen inside one critical section, so a run
    that was skipped (and therefore wrote *outside* the sync lock) can
    never merge its outcome onto a snapshot another process has already
    superseded — which is how a cleared circuit came back open.

    Writing is best effort: this is an aid to unattended runs, never a
    precondition for one, so a read-only or full disk must not turn a
    successful sync into a failed command. The returned dict is the state
    as computed, whether or not it reached disk.
    """
    timestamp = int(time.time()) if now is None else int(now)

    def _mutate(current: dict[str, Any]) -> dict[str, Any]:
        entry = dict(current)
        entry["last_outcome"] = outcome
        entry["last_run_at"] = timestamp
        if outcome in ("success", "partial"):
            entry["consecutive_failures"] = 0
            entry["failure_class"] = None
            entry["circuit_open"] = False
            entry["last_success_at"] = timestamp
        elif outcome == "failed":
            resolved = (
                failure_class if failure_class in FAILURE_CLASSES else "other"
            )
            entry["consecutive_failures"] = (
                (_as_int(entry.get("consecutive_failures")) or 0) + 1
            )
            entry["failure_class"] = resolved
            entry["circuit_open"] = resolved in CIRCUIT_CLASSES
            entry["last_failure_at"] = timestamp
        # "skipped" leaves the counters alone: another process was
        # syncing, which says nothing at all about whether this site is
        # reachable, and must not clear a real failure streak either.
        return entry

    return _update_site(site, _mutate, path=path)


def clear_site(site: str, *, path: Path | None = None) -> None:
    """Forget everything recorded for *site* (used by tests and resets)."""
    _update_site(site, lambda current: None, path=path)


@contextlib.contextmanager
def _serialised():
    """Hold the process mutex and, if it can be had quickly, the file lock.

    The file lock is advisory in the strictest sense: it makes concurrent
    writers take turns when it is available, and changes nothing when it
    is not. State this small is not worth failing a sync over.
    """
    with _process_lock:
        lock = SyncLock(
            "worsaga-syncstate",
            sync_state_path(),
            path=state_lock_path(),
            ttl_seconds=STATE_LOCK_TTL_SECONDS,
        )
        taken = False
        for attempt in range(_LOCK_ATTEMPTS):
            try:
                taken = lock.acquire()
            except OSError as exc:
                logger.debug("could not take the sync state lock: %s", exc)
                break
            if taken:
                break
            if attempt < _LOCK_ATTEMPTS - 1:
                time.sleep(_LOCK_RETRY_SECONDS)
        if not taken:
            logger.debug(
                "writing sync state without the file lock; another process "
                "is holding it",
            )
        try:
            yield
        finally:
            if taken:
                lock.release()


def _update_site(
    site: str,
    mutate: Any,
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    """Read, apply *mutate*, and write back, all inside the critical section."""
    destination = path or sync_state_path()
    key = str(site)
    with _serialised():
        # Read inside the lock: merging onto a snapshot taken before it
        # was held is exactly the lost update this exists to prevent.
        sites = dict(_read_all(destination))
        entry = mutate(_normalise(sites.get(key)))
        if entry is None:
            sites.pop(key, None)
        else:
            sites[key] = entry
        try:
            write_private_file(
                destination,
                json.dumps(
                    {"version": SYNC_STATE_VERSION, "sites": sites},
                    indent=2,
                    sort_keys=True,
                ) + "\n",
            )
        except (OSError, ValueError, TypeError) as exc:
            logger.debug("could not record sync state for %s: %s", site, exc)
    return entry or {}


__all__ = [
    "CIRCUIT_CLASSES",
    "CIRCUIT_REMEDY",
    "FAILURE_CLASSES",
    "OUTCOMES",
    "STATE_LOCK_TTL_SECONDS",
    "circuit_message",
    "circuit_state",
    "classify_failure",
    "clear_site",
    "read_site_state",
    "record_outcome",
    "state_lock_path",
    "sync_state_path",
    "worst_failure_class",
]
