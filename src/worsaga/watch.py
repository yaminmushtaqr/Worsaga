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

Notification content is course metadata only (change kinds and titles)
— never tokens, URLs, or file contents.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from worsaga.client import MoodleWriteAttemptError
from worsaga.concurrency import ProgressCallback
from worsaga.notify import send_notification
from worsaga.sync import SYNC_LOOKAHEAD_DAYS, run_sync

if TYPE_CHECKING:
    from worsaga.client import MoodleClient

DEFAULT_WATCH_INTERVAL = 900  # 15 minutes
MIN_WATCH_INTERVAL = 60

#: Change titles listed in a notification body before "and N more".
_NOTIFY_MAX_LINES = 3


def notification_text(changes: list[dict[str, Any]]) -> tuple[str, str]:
    """Return ``(title, body)`` describing *changes* for a notification."""
    count = len(changes)
    title = f"Worsaga: {count} change{'s' if count != 1 else ''}"
    lines = []
    for change in changes[:_NOTIFY_MAX_LINES]:
        kind = str(change.get("kind", "change")).replace("_", " ")
        label = str(change.get("title", "")).strip() or "(untitled)"
        course = str(change.get("course_shortname", "")).strip()
        prefix = f"{course}: " if course else ""
        lines.append(f"{prefix}{kind} - {label}")
    if count > _NOTIFY_MAX_LINES:
        lines.append(f"...and {count - _NOTIFY_MAX_LINES} more")
    return title, "\n".join(lines)


def run_watch(
    client: "MoodleClient",
    *,
    interval_seconds: int = DEFAULT_WATCH_INTERVAL,
    max_cycles: int | None = None,
    notify: bool = True,
    lookahead_days: int = SYNC_LOOKAHEAD_DAYS,
    cache_path: str | Path | None = None,
    on_cycle: Callable[[dict[str, Any]], None] | None = None,
    on_cycle_start: Callable[[int], None] | None = None,
    on_progress: ProgressCallback | None = None,
    notify_fn: Callable[[str, str], dict] = send_notification,
    sleep_fn: Callable[[float], None] | None = None,
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
    on_cycle : callable, optional
        Invoked with each cycle's result dict as it completes — the
        sync result plus ``cycle`` (1-based), ``ok``, and
        ``notification`` (the send result, when one was attempted).
    on_cycle_start : callable, optional
        Invoked with the 1-based cycle number just before that cycle's
        sync begins, so a caller can announce the cycle (a long sync
        should never look hung before its first output).
    on_progress : callable, optional
        Forwarded to :func:`worsaga.sync.run_sync` for per-course progress
        during each cycle's fetch phase.
    notify_fn, sleep_fn : callables
        Injection points for tests; defaults are the real ones.

    Returns
    -------
    dict
        ``cycles``, ``changes_total``, ``failures``, ``interval_seconds``.
    """
    interval_seconds = max(MIN_WATCH_INTERVAL, int(interval_seconds))
    # Resolved at call time so tests can patch time.sleep.
    if sleep_fn is None:
        sleep_fn = time.sleep
    cycles = failures = changes_total = 0

    if max_cycles is not None and max_cycles <= 0:
        return {
            "cycles": 0,
            "changes_total": 0,
            "failures": 0,
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
                on_progress=on_progress,
            )
            result["ok"] = True
        except MoodleWriteAttemptError:
            raise
        except Exception as exc:
            failures += 1
            result = {
                "ok": False,
                "error": str(exc),
                "changes": [],
                # Failed cycles still carry a timestamp for display.
                "synced_at": int(time.time()),
            }
        result["cycle"] = cycles

        changes = result.get("changes", [])
        changes_total += len(changes)
        if notify and changes:
            title, body = notification_text(changes)
            result["notification"] = notify_fn(title, body)

        if on_cycle is not None:
            on_cycle(result)

        if max_cycles is not None and cycles >= max_cycles:
            break
        sleep_fn(interval_seconds)

    return {
        "cycles": cycles,
        "changes_total": changes_total,
        "failures": failures,
        "interval_seconds": interval_seconds,
    }


__all__ = [
    "DEFAULT_WATCH_INTERVAL",
    "MIN_WATCH_INTERVAL",
    "notification_text",
    "run_watch",
]
