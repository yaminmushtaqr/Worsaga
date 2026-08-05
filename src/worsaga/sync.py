"""Metadata sync and change detection over the local cache.

``run_sync`` fetches metadata-only snapshots (deadlines, file metadata,
grades, forum discussions — never file contents), diffs them against the
local SQLite cache, and records change events: new deadlines, new files,
grade updates, and forum updates.

Correctness rules:

- The first successful sync of a category for a site establishes a
  baseline and emits no change events. Baseline state is recorded
  explicitly per (site, category) — an empty category still finishes
  baselining.
- A failed category fetch is reported as a warning and the category is
  skipped (``synced: false``) — an errored fetch is never mistaken for
  an empty (or changed) Moodle. Deadline and forum fetches are strict
  for this reason (no partial degradation inside a snapshot).
- Grades tolerate per-course permission failures: the set of covered
  courses is recorded as the category's *scope*, and items from courses
  outside the previous scope are adopted silently instead of being
  reported as spurious changes.
- Items that disappear from Moodle are kept in the cache
  (``last_seen_at`` stops advancing) — removal events are out of scope.
- The diff/write phase runs inside a ``BEGIN IMMEDIATE`` transaction,
  so concurrent syncs serialize instead of recording duplicate events.

Shared by the CLI (``worsaga sync`` / ``worsaga changes``) and the MCP
server (``sync_now`` / ``get_changes``). Tokens and authenticated URLs
never reach the cache — see :mod:`worsaga.cache`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

from worsaga.cache import CacheStore, default_cache_path
from worsaga.client import MoodleClient, MoodleWriteAttemptError
from worsaga.concurrency import ProgressCallback, run_parallel
from worsaga.deadlines import get_upcoming_deadlines
from worsaga.forums import normalize_forum_discussions, normalize_forums
from worsaga.grades import collect_grades
from worsaga.materials import extract_materials, strip_file_urls
from worsaga.models import change_record

logger = logging.getLogger(__name__)

SYNC_LOOKAHEAD_DAYS = 60

SYNC_CATEGORIES = ("deadlines", "files", "grades", "forums")


def _fingerprint(payload: dict[str, Any], fields: tuple[str, ...]) -> str:
    """Return a stable hash over the change-relevant fields of *payload*."""
    data = {field: payload.get(field) for field in fields}
    text = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Category:
    """One synced category: how to key, fingerprint, and describe items."""

    def __init__(
        self,
        name: str,
        *,
        fingerprint_fields: tuple[str, ...],
        extra_diff_fields: tuple[str, ...] = (),
        new_kind: str,
        updated_kind: str,
        key_fn: Callable[[dict[str, Any]], str],
        title_fn: Callable[[dict[str, Any]], str],
        course_fn: Callable[[dict[str, Any]], tuple[int | None, str]],
        scope_fn: Callable[[dict[str, Any]], Any] | None = None,
        new_item_is_change: Callable[[dict[str, Any]], bool] = lambda payload: True,
    ):
        self.name = name
        self.fingerprint_fields = fingerprint_fields
        # Every fingerprinted field appears in before/after so no change
        # is opaque (e.g. a feedback-only grade change must be visible).
        self.diff_fields = fingerprint_fields + tuple(
            field for field in extra_diff_fields
            if field not in fingerprint_fields
        )
        self.new_kind = new_kind
        self.updated_kind = updated_kind
        self.key_fn = key_fn
        self.title_fn = title_fn
        self.course_fn = course_fn
        self.scope_fn = scope_fn
        self.new_item_is_change = new_item_is_change

    def fingerprint(self, payload: dict[str, Any]) -> str:
        return _fingerprint(payload, self.fingerprint_fields)

    def diff_view(self, payload: dict[str, Any] | None) -> dict[str, Any] | None:
        if payload is None:
            return None
        return {field: payload.get(field) for field in self.diff_fields}


_CATEGORIES = {
    "deadlines": _Category(
        "deadlines",
        fingerprint_fields=("name", "course", "type", "due_ts"),
        extra_diff_fields=("due_iso",),
        new_kind="new_deadline",
        updated_kind="deadline_changed",
        key_fn=lambda d: f"{d.get('type')}:{d.get('id') or d.get('name')}",
        title_fn=lambda d: str(d.get("name", "")),
        course_fn=lambda d: (None, str(d.get("course", ""))),
    ),
    "files": _Category(
        "files",
        fingerprint_fields=("file_name", "module_name", "file_size", "time_modified"),
        extra_diff_fields=("section_name",),
        new_kind="new_file",
        updated_kind="file_updated",
        key_fn=lambda m: f"{m.get('course_id')}:{m.get('dedupe_key')}",
        title_fn=lambda m: str(m.get("file_name") or m.get("module_name") or ""),
        course_fn=lambda m: (m.get("course_id"), str(m.get("course_shortname", ""))),
    ),
    "grades": _Category(
        "grades",
        fingerprint_fields=("grade_display", "percentage", "status", "feedback"),
        new_kind="grade_updated",
        updated_kind="grade_updated",
        key_fn=lambda g: f"{g.get('course_id')}:{g.get('item_id') or g.get('item_name')}",
        title_fn=lambda g: str(g.get("item_name", "")),
        course_fn=lambda g: (g.get("course_id"), str(g.get("course_shortname", ""))),
        scope_fn=lambda g: g.get("course_id"),
        # A brand-new gradebook item only counts as a "grade update" when
        # it actually carries a grade; empty placeholders are just cached.
        new_item_is_change=lambda g: bool(g.get("graded")),
    ),
    "forums": _Category(
        "forums",
        fingerprint_fields=("name", "modified_at"),
        extra_diff_fields=("author",),
        new_kind="new_forum_discussion",
        updated_kind="forum_discussion_updated",
        key_fn=lambda f: f"{f.get('forum_id')}:{f.get('discussion_id')}",
        title_fn=lambda f: str(f.get("name", "")),
        course_fn=lambda f: (f.get("course_id"), str(f.get("course_shortname", ""))),
    ),
}


def _safe_fetch(
    name: str,
    warnings: list[str],
    fn: Callable[[], Any],
) -> Any:
    """Run one category fetch; a failure becomes a warning, not a crash.

    Returns None on failure so the category is skipped entirely — an
    errored fetch must never look like an empty (or changed) Moodle.
    """
    try:
        return fn()
    except MoodleWriteAttemptError:
        raise
    except Exception as exc:
        logger.warning("sync fetch failed for %s: %s", name, exc)
        warnings.append(f"{name}: {exc}")
        return None


def _fetch_file_metadata(
    client: MoodleClient,
    *,
    on_progress: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    """Return token-free material metadata across all enrolled courses.

    The per-course ``get_course_contents`` fetch fans out concurrently and
    the results are reassembled in course order. As with the sequential
    version, a per-course failure propagates (this snapshot is strict — an
    errored fetch must never look like an empty Moodle); the caller's
    :func:`_safe_fetch` turns it into a skipped category.
    """
    courses = [c for c in client.get_courses() if c.get("id")]

    def _fetch_course(course: dict[str, Any]) -> list[dict[str, Any]]:
        course_id = course.get("id")
        sections = client.get_course_contents(course_id)
        materials = strip_file_urls(
            extract_materials(sections, course_id, base_url=client.base_url)
        )
        for material in materials:
            material["course_shortname"] = str(course.get("shortname", ""))
        return materials

    records: list[dict[str, Any]] = []
    for per_course in run_parallel(
        courses,
        _fetch_course,
        label_fn=lambda c: str(c.get("shortname") or c.get("id") or ""),
        on_progress=on_progress,
    ):
        records.extend(per_course)
    return records


def _fetch_forum_discussions(
    client: MoodleClient,
    *,
    on_progress: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    """Return all forum discussions across enrolled courses.

    Unlike :func:`worsaga.forums.get_latest_updates`, per-forum failures
    propagate: for change detection an errored fetch must never be
    mistaken for a Moodle with no forum activity. The per-forum fetch fans
    out concurrently with results reassembled in forum order.
    """
    courses = client.get_courses()
    course_ids = [c["id"] for c in courses if c.get("id")]
    course_names = {
        c["id"]: str(c.get("shortname") or c["id"]) for c in courses if c.get("id")
    }
    forums = normalize_forums(client.get_forums_by_courses(course_ids), course_id=0)

    def _fetch_forum(forum: dict[str, Any]) -> list[dict[str, Any]]:
        course_id = forum.get("course_id") or 0
        discussions = normalize_forum_discussions(
            client.get_forum_discussions(forum["forum_id"]),
            course_id=course_id,
            forum_id=forum["forum_id"],
            forum_name=forum.get("name", ""),
            base_url=client.base_url,
        )
        for discussion in discussions:
            discussion["course_shortname"] = course_names.get(
                course_id, str(course_id)
            )
        return discussions

    records: list[dict[str, Any]] = []
    for per_forum in run_parallel(
        forums,
        _fetch_forum,
        label_fn=lambda f: str(f.get("name") or f.get("forum_id") or ""),
        on_progress=on_progress,
    ):
        records.extend(per_forum)
    return records


def collect_snapshots(
    client: MoodleClient,
    *,
    lookahead_days: int = SYNC_LOOKAHEAD_DAYS,
    on_progress: ProgressCallback | None = None,
) -> tuple[
    dict[str, list[dict[str, Any]] | None],
    dict[str, list[Any] | None],
    list[str],
]:
    """Fetch the metadata-only snapshot for every sync category.

    Returns ``(snapshots, scopes, warnings)``. A category whose fetch
    failed is ``None`` in *snapshots* with a matching entry in
    *warnings*. ``scopes`` records per-category coverage where partial
    success is tolerated (grades: the course ids whose gradebooks were
    actually readable); ``None`` scope means full coverage.

    ``on_progress`` (default silent) receives per-course/per-forum progress
    from the grades, files, and forums fan-outs, each label prefixed with
    its phase (e.g. ``files: ECON101``).
    """
    warnings: list[str] = []
    scopes: dict[str, list[Any] | None] = {name: None for name in SYNC_CATEGORIES}

    def _phase(name: str) -> ProgressCallback | None:
        if on_progress is None:
            return None
        return lambda done, total, label: on_progress(
            done, total, f"{name}: {label}"
        )

    try:
        course_ids = [c["id"] for c in client.get_courses() if c.get("id")]
    except MoodleWriteAttemptError:
        raise
    except Exception as exc:
        logger.warning("sync course discovery failed: %s", exc)
        warnings.append(f"courses: {exc}")
        return {name: None for name in SYNC_CATEGORIES}, scopes, warnings

    grades_result = _safe_fetch(
        "grades", warnings,
        lambda: collect_grades(client, on_progress=_phase("grades")),
    )
    grades_snapshot: list[dict[str, Any]] | None = None
    if grades_result is not None:
        grades_snapshot = grades_result["grades"]
        failed_course_ids = set()
        for grade_warning in grades_result.get("warnings", []):
            failed_course_ids.add(grade_warning.get("course_id"))
            warnings.append(
                f"grades: {grade_warning.get('course_shortname', '?')}: "
                f"{grade_warning.get('message', '')}"
            )
        scopes["grades"] = [
            cid for cid in course_ids if cid not in failed_course_ids
        ]

    snapshots: dict[str, list[dict[str, Any]] | None] = {
        "deadlines": _safe_fetch(
            "deadlines", warnings,
            lambda: get_upcoming_deadlines(
                client, lookahead_days=lookahead_days, strict=True,
            ),
        ),
        "files": _safe_fetch(
            "files", warnings,
            lambda: _fetch_file_metadata(client, on_progress=_phase("files")),
        ),
        "grades": grades_snapshot,
        "forums": _safe_fetch(
            "forums", warnings,
            lambda: _fetch_forum_discussions(client, on_progress=_phase("forums")),
        ),
    }
    return snapshots, scopes, warnings


def run_sync(
    client: MoodleClient,
    *,
    cache_path: str | Path | None = None,
    lookahead_days: int = SYNC_LOOKAHEAD_DAYS,
    now: int | None = None,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Sync metadata into the local cache and return detected changes.

    ``on_progress`` (default silent) is forwarded to the per-course /
    per-forum snapshot fan-outs so callers can show live progress while the
    network-bound fetch phase runs; the diff/write phase against the cache
    stays single-threaded.
    """
    started_at = int(time.time()) if now is None else int(now)
    site = client.base_url
    snapshots, scopes, warnings = collect_snapshots(
        client, lookahead_days=lookahead_days, on_progress=on_progress,
    )

    categories: dict[str, Any] = {}
    changes: list[dict[str, Any]] = []

    with CacheStore(cache_path) as cache:
        # Serialize the diff/write phase: a concurrent sync waits here
        # and then diffs against the committed state instead of racing
        # this one and double-recording the same events.
        cache.begin_immediate()
        for name in SYNC_CATEGORIES:
            snapshot = snapshots[name]
            if snapshot is None:
                categories[name] = {"synced": False, "items": 0, "new": 0,
                                    "updated": 0, "adopted": 0,
                                    "baseline": False}
                continue

            spec = _CATEGORIES[name]
            prior_state = cache.get_category_state(site, name)
            baseline = prior_state is None
            prior_scope = None if prior_state is None else prior_state["scope"]
            prior_scope_set = None if prior_scope is None else set(prior_scope)
            prior = cache.get_items(site, name)
            new_count = updated_count = adopted_count = 0

            for payload in snapshot:
                item_key = spec.key_fn(payload)
                fingerprint = spec.fingerprint(payload)
                old = prior.get(item_key)

                change: dict[str, Any] | None = None
                if old is None and not baseline:
                    in_prior_scope = (
                        prior_scope_set is None
                        or spec.scope_fn is None
                        or spec.scope_fn(payload) in prior_scope_set
                    )
                    if in_prior_scope:
                        new_count += 1
                        if spec.new_item_is_change(payload):
                            change = _build_change(
                                spec, spec.new_kind, payload,
                                before=None, detected_at=started_at,
                            )
                    else:
                        # The item's course was not covered by the last
                        # successful sync — adopt it silently instead of
                        # reporting a spurious change.
                        adopted_count += 1
                elif old is not None and old["fingerprint"] != fingerprint:
                    updated_count += 1
                    change = _build_change(
                        spec, spec.updated_kind, payload,
                        before=old["payload"], detected_at=started_at,
                    )

                if change is not None:
                    changes.append({**change, "category": name, "item_key": item_key})
                    cache.record_change(site, name, item_key, change, now=started_at)

                cache.upsert_item(
                    site, name, item_key, fingerprint, payload, now=started_at,
                )

            cache.set_category_state(
                site, name, now=started_at, scope=scopes.get(name),
            )
            categories[name] = {
                "synced": True,
                "items": len(snapshot),
                "new": new_count,
                "updated": updated_count,
                "adopted": adopted_count,
                "baseline": baseline,
            }

        finished_at = int(time.time()) if now is None else int(now)
        summary = {
            "categories": categories,
            "changes": len(changes),
            "warnings": warnings,
        }
        cache.record_sync_run(site, started_at, finished_at, summary)
        cache.commit()
        resolved_path = str(cache.path)

    return {
        "site": site,
        "synced_at": started_at,
        "categories": categories,
        "changes": changes,
        "warnings": warnings,
        "cache_path": resolved_path,
    }


