"""Live digest aggregation."""

from __future__ import annotations

from typing import Any, Callable

from worsaga.assignments import get_assignments
from worsaga.client import MoodleClient, MoodleWriteAttemptError
from worsaga.concurrency import ProgressCallback
from worsaga.deadlines import get_upcoming_deadlines
from worsaga.forums import get_latest_updates
from worsaga.messages import get_messages, get_notifications


def _safe_source(
    name: str,
    warnings: list[str],
    fn: Callable[[], Any],
    default: Any,
) -> Any:
    try:
        return fn()
    except MoodleWriteAttemptError:
        raise
    except Exception as exc:
        warnings.append(f"{name}: {exc}")
        return default


def get_digest(
    client: MoodleClient,
    *,
    since_days: int = 1,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Return a live digest; partial source failures become warnings.

    The five sources run in turn (each with its own internal per-course /
    per-forum concurrency, which is where the time goes). ``on_progress``
    (default silent) reports one completed source at a time so a caller can
    show liveness instead of a multi-second hang.
    """
    warnings: list[str] = []
    sources: list[tuple[str, Callable[[], Any]]] = [
        ("deadlines",
         lambda: get_upcoming_deadlines(client, lookahead_days=max(1, since_days))),
        ("assignments", lambda: get_assignments(client)),
        ("updates", lambda: get_latest_updates(client, since_days=since_days)),
        ("notifications", lambda: get_notifications(client)),
        ("messages", lambda: get_messages(client, since_days=since_days)),
    ]
    collected: dict[str, Any] = {}
    total = len(sources)
    for index, (name, fn) in enumerate(sources, start=1):
        collected[name] = _safe_source(name, warnings, fn, [])
        if on_progress is not None:
            on_progress(index, total, name)
    return {
        "since_days": since_days,
        "deadlines": collected["deadlines"],
        "assignments": collected["assignments"],
        "updates": collected["updates"],
        "notifications": collected["notifications"],
        "messages": collected["messages"],
        "warnings": warnings,
    }
