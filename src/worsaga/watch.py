"""Local watch mode: a foreground sync loop with change notifications.

``run_watch`` repeatedly runs the metadata sync (:func:`worsaga.sync.run_sync`)
on a fixed interval, reports each cycle through a callback, and raises a
local desktop notification (:mod:`worsaga.notify`) when changes are
detected. It is a *local* loop — it runs in the foreground of a
terminal session and stops with Ctrl+C. For unattended background
syncs, use ``worsaga auto-sync`` instead, which registers the platform
scheduler.

Loop robustness: a cycle whose sync raises (network down, Moodle
unreachable) is reported as a failed cycle and the loop continues —
except for :class:`~worsaga.client.MoodleWriteAttemptError`, which is a
safety invariant and always propagates. Timing is injectable
(``sleep_fn``/``max_cycles``) so tests never sleep for real.

A cycle is judged by the sync's own ``outcome``, not by whether it raised.
A run that reached Moodle and could not fetch a single category — or one
refused before it started because the cache belongs to another account —
returns normally today, and used to be reported as a successful cycle with
no changes. It is a failed cycle. ``skipped`` (another process held the
sync lock) is neither: nothing was attempted, so nothing is claimed.

Consecutive failed cycles **back off**: the interval doubles per failure,
capped at eight intervals or an hour, whichever is smaller, with +/-10%
jitter so several watchers that lost the same network do not return in
lockstep. Any cycle that is not a failure resets the loop to its base
interval. Cycles refused by the credential circuit breaker
(:mod:`worsaga.syncstate`) make no requests at all but still count as
failures, so a revoked token costs an ever-shrinking number of wake-ups
rather than a fixed drumbeat forever.

Every cycle is an unattended sync, so it inherits the unattended
collection default in :mod:`worsaga.sync`: deadlines, files, and grades,
but not forums. Pass ``categories`` (``worsaga watch --categories ...``,
or ``WORSAGA_SYNC_CATEGORIES``) to change that.

Notifications are deliberately uninformative by default. A desktop
notification is drawn by the operating system on top of whatever is
currently on screen — a shared display, a projector, a screen recording,
a lock screen — so the default body carries counts and course
short-codes only ("2 in ECON101, 1 in CS210") and never an item title.
``--notify-details`` / ``notify_details=True`` restores titles for
someone who has decided their screen is private.

What that does and does not promise, precisely:

- The Moodle token is stripped at the notification boundary itself
  (:func:`worsaga.notify.send_notification` runs both strings through
  :func:`worsaga.redact.redact_text`), so it cannot reach a toast however
  it got into the text.
- Grade values, instructor feedback, and file contents are never part of
  what Worsaga *composes* into a notification, in either mode. There is
  no flag that turns them on.
- Detailed mode carries item titles as their authors wrote them. A
  discussion title is free text typed by a person, so it can contain a
  URL, a mark, or anything else — which is exactly why counts-only is the
  default and titles are an explicit opt-in, not an oversight.
"""

from __future__ import annotations

import random
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from worsaga.client import MoodleWriteAttemptError
from worsaga.concurrency import ProgressCallback
from worsaga.notify import send_notification
from worsaga.sync import SYNC_LOOKAHEAD_DAYS, run_sync

if TYPE_CHECKING:
    from worsaga.client import MoodleClient

DEFAULT_WATCH_INTERVAL = 900  # 15 minutes
#: Politeness floor. Each cycle is a full metadata sync across every
#: enrolled course, so a tighter loop only multiplies load on a shared
#: Moodle instance without surfacing changes meaningfully sooner.
MIN_WATCH_INTERVAL = 300  # 5 minutes

#: Lines listed in a notification body before "and N more" — change
#: titles with ``details``, per-course counts without.
_NOTIFY_MAX_LINES = 3

#: Bucket for changes that arrived without a course short-code. Named the
#: same way an untitled change is, so the body never implies a course
#: that is not there.
_NOTIFY_NO_COURSE = "(no course)"

#: Backoff after consecutive failed cycles: the interval is multiplied by
#: ``2 ** failures`` and then capped. Whichever cap binds first wins, so a
#: 15-minute watch tops out at an hour and a 5-minute watch at 40 minutes.
BACKOFF_MULTIPLIER = 2
MAX_BACKOFF_INTERVALS = 8
MAX_BACKOFF_SECONDS = 3600

#: Proportional jitter applied to a backed-off wait (+/-10%).
BACKOFF_JITTER = 0.10


def notification_text(
    changes: list[dict[str, Any]], *, details: bool = False,
) -> tuple[str, str]:
    """Return ``(title, body)`` describing *changes* for a notification.

    The title is always a bare count. The body depends on *details*:

    - ``False`` (the default) — per-course counts and nothing else, e.g.
      ``"2 in ECON101, 1 in CS210"``. Enough to know whether the loop is
      worth switching to, and not enough for a passer-by to learn what
      assignment is due or what an instructor wrote.
    - ``True`` — the previous behaviour: up to
      :data:`_NOTIFY_MAX_LINES` change titles with their kind and course,
      then ``...and N more``.
    """
    count = len(changes)
    title = f"Worsaga: {count} change{'s' if count != 1 else ''}"
    if details:
        return title, _detailed_body(changes)
    return title, _counts_body(changes)


