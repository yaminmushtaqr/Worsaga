"""Bounded concurrency and progress reporting for per-item fan-outs.

The all-course orchestrators (digest, sync, assignments, updates, grades)
each fan a read-only Moodle call out over many courses or forums. Run
sequentially against a real account (dozens of courses) these take minutes
with no feedback. :func:`run_parallel` runs the per-item work on a small
thread pool and returns the results in the original input order, so change
detection and displayed ordering stay deterministic regardless of which
item finished first.

Thread-safety of the shared client is what makes this safe:
:class:`worsaga.client.MoodleClient` holds only immutable config and issues
each request through a fresh ``urllib`` connection (there is no shared
``requests.Session`` connection pool), and
:class:`worsaga.demo.DemoMoodleClient` only deep-copies out of an immutable
dataset. A single client can therefore be shared across worker threads for
these read-only calls.

Progress is an optional callback (default silent). It is invoked once per
completed item, always from the single consumer thread that drains the
pool, so the counter is monotonic ``1..total`` with no locking. The CLI
wires it to a one-line stderr printer; the MCP server (which shares these
orchestrators over a stdio protocol whose stdout is the wire) passes
nothing and stays silent.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Sequence, TypeVar

T = TypeVar("T")
R = TypeVar("R")

#: Default cap on worker threads for a single fan-out. Deliberately small:
#: enough to hide per-request latency across dozens of courses without
#: hammering Moodle or exhausting connections. Override per process with the
#: ``WORSAGA_CONCURRENCY`` environment variable (itself capped at 8).
#:
#: This is **not** what paces Worsaga against a Moodle site — that is
#: :mod:`worsaga.ratelimit`, which holds the wire to two concurrent requests
#: per origin with a minimum gap between starts, however many workers are
#: running. The worker count only decides how much parsing and cache work
#: overlaps with those requests, which is why four is comfortable.
DEFAULT_MAX_WORKERS = 4

#: Hard ceiling on ``WORSAGA_CONCURRENCY``. The escape hatch exists for slow
#: sites, not for turning Worsaga into a load generator.
MAX_ALLOWED_WORKERS = 8

#: Progress callback signature: ``(completed, total, label)``. ``completed``
#: counts finished items (1..total); ``label`` describes the item that just
#: finished.
ProgressCallback = Callable[[int, int, str], None]


def resolve_max_workers(total: int) -> int:
    """Return the worker-thread count for a fan-out of *total* items.

    Never more than the item count and at least 1. ``WORSAGA_CONCURRENCY``
    overrides :data:`DEFAULT_MAX_WORKERS` (clamped to 1..8); an unset or
    unparseable value falls back to the default.
    """
    cap = DEFAULT_MAX_WORKERS
    raw = os.environ.get("WORSAGA_CONCURRENCY", "").strip()
    if raw:
        try:
            cap = max(1, min(MAX_ALLOWED_WORKERS, int(raw)))
        except ValueError:
            cap = DEFAULT_MAX_WORKERS
    return max(1, min(cap, total))


def run_parallel(
    items: Sequence[T],
    fn: Callable[[T], R],
    *,
    label_fn: Callable[[T], str],
    on_progress: ProgressCallback | None = None,
    max_workers: int | None = None,
) -> list[R]:
    """Map *fn* over *items* concurrently, returning results in input order.

    ``fn`` runs once per item on a bounded thread pool. Results are placed
    back at their original index regardless of completion order, so callers
    stay deterministic. When given, ``on_progress`` is invoked as
    ``(completed, total, label_fn(item))`` once per finished item from the
    calling thread only — the count is a monotonic ``1..total`` and needs no
    lock.

    An exception raised inside *fn* propagates (the first one observed) once
    the pool has shut down; :class:`worsaga.client.MoodleWriteAttemptError`
    therefore still reaches the caller unchanged. Callers that must tolerate
    per-item failure handle it inside *fn*, exactly as the sequential
    fan-outs already do.
    """
    items = list(items)
    total = len(items)
    if total == 0:
        return []

    workers = (
        resolve_max_workers(total)
        if max_workers is None
        else max(1, min(max_workers, total))
    )

    results: list[Any] = [None] * total

    if workers == 1:
        # Identical observable behaviour with no thread overhead — used for
        # single-item fan-outs and demo/test runs.
        for index, item in enumerate(items):
            results[index] = fn(item)
            if on_progress is not None:
                on_progress(index + 1, total, label_fn(item))
        return results

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_index = {
            executor.submit(fn, item): index
            for index, item in enumerate(items)
        }
        completed = 0
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            results[index] = future.result()
            completed += 1
            if on_progress is not None:
                on_progress(completed, total, label_fn(items[index]))
    return results


__all__ = [
    "DEFAULT_MAX_WORKERS",
    "MAX_ALLOWED_WORKERS",
    "ProgressCallback",
    "resolve_max_workers",
    "run_parallel",
]
