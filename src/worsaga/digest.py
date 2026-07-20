"""Live digest aggregation."""

from __future__ import annotations

from typing import Any, Callable

from worsaga.assignments import get_assignments
from worsaga.client import MoodleClient, MoodleWriteAttemptError
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


def get_digest(client: MoodleClient, *, since_days: int = 1) -> dict[str, Any]:
    """Return a live digest; partial source failures become warnings."""
    warnings: list[str] = []
    deadlines = _safe_source(
        "deadlines",
        warnings,
        lambda: get_upcoming_deadlines(client, lookahead_days=max(1, since_days)),
        [],
    )
    assignments = _safe_source(
        "assignments",
        warnings,
        lambda: get_assignments(client),
        [],
    )
    updates = _safe_source(
        "updates",
        warnings,
        lambda: get_latest_updates(client, since_days=since_days),
        [],
    )
    notifications = _safe_source(
        "notifications",
        warnings,
        lambda: get_notifications(client),
        [],
    )
    messages = _safe_source(
        "messages",
        warnings,
        lambda: get_messages(client, since_days=since_days),
        [],
    )
    return {
        "since_days": since_days,
        "deadlines": deadlines,
        "assignments": assignments,
        "updates": updates,
        "notifications": notifications,
        "messages": messages,
        "warnings": warnings,
    }