def _detailed_body(changes: list[dict[str, Any]]) -> str:
    """Return a notification body listing change titles (opt-in).

    Titles are Moodle-authored text reproduced as written — nobody here
    decides what is in a discussion title — which is what makes this an
    opt-in rather than the default.
    """
    lines = []
    for change in changes[:_NOTIFY_MAX_LINES]:
        kind = str(change.get("kind", "change")).replace("_", " ")
        label = str(change.get("title", "")).strip() or "(untitled)"
        course = str(change.get("course_shortname", "")).strip()
        prefix = f"{course}: " if course else ""
        lines.append(f"{prefix}{kind} - {label}")
    if len(changes) > _NOTIFY_MAX_LINES:
        lines.append(f"...and {len(changes) - _NOTIFY_MAX_LINES} more")
    return "\n".join(lines)


def _counts_body(changes: list[dict[str, Any]]) -> str:
    """Return a notification body of per-course counts (the default).

    Courses are ordered by how much changed, then by name, so the same
    set of changes always produces the same line. The
    :data:`_NOTIFY_NO_COURSE` bucket always sorts last whatever its count:
    it is a leftover rather than a course, and sorting it by name would
    let a parenthesis push it ahead of real course codes. Only the first
    :data:`_NOTIFY_MAX_LINES` entries are named; the rest are summed into
    a trailing count rather than growing a notification without limit.

    That trailing count says "in other courses" only when every bucket it
    covers really is a course. When the hidden tail includes the
    :data:`_NOTIFY_NO_COURSE` bucket — which it usually does, since that
    bucket sorts last — the label would be attributing changes to courses
    they were never associated with, so it degrades to a bare "N more".
    """
    counts: dict[str, int] = {}
    for change in changes:
        course = (
            str(change.get("course_shortname", "")).strip()
            or _NOTIFY_NO_COURSE
        )
        counts[course] = counts.get(course, 0) + 1

    ordered = sorted(
        counts.items(),
        key=lambda item: (item[0] == _NOTIFY_NO_COURSE, -item[1], item[0]),
    )
    shown = ordered[:_NOTIFY_MAX_LINES]
    hidden = ordered[_NOTIFY_MAX_LINES:]
    parts = [f"{total} in {course}" for course, total in shown]
    remaining = sum(total for _course, total in hidden)
    if remaining:
        hides_no_course = any(
            course == _NOTIFY_NO_COURSE for course, _total in hidden
        )
        parts.append(
            f"{remaining} more" if hides_no_course
            else f"{remaining} in other courses"
        )
    return ", ".join(parts)


def backoff_seconds(
    interval_seconds: int,
    consecutive_failures: int,
    *,
    rng: Callable[[], float] | None = None,
) -> int:
    """Return the wait before the next cycle after *consecutive_failures*.

    Zero failures returns the base interval unchanged — the ordinary case
    pays nothing for this. Otherwise the interval doubles per failure,
    stops at :data:`MAX_BACKOFF_INTERVALS` intervals or
    :data:`MAX_BACKOFF_SECONDS`, and is jittered by +/-
    :data:`BACKOFF_JITTER` so two watchers that lost the same network do
    not come back at the same instant.
    """
    interval = max(1, int(interval_seconds))
    if consecutive_failures <= 0:
        return interval
    ceiling = min(interval * MAX_BACKOFF_INTERVALS, MAX_BACKOFF_SECONDS)
    # Never below the base interval, even when the ceiling is (a
    # pathological interval longer than an hour).
    ceiling = max(interval, ceiling)
    raw = interval * (BACKOFF_MULTIPLIER ** min(consecutive_failures, 30))
    capped = min(raw, ceiling)
    draw = random.random() if rng is None else rng()
    factor = 1.0 + BACKOFF_JITTER * (2.0 * max(0.0, min(1.0, draw)) - 1.0)
    return max(interval, int(round(capped * factor)))