def _build_change(
    spec: _Category,
    kind: str,
    payload: dict[str, Any],
    *,
    before: dict[str, Any] | None,
    detected_at: int,
) -> dict[str, Any]:
    course_id, course_shortname = spec.course_fn(payload)
    return change_record(
        kind=kind,
        title=spec.title_fn(payload),
        detected_at=detected_at,
        course_id=course_id,
        course_shortname=course_shortname,
        before=spec.diff_view(before),
        after=spec.diff_view(payload),
    )


def get_recent_changes(
    site: str,
    *,
    cache_path: str | Path | None = None,
    since_days: int = 7,
    since_ts: int | None = None,
    category: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return change events recorded by previous syncs, newest first.

    ``since_ts`` (a Unix timestamp) takes precedence over ``since_days``
    so callers with sub-day windows are not rounded up to whole days.
    """
    if category is not None and category not in SYNC_CATEGORIES:
        raise ValueError(
            f"unknown category '{category}'. "
            f"Valid categories: {', '.join(SYNC_CATEGORIES)}"
        )
    if since_ts is None:
        since_ts = int(time.time()) - max(0, since_days) * 86400
    with CacheStore(cache_path) as cache:
        return cache.get_changes(
            site, since_ts=int(since_ts), category=category, limit=limit,
        )


def last_sync_at(
    site: str, *, cache_path: str | Path | None = None,
) -> int | None:
    """Return the finish time of the most recent sync for *site*, if any."""
    with CacheStore(cache_path) as cache:
        return cache.last_sync_at(site)


__all__ = [
    "SYNC_CATEGORIES",
    "SYNC_LOOKAHEAD_DAYS",
    "collect_snapshots",
    "default_cache_path",
    "get_recent_changes",
    "last_sync_at",
    "run_sync",
]