def run_watch(
    client: "MoodleClient",
    *,
    interval_seconds: int = DEFAULT_WATCH_INTERVAL,
    max_cycles: int | None = None,
    notify: bool = True,
    notify_details: bool = False,
    lookahead_days: int = SYNC_LOOKAHEAD_DAYS,
    cache_path: str | Path | None = None,
    categories: str | Sequence[str] | None = None,
    store_feedback: bool | None = None,
    on_cycle: Callable[[dict[str, Any]], None] | None = None,
    on_cycle_start: Callable[[int], None] | None = None,
    on_progress: ProgressCallback | None = None,
    notify_fn: Callable[[str, str], dict] = send_notification,
    sleep_fn: Callable[[float], None] | None = None,
    rng: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Run the sync loop and return an overall summary when it ends.

    Parameters
    ----------
    client : MoodleClient
        Authenticated client (or the demo client).
    interval_seconds : int
        Seconds between sync cycles; clamped to at least
        ``MIN_WATCH_INTERVAL`` so a typo can never hammer Moodle.
    max_cycles : int, optional
        Stop after this many cycles (None = run until interrupted).
    notify : bool
        Raise a desktop notification when a cycle detects changes.
    notify_details : bool
        Include change titles in the notification body. Off by default:
        a notification is drawn wherever the screen happens to be
        pointing, so it carries per-course counts unless asked
        otherwise. The token is stripped either way, and grade values,
        feedback, and file contents are never composed into a
        notification either way; a title, when it is included, is
        whatever its author wrote.
    categories : str | sequence of str, optional
        Which sync categories each cycle collects. ``None`` (default)
        takes the unattended default — no forums. Forwarded verbatim to
        :func:`worsaga.sync.run_sync`, which validates it.
    store_feedback : bool, optional
        Opt in to persisting full instructor feedback text. ``None``
        (default) leaves it to ``WORSAGA_SYNC_STORE_FEEDBACK``, which is
        off unless it was deliberately set.
    on_cycle : callable, optional
        Invoked with each cycle's result dict as it completes — the
        sync result plus ``cycle`` (1-based), ``ok``, ``outcome``,
        ``consecutive_failures``, ``next_cycle_in`` (the wait before the
        next cycle, absent on the last one), ``backoff`` (True when that
        wait is longer than the base interval), and ``notification``
        (the send result, when one was attempted).
    on_cycle_start : callable, optional
        Invoked with the 1-based cycle number just before that cycle's
        sync begins, so a caller can announce the cycle (a long sync
        should never look hung before its first output).
    on_progress : callable, optional
        Forwarded to :func:`worsaga.sync.run_sync` for per-course progress
        during each cycle's fetch phase.
    notify_fn, sleep_fn, rng : callables
        Injection points for tests; defaults are the real ones.

    Returns
    -------
    dict
        ``cycles``, ``changes_total``, ``failures``, ``skipped``,
        ``interval_seconds``, ``consecutive_failures``.
    """
    interval_seconds = max(MIN_WATCH_INTERVAL, int(interval_seconds))
    # Resolved at call time so tests can patch time.sleep.
    if sleep_fn is None:
        sleep_fn = time.sleep
    cycles = failures = changes_total = skipped = 0
    consecutive_failures = 0

    if max_cycles is not None and max_cycles <= 0:
        return {
            "cycles": 0,
            "changes_total": 0,
            "failures": 0,
            "skipped": 0,
            "consecutive_failures": 0,
            "interval_seconds": interval_seconds,
        }

    while True:
        cycles += 1
        if on_cycle_start is not None:
            on_cycle_start(cycles)
        result: dict[str, Any]
        try:
            result = run_sync(
                client, cache_path=cache_path, lookahead_days=lookahead_days,
                on_progress=on_progress, unattended=True,
                categories=categories, store_feedback=store_feedback,
            )
            # The sync's own verdict, not "it returned without raising":
            # a run that fetched nothing is a failed cycle even though it
            # completed cleanly.
            result.setdefault("outcome", "success")
        except MoodleWriteAttemptError:
            raise
        except Exception as exc:
            result = {
                "outcome": "failed",
                "error": str(exc),
                "changes": [],
                # Failed cycles still carry a timestamp for display.
                "synced_at": int(time.time()),
            }

        outcome = str(result.get("outcome") or "failed")
        result["ok"] = outcome != "failed"
        if outcome == "failed":
            failures += 1
            consecutive_failures += 1
        elif outcome == "skipped":
            # Another process was already syncing. Neither a success nor
            # a failure: it says nothing about whether this site is
            # reachable, so it neither counts against the loop nor lets a
            # real failure streak off the hook.
            skipped += 1
        else:
            consecutive_failures = 0
        result["cycle"] = cycles
        result["consecutive_failures"] = consecutive_failures

        changes = result.get("changes", [])
        changes_total += len(changes)
        if notify and changes:
            title, body = notification_text(changes, details=notify_details)
            result["notification"] = notify_fn(title, body)

        last_cycle = max_cycles is not None and cycles >= max_cycles
        wait = 0
        if not last_cycle:
            wait = backoff_seconds(
                interval_seconds, consecutive_failures, rng=rng,
            )
            result["next_cycle_in"] = wait
            result["backoff"] = wait > interval_seconds

        if on_cycle is not None:
            on_cycle(result)

        if last_cycle:
            break
        sleep_fn(wait)

    return {
        "cycles": cycles,
        "changes_total": changes_total,
        "failures": failures,
        "skipped": skipped,
        "consecutive_failures": consecutive_failures,
        "interval_seconds": interval_seconds,
    }


__all__ = [
    "DEFAULT_WATCH_INTERVAL",
    "MAX_BACKOFF_SECONDS",
    "MIN_WATCH_INTERVAL",
    "backoff_seconds",
    "notification_text",
    "run_watch",
]
