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
- Every run reports an ``outcome``: ``success`` (every *selected*
  category synced), ``partial`` (some did), ``failed`` (none did —
  including the run that is refused before it starts because the cache
  belongs to another account), or ``skipped`` (another process held the
  sync lock, so this run made no requests at all). Callers branch on that
  rather than inferring health from an empty change list: a sync that
  fetched nothing used to look exactly like a sync that found nothing new.
- A category the run did not *select* is not a category that failed.
  Unselected categories are reported with ``selected: false``, are left
  out of the outcome entirely, and have their cached rows, their category
  state, and their change history left exactly as they were — no
  tombstones, no "disappeared" events, no reset baseline. Re-enabling one
  later resumes the diff from where it stopped.
- One sync per site at a time, enforced across processes by
  :mod:`worsaga.synclock`, so a ``watch`` loop and a scheduled run cannot
  fetch every course twice at once.
- Unattended runs consult the circuit breaker in :mod:`worsaga.syncstate`
  first: once a sync has had its credentials rejected they stop before
  touching the network until a foreground sync succeeds.

What a run collects
-------------------
Two data-minimisation defaults sit here rather than at the surfaces, so
the CLI, the MCP server, ``watch``, and the scheduled auto-sync cannot
disagree about them:

- **Forums are not collected by an unattended run.** A forum discussion
  is other people's writing, and a scheduled job accumulating it in the
  background is a different proposition from a student opening
  ``worsaga updates``. ``deadlines``, ``files``, and ``grades`` are the
  unattended default; a foreground ``worsaga sync`` still collects all
  four. Either way ``--categories`` (``WORSAGA_SYNC_CATEGORIES``) decides.
- **Instructor feedback text is never persisted** unless it is explicitly
  asked for. The cache keeps ``feedback_present`` and a truncated
  ``feedback_hash`` so a feedback-only change is still detected, and the
  full text stays where it was always fine: the live ``worsaga grades``
  view, which fetches and prints it without storing anything.

Shared by the CLI (``worsaga sync`` / ``worsaga changes``) and the MCP
server (``sync_now`` / ``get_changes``). Tokens and authenticated URLs
never reach the cache — see :mod:`worsaga.cache`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from worsaga.cache import CacheStore, default_cache_path
from worsaga.client import MoodleClient, MoodleWriteAttemptError
from worsaga.concurrency import ProgressCallback, run_parallel
from worsaga.deadlines import get_upcoming_deadlines
from worsaga.forums import normalize_forum_discussions, normalize_forums
from worsaga.grades import collect_grades
from worsaga.materials import extract_materials, strip_file_urls
from worsaga.models import change_record
from worsaga.principal import known_principal
from worsaga.synclock import SyncLock
from worsaga.syncstate import (
    circuit_message,
    circuit_state,
    classify_failure,
    record_outcome,
    worst_failure_class,
)

logger = logging.getLogger(__name__)

SYNC_LOOKAHEAD_DAYS = 60

#: Every category a sync knows how to collect, in collection order.
SYNC_CATEGORIES = ("deadlines", "files", "grades", "forums")

#: What a run nobody is watching collects by default — a ``watch`` cycle
#: or the scheduled auto-sync. ``forums`` is deliberately absent: it is
#: the one category made of other people's writing, and quietly
#: accumulating it in the background is not a default anyone asked for.
#: Opt back in with ``--categories`` or ``WORSAGA_SYNC_CATEGORIES``.
UNATTENDED_SYNC_CATEGORIES = ("deadlines", "files", "grades")

#: Persistent default for both foreground and unattended runs, overriding
#: the built-ins above. Comma-separated category names, or ``all``.
SYNC_CATEGORIES_ENV = "WORSAGA_SYNC_CATEGORIES"

#: Opt in to persisting full instructor feedback text in the local cache.
#: Off by default everywhere; see :func:`resolve_store_feedback`.
STORE_FEEDBACK_ENV = "WORSAGA_SYNC_STORE_FEEDBACK"

#: Hex characters kept from the feedback digest. Sixty-four bits is far
#: more than change detection over one person's gradebook needs, and a
#: short digest is a worse oracle for confirming a guess at the text than
#: a full one would be.
FEEDBACK_HASH_CHARS = 16

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _env_flag(name: str) -> bool:
    """Return whether environment variable *name* is set to a true value."""
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def parse_sync_categories(value: str | Sequence[str]) -> tuple[str, ...]:
    """Return the validated, canonically ordered categories *value* names.

    Accepts a comma-separated string (``"deadlines,grades"``), a sequence
    of names, or the literal ``all``. Names are case-insensitive and
    de-duplicated, and the result is always in :data:`SYNC_CATEGORIES`
    order so a selection reads the same however it was written.

    Raises :class:`ValueError` naming the valid categories for an unknown
    name or an empty selection. A privacy control that silently ignores
    what it was told is worse than one that refuses to start.
    """
    if not isinstance(value, str):
        value = ",".join(str(item) for item in value)
    text = value.strip()
    if text.lower() == "all":
        return SYNC_CATEGORIES
    names = [part.strip().lower() for part in text.split(",")]
    names = [name for name in names if name]
    valid = f"Valid categories: {', '.join(SYNC_CATEGORIES)} (or 'all')."
    if not names:
        raise ValueError(f"no sync categories were named. {valid}")
    unknown = [name for name in names if name not in SYNC_CATEGORIES]
    if unknown:
        raise ValueError(
            f"unknown sync category '{unknown[0]}'. {valid}"
        )
    chosen = set(names)
    return tuple(name for name in SYNC_CATEGORIES if name in chosen)


def resolve_sync_categories(
    categories: str | Sequence[str] | None = None,
    *,
    unattended: bool = False,
) -> tuple[str, ...]:
    """Return the categories a run should collect.

    Precedence: an explicit *categories* argument (the ``--categories``
    option, the MCP ``categories`` parameter), then
    ``WORSAGA_SYNC_CATEGORIES``, then the built-in default —
    :data:`UNATTENDED_SYNC_CATEGORIES` for a run nobody is watching and
    :data:`SYNC_CATEGORIES` for a foreground one.
    """
    if categories is not None:
        return parse_sync_categories(categories)
    from_env = os.environ.get(SYNC_CATEGORIES_ENV, "").strip()
    if from_env:
        return parse_sync_categories(from_env)
    return UNATTENDED_SYNC_CATEGORIES if unattended else SYNC_CATEGORIES


def resolve_store_feedback(store_feedback: bool | None = None) -> bool:
    """Return whether this run may persist full instructor feedback text.

    ``False`` unless explicitly asked for, in every mode — foreground and
    unattended alike. The brief only requires unattended runs to minimise,
    but there is no run for which storing the text is *needed*: the live
    ``worsaga grades`` view fetches and prints it without the cache, and
    change detection works from the hash. So the opt-in
    (``--store-feedback`` / ``WORSAGA_SYNC_STORE_FEEDBACK``) is the only
    way the text reaches disk, and it is honoured wherever it is given —
    silently ignoring a setting the user deliberately turned on would be
    its own kind of surprise.
    """
    if store_feedback is not None:
        return bool(store_feedback)
    return _env_flag(STORE_FEEDBACK_ENV)


#: A fingerprint written before the shape was versioned: a bare SHA-256
#: hex digest and nothing else. Matched exactly, so a truncated, empty, or
#: differently-tagged value is classified as unknown rather than guessed at.
_BARE_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


def _fingerprint(payload: dict[str, Any], fields: tuple[str, ...]) -> str:
    """Return a stable hash over the change-relevant fields of *payload*."""
    data = {field: payload.get(field) for field in fields}
    text = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def feedback_hash(feedback: Any) -> str:
    """Return the stored stand-in for one grade item's feedback text.

    Empty for no feedback, otherwise the leading
    :data:`FEEDBACK_HASH_CHARS` of its SHA-256. Two runs that see the same
    words produce the same value, so a feedback-only edit is still a
    detected grade change — without the words themselves ever reaching the
    cache, the change log, or a change event replayed to an agent.
    """
    text = str(feedback or "")
    if not text:
        return ""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return digest[:FEEDBACK_HASH_CHARS]


def _prepare_grade(
    payload: dict[str, Any], *, store_feedback: bool,
) -> dict[str, Any]:
    """Return a grade payload with feedback reduced to presence and a hash.

    The two derived fields are added in *both* modes so the fingerprint
    means the same thing whichever way a run was invoked: turning the
    opt-in on or off changes what is stored, never whether every grade
    suddenly looks changed.
    """
    prepared = dict(payload)
    text = str(payload.get("feedback") or "")
    prepared["feedback_present"] = bool(text)
    prepared["feedback_hash"] = feedback_hash(text)
    if not store_feedback:
        prepared.pop("feedback", None)
    return prepared


class _Category:
    """One synced category: how to key, fingerprint, and describe items."""

    def __init__(
        self,
        name: str,
        *,
        fingerprint_fields: tuple[str, ...],
        fingerprint_version: int = 1,
        migration_fields: tuple[str, ...] = (),
        extra_diff_fields: tuple[str, ...] = (),
        new_kind: str,
        updated_kind: str,
        key_fn: Callable[[dict[str, Any]], str],
        title_fn: Callable[[dict[str, Any]], str],
        course_fn: Callable[[dict[str, Any]], tuple[int | None, str]],
        scope_fn: Callable[[dict[str, Any]], Any] | None = None,
        new_item_is_change: Callable[[dict[str, Any]], bool] = lambda payload: True,
        prepare_fn: Callable[..., dict[str, Any]] | None = None,
    ):
        self.name = name
        self.fingerprint_fields = fingerprint_fields
        self.fingerprint_version = fingerprint_version
        self.migration_fields = migration_fields
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
        self.prepare_fn = prepare_fn

    def prepare(
        self, payload: dict[str, Any], *, store_feedback: bool,
    ) -> dict[str, Any]:
        """Return the payload as it should be diffed, stored, and reported."""
        if self.prepare_fn is None:
            return payload
        return self.prepare_fn(payload, store_feedback=store_feedback)

    def fingerprint(self, payload: dict[str, Any]) -> str:
        """Return the stored fingerprint, tagged when the shape has moved on.

        Version 1 is the original bare hex digest, kept exactly as it was
        so the three categories that never changed shape do not all look
        modified. A category that *has* changed which fields it
        fingerprints carries a ``v<n>:`` prefix, which is what lets
        :meth:`is_current_fingerprint` recognise a row written by an
        older Worsaga instead of reporting it as a change.
        """
        digest = _fingerprint(payload, self.fingerprint_fields)
        if self.fingerprint_version <= 1:
            return digest
        return f"v{self.fingerprint_version}:{digest}"

    def fingerprint_state(self, value: Any) -> str:
        """Classify a stored fingerprint: ``current``, ``legacy``, ``unknown``.

        Three states, not two, because "not the current shape" covers two
        very different situations. A **legacy** value is one this version
        knows how to reason about — the bare 64-character hex digest every
        Worsaga wrote before the shape was versioned — so its payload can
        be compared field by field to see whether anything *actually*
        changed. An **unknown** value (empty, truncated, or tagged with a
        version this build has never heard of, such as a row written by a
        newer Worsaga and then opened by an older one) cannot be reasoned
        about at all: it is adopted silently, because guessing would mean
        either a change storm or a fabricated diff.
        """
        text = str(value or "")
        if text.startswith(f"v{self.fingerprint_version}:"):
            return "current"
        if self.fingerprint_version <= 1:
            # This category has never changed shape, so anything stored is
            # by definition the current shape.
            return "current"
        if _BARE_DIGEST_RE.fullmatch(text):
            return "legacy"
        return "unknown"

    def migration_changed(
        self, before: dict[str, Any] | None, after: dict[str, Any],
    ) -> bool:
        """Whether a migrating item also changed in a way worth reporting.

        Compares only :attr:`migration_fields` — the fields whose meaning
        did not move between fingerprint versions. Migration and a real
        change are not mutually exclusive: a grade that went from 70 to 80
        on the same run that re-fingerprinted it is still news, and
        adopting the whole row silently would swallow it.
        """
        if not self.migration_fields or before is None:
            return False
        return any(
            before.get(field) != after.get(field)
            for field in self.migration_fields
        )

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
        # ``feedback_hash`` stands in for the instructor's words: a
        # feedback-only edit still changes the fingerprint, but neither the
        # items table nor a change event ever holds the text. Bumping the
        # version is what stops the field swap from reporting every grade
        # in an existing cache as updated exactly once.
        fingerprint_fields=(
            "grade_display", "percentage", "status", "feedback_hash",
        ),
        fingerprint_version=2,
        # The fields whose meaning is identical either side of the shape
        # change, so a migrating row can still be judged. Deliberately
        # excludes anything feedback-derived: the old row has the text and
        # the new one has a hash, which are not comparable and would make
        # every migration look like a change.
        migration_fields=(
            "grade_display", "percentage", "status", "graded", "graded_at",
        ),
        extra_diff_fields=("feedback_present",),
        prepare_fn=_prepare_grade,
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
    failures: list[str] | None = None,
) -> Any:
    """Run one category fetch; a failure becomes a warning, not a crash.

    Returns None on failure so the category is skipped entirely — an
    errored fetch must never look like an empty (or changed) Moodle. When
    *failures* is given, the exception's coarse class
    (:func:`worsaga.syncstate.classify_failure`) is appended to it — the
    warning text is for the user, the class is what decides whether an
    unattended loop should keep trying.
    """
    try:
        return fn()
    except MoodleWriteAttemptError:
        raise
    except Exception as exc:
        logger.warning("sync fetch failed for %s: %s", name, exc)
        warnings.append(f"{name}: {exc}")
        if failures is not None:
            failures.append(classify_failure(exc))
        return None


def _fetch_file_metadata(
    client: MoodleClient,
    *,
    courses: list[dict[str, Any]] | None = None,
    on_progress: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    """Return token-free material metadata across all enrolled courses.

    The per-course ``get_course_contents`` fetch fans out concurrently and
    the results are reassembled in course order. As with the sequential
    version, a per-course failure propagates (this snapshot is strict — an
    errored fetch must never look like an empty Moodle); the caller's
    :func:`_safe_fetch` turns it into a skipped category.

    *courses* reuses the enrolled-course list the run already fetched;
    omitting it falls back to fetching one.
    """
    if courses is None:
        courses = client.get_courses()
    courses = [c for c in courses if c.get("id")]

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
    courses: list[dict[str, Any]] | None = None,
    on_progress: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    """Return all forum discussions across enrolled courses.

    Unlike :func:`worsaga.forums.get_latest_updates`, per-forum failures
    propagate: for change detection an errored fetch must never be
    mistaken for a Moodle with no forum activity. The per-forum fetch fans
    out concurrently with results reassembled in forum order.

    *courses* reuses the enrolled-course list the run already fetched;
    omitting it falls back to fetching one.
    """
    if courses is None:
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
    categories: Sequence[str] | None = None,
) -> tuple[
    dict[str, list[dict[str, Any]] | None],
    dict[str, list[Any] | None],
    list[str],
]:
    """Fetch the metadata-only snapshot for every selected sync category.

    Returns ``(snapshots, scopes, warnings)``. A category whose fetch
    failed is ``None`` in *snapshots* with a matching entry in
    *warnings*; a category that was not selected is **absent** from
    *snapshots* altogether, because ``None`` already means "tried and
    failed" and the two must never be confused. *categories* defaults to
    all of them.

    ``scopes`` records per-category coverage where partial success is
    tolerated (grades: the course ids whose gradebooks were actually
    readable); ``None`` scope means full coverage.

    ``on_progress`` (default silent) receives per-course/per-forum progress
    from the grades, files, and forums fan-outs, each label prefixed with
    its phase (e.g. ``files: ECON101``).
    """
    snapshots, scopes, warnings, _ = _collect_snapshots(
        client, lookahead_days=lookahead_days, on_progress=on_progress,
        categories=categories,
    )
    return snapshots, scopes, warnings


def _collect_snapshots(
    client: MoodleClient,
    *,
    lookahead_days: int = SYNC_LOOKAHEAD_DAYS,
    on_progress: ProgressCallback | None = None,
    heartbeat: Callable[[], None] | None = None,
    categories: Sequence[str] | None = None,
) -> tuple[
    dict[str, list[dict[str, Any]] | None],
    dict[str, list[Any] | None],
    list[str],
    list[str],
]:
    """:func:`collect_snapshots` plus the coarse class of each failure.

    The extra element is what lets :func:`run_sync` say *why* a run
    failed (``auth``, ``network``, ``rate_limited``, ``other``) rather
    than only that it did. Kept separate so the public three-value
    signature callers already destructure stays exactly as it was.

    The enrolled-course list is fetched **once** here and handed to every
    fan-out that needs it. Previously each of course discovery, files, and
    forums asked for it again — three identical requests per run, plus a
    fourth from the grades collector — for a list that must be consistent
    within one run anyway. Fetching it here also keeps the client's
    enrolment memo refreshed once per run, which is what bounds the scope
    checks for the rest of the run.

    ``heartbeat`` (when given) is called at each category boundary. It is
    how :class:`worsaga.synclock.SyncLock` says the owner is still working
    on a platform that can only judge abandonment by age.

    An unselected category costs nothing here: its fetch is not attempted,
    so no request is made for it and it contributes no warning and no
    failure class.
    """
    selected = tuple(
        SYNC_CATEGORIES if categories is None else categories
    )
    warnings: list[str] = []
    failures: list[str] = []
    scopes: dict[str, list[Any] | None] = {name: None for name in selected}

    def _phase(name: str) -> ProgressCallback | None:
        if on_progress is None:
            return None
        return lambda done, total, label: on_progress(
            done, total, f"{name}: {label}"
        )

    def _beat() -> None:
        if heartbeat is not None:
            heartbeat()

    try:
        courses = client.get_courses()
    except MoodleWriteAttemptError:
        raise
    except Exception as exc:
        logger.warning("sync course discovery failed: %s", exc)
        warnings.append(f"courses: {exc}")
        failures.append(classify_failure(exc))
        return (
            {name: None for name in selected}, scopes, warnings, failures,
        )

    course_ids = [c["id"] for c in courses if c.get("id")]
    _beat()

    grades_result = _safe_fetch(
        "grades", warnings,
        lambda: collect_grades(
            client, courses=courses, on_progress=_phase("grades"),
        ),
        failures,
    ) if "grades" in selected else None
    grades_snapshot: list[dict[str, Any]] | None = None
    if grades_result is not None:
        grades_snapshot = grades_result["grades"]
        grade_warnings = grades_result.get("warnings", [])
        failed_course_ids = set()
        for grade_warning in grade_warnings:
            failed_course_ids.add(grade_warning.get("course_id"))
            warnings.append(
                f"grades: {grade_warning.get('course_shortname', '?')}: "
                f"{grade_warning.get('message', '')}"
            )
        covered = [cid for cid in course_ids if cid not in failed_course_ids]
        if course_ids and not covered:
            # Grades is the one category that tolerates per-course
            # failures, and that tolerance has an edge: when *every*
            # gradebook fails, the collector still returns successfully
            # with an empty list. Recording that as a synced category
            # would report a run that read nothing as a success, reset the
            # failure streak, and close the credential circuit — which is
            # exactly what a revoked token looks like from here. An empty
            # result is only ever "no grades" when there was at least one
            # course it could have come from.
            grades_snapshot = None
            failures.append(worst_failure_class([
                str(grade_warning.get("failure_class") or "other")
                for grade_warning in grade_warnings
            ]))
            warnings.append(
                f"grades: no gradebook could be read for any of the "
                f"{len(course_ids)} enrolled course(s), so the category was "
                "skipped rather than recorded as empty"
            )
        else:
            scopes["grades"] = covered
    _beat()

    deadlines_snapshot = _safe_fetch(
        "deadlines", warnings,
        lambda: get_upcoming_deadlines(
            client, lookahead_days=lookahead_days, strict=True,
            courses=courses,
        ),
        failures,
    ) if "deadlines" in selected else None
    _beat()
    files_snapshot = _safe_fetch(
        "files", warnings,
        lambda: _fetch_file_metadata(
            client, courses=courses, on_progress=_phase("files"),
        ),
        failures,
    ) if "files" in selected else None
    _beat()
    forums_snapshot = _safe_fetch(
        "forums", warnings,
        lambda: _fetch_forum_discussions(
            client, courses=courses, on_progress=_phase("forums"),
        ),
        failures,
    ) if "forums" in selected else None
    _beat()

    # files and forums are deliberately strict inside their fan-outs: one
    # failed course or forum propagates and lands the whole category in
    # _safe_fetch as None. There is no "swallowed every unit" edge to
    # guard there the way there is for grades.
    #
    # Keyed on the selection, not on SYNC_CATEGORIES: an unselected
    # category must be absent rather than None, or the caller would read
    # "not collected" as "collection failed".
    collected: dict[str, list[dict[str, Any]] | None] = {
        "deadlines": deadlines_snapshot,
        "files": files_snapshot,
        "grades": grades_snapshot,
        "forums": forums_snapshot,
    }
    snapshots = {name: collected[name] for name in SYNC_CATEGORIES
                 if name in selected}
    return snapshots, scopes, warnings, failures


def sync_outcome(categories: dict[str, Any]) -> str:
    """Return ``success`` / ``partial`` / ``failed`` for a category map.

    The one rule the rest of Worsaga branches on: every category synced is
    a success, some is partial, none is a failure. A run that fetched
    nothing must never be reported the same way as a run that found
    nothing new.

    Only *selected* categories count. A category the run deliberately did
    not collect is not evidence about whether the site was reachable, so
    counting it would turn every minimised run into a permanent
    ``partial`` — and a permanent ``partial`` is a non-zero exit code, a
    failed scheduled task, and eventually an ignored one. Entries without
    a ``selected`` key are treated as selected, so a category map from an
    older Worsaga (or a hand-built one in a test) reads exactly as before.
    """
    considered = [
        stats for stats in categories.values() if stats.get("selected", True)
    ]
    if not considered:
        return "failed"
    synced = sum(1 for stats in considered if stats.get("synced"))
    if synced == len(considered):
        return "success"
    return "partial" if synced else "failed"


def _empty_categories(
    categories: Sequence[str] = SYNC_CATEGORIES,
) -> dict[str, Any]:
    """Return the per-category stats for a run that collected nothing."""
    selected = set(categories)
    return {
        name: {"synced": False, "selected": name in selected, "items": 0,
               "new": 0, "updated": 0, "adopted": 0, "migrated": 0,
               "baseline": False}
        for name in SYNC_CATEGORIES
    }


def _resolved_cache_path(cache_path: str | Path | None) -> str:
    """Return the cache path as text without creating anything.

    The refusal paths below report where the cache *would* be; opening a
    :class:`~worsaga.cache.CacheStore` to find out would create the
    database, the directory, and the schema as a side effect of a run that
    is deliberately not happening.
    """
    return str(Path(cache_path) if cache_path else default_cache_path())


def _no_run_result(
    site: str,
    *,
    started_at: int,
    cache_path: str | Path | None,
    outcome: str,
    warning: str,
    categories: Sequence[str] = SYNC_CATEGORIES,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the result shape for a run that did not (or could not) go."""
    result: dict[str, Any] = {
        "site": site,
        "synced_at": started_at,
        "outcome": outcome,
        "categories": _empty_categories(categories),
        "selected_categories": list(categories),
        "changes": [],
        "warnings": [warning],
        "cache_path": _resolved_cache_path(cache_path),
    }
    if extra:
        result.update(extra)
    return result


def run_sync(
    client: MoodleClient,
    *,
    cache_path: str | Path | None = None,
    lookahead_days: int = SYNC_LOOKAHEAD_DAYS,
    now: int | None = None,
    on_progress: ProgressCallback | None = None,
    unattended: bool = False,
    categories: str | Sequence[str] | None = None,
    store_feedback: bool | None = None,
) -> dict[str, Any]:
    """Sync metadata into the local cache and return detected changes.

    ``on_progress`` (default silent) is forwarded to the per-course /
    per-forum snapshot fan-outs so callers can show live progress while the
    network-bound fetch phase runs; the diff/write phase against the cache
    stays single-threaded.

    ``unattended=True`` marks a run nobody is watching — a ``watch`` cycle
    or the scheduled auto-sync. Those consult the credential circuit
    breaker first and refuse to touch the network while it is open,
    *and* collect the narrower :data:`UNATTENDED_SYNC_CATEGORIES` by
    default. Foreground runs always attempt, because a successful one is
    what closes the circuit.

    ``categories`` overrides that default either way — a comma-separated
    string or a sequence of names, validated by
    :func:`parse_sync_categories`. ``store_feedback`` overrides
    ``WORSAGA_SYNC_STORE_FEEDBACK``; see :func:`resolve_store_feedback`.

    The result always carries an ``outcome``: ``success``, ``partial``,
    ``failed``, or ``skipped`` (another process was already syncing this
    site), plus ``selected_categories``. ``skipped`` and circuit-refused
    runs make no requests at all.
    """
    started_at = int(time.time()) if now is None else int(now)
    site = client.base_url
    # Resolved before anything else: an unusable selection is a
    # configuration error, and one that has to surface before a run makes
    # requests it may not have been allowed to make.
    selected = resolve_sync_categories(categories, unattended=unattended)
    keep_feedback = resolve_store_feedback(store_feedback)
    # Demo mode is offline and single-purpose: no lock file, no
    # cross-process state, nothing left behind on the user's machine.
    is_demo = bool(getattr(client, "is_demo", False))

    if unattended and not is_demo:
        blocked = circuit_state(site)
        if blocked is not None:
            logger.warning(
                "Skipping the unattended sync of %s: %s",
                site, circuit_message(blocked),
            )
            return _no_run_result(
                site,
                started_at=started_at,
                cache_path=cache_path,
                outcome="failed",
                warning=circuit_message(blocked),
                categories=selected,
                extra={
                    "circuit_open": True,
                    "failure_class": str(blocked.get("failure_class") or "auth"),
                },
            )

    lock = None if is_demo else SyncLock(site, _resolved_cache_path(cache_path))
    if lock is not None and not lock.acquire():
        logger.info("Sync of %s skipped: %s", site, lock.busy_message())
        result = _no_run_result(
            site,
            started_at=started_at,
            cache_path=cache_path,
            outcome="skipped",
            warning=lock.busy_message(),
            categories=selected,
            extra={"skipped_reason": "sync_in_progress"},
        )
        record_outcome(site, "skipped", now=started_at)
        return result

    try:
        return _run_sync_locked(
            client,
            site=site,
            started_at=started_at,
            cache_path=cache_path,
            lookahead_days=lookahead_days,
            now=now,
            on_progress=on_progress,
            is_demo=is_demo,
            heartbeat=None if lock is None else lock.touch,
            selected=selected,
            store_feedback=keep_feedback,
        )
    finally:
        # Always, including on MoodleWriteAttemptError and KeyboardInterrupt:
        # a lock left behind by an exception would block every later sync
        # until the TTL expired.
        if lock is not None:
            lock.release()


def _run_sync_locked(
    client: MoodleClient,
    *,
    site: str,
    started_at: int,
    cache_path: str | Path | None,
    lookahead_days: int,
    now: int | None,
    on_progress: ProgressCallback | None,
    is_demo: bool,
    heartbeat: Callable[[], None] | None = None,
    selected: Sequence[str] = SYNC_CATEGORIES,
    store_feedback: bool = False,
) -> dict[str, Any]:
    """Collect, diff, and write one sync run. The lock is already held."""
    snapshots, scopes, warnings, failures = _collect_snapshots(
        client, lookahead_days=lookahead_days, on_progress=on_progress,
        heartbeat=heartbeat, categories=selected,
    )

    def _finish(result: dict[str, Any]) -> dict[str, Any]:
        """Attach the outcome, record it for later runs, and return it."""
        outcome = sync_outcome(result["categories"])
        result["outcome"] = outcome
        if outcome == "failed":
            result["failure_class"] = worst_failure_class(failures)
        if not is_demo:
            record_outcome(
                site, outcome,
                failure_class=result.get("failure_class"),
                now=started_at,
            )
        return result

    categories: dict[str, Any] = {}
    changes: list[dict[str, Any]] = []

    with CacheStore(cache_path) as cache:
        # Serialize the diff/write phase: a concurrent sync waits here
        # and then diffs against the committed state instead of racing
        # this one and double-recording the same events.
        cache.begin_immediate()
        # First statement in the transaction, and the principal is read
        # here rather than before the fan-out: every authenticated read
        # injects the user id, so a collection that produced anything has
        # already verified the client. A cache belonging to a different
        # account raises here, and the rollback on close leaves it as it
        # was.
        if not cache.bind_principal(site, known_principal(client)):
            logger.warning(
                "Nothing could be fetched from %s this run, so the account "
                "behind it was never verified. The local sync cache at %s "
                "already belongs to another account, so no rows and no run "
                "record were written. Check the connection and credentials "
                "and sync again.",
                site, cache.path,
            )
            warnings.append(
                "nothing was written to the local cache: no Moodle call "
                "succeeded, so this run could not be attributed to the "
                "account the cache belongs to"
            )
            # Every category is unsynced here, so this returns "failed" —
            # which is the point: a principal-suppressed run used to be
            # indistinguishable from a completely successful one that
            # happened to find nothing.
            return _finish({
                "site": site,
                "synced_at": started_at,
                "categories": _empty_categories(selected),
                "selected_categories": list(selected),
                "changes": [],
                "warnings": warnings,
                "cache_path": str(cache.path),
            })
        for name in SYNC_CATEGORIES:
            if name not in snapshots:
                # Not selected. Nothing is fetched, diffed, written, or
                # expired: the cached rows, the category state, and the
                # change history stay exactly as the last run that did
                # collect this category left them, so re-enabling it later
                # resumes from that baseline rather than re-reporting
                # everything.
                categories[name] = {"synced": False, "selected": False,
                                    "items": 0, "new": 0, "updated": 0,
                                    "adopted": 0, "migrated": 0,
                                    "baseline": False}
                continue
            snapshot = snapshots[name]
            if snapshot is None:
                categories[name] = {"synced": False, "selected": True,
                                    "items": 0, "new": 0, "updated": 0,
                                    "adopted": 0, "migrated": 0,
                                    "baseline": False}
                continue

            spec = _CATEGORIES[name]
            prior_state = cache.get_category_state(site, name)
            baseline = prior_state is None
            prior_scope = None if prior_state is None else prior_state["scope"]
            prior_scope_set = None if prior_scope is None else set(prior_scope)
            prior = cache.get_items(site, name)
            new_count = updated_count = adopted_count = migrated_count = 0

            for raw_payload in snapshot:
                payload = spec.prepare(
                    raw_payload, store_feedback=store_feedback,
                )
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
                    state = spec.fingerprint_state(old["fingerprint"])
                    if state == "current":
                        updated_count += 1
                        change = _build_change(
                            spec, spec.updated_kind, payload,
                            before=old["payload"], detected_at=started_at,
                        )
                    elif state == "legacy":
                        # The row was written by a Worsaga that
                        # fingerprinted different fields, so the two hashes
                        # are not comparable and the difference between
                        # them says nothing about Moodle. Adopt the new
                        # shape rather than announcing that every grade
                        # changed the day the user upgraded — a report like
                        # that teaches people to ignore reports.
                        #
                        # But adopt it *without* going blind: the fields
                        # that mean the same thing either side of the
                        # change are still comparable, so a grade that
                        # really did move is reported on this run as well.
                        # Migration and a real change are not alternatives.
                        migrated_count += 1
                        if spec.migration_changed(old["payload"], payload):
                            updated_count += 1
                            change = _build_change(
                                spec, spec.updated_kind, payload,
                                before=old["payload"], detected_at=started_at,
                            )
                    else:
                        # Empty, truncated, or tagged with a version this
                        # build does not know (a row written by a newer
                        # Worsaga). Nothing about it can be compared, so it
                        # is adopted quietly and noted for a debug log
                        # rather than turned into a diff nobody can trust.
                        migrated_count += 1
                        logger.debug(
                            "adopting an unrecognised %s fingerprint for %s "
                            "on %s without reporting a change",
                            name, item_key, site,
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
                "selected": True,
                "items": len(snapshot),
                "new": new_count,
                "updated": updated_count,
                "adopted": adopted_count,
                "migrated": migrated_count,
                "baseline": baseline,
            }

        finished_at = int(time.time()) if now is None else int(now)
        outcome = sync_outcome(categories)
        # A run that synced nothing does not advance "last synced at".
        # ``sync_runs`` is the only source for that timestamp, and a
        # totally failed run left no rows and no category state behind
        # either, so recording it would tell every status surface the data
        # is fresh at the exact moment it stopped being fresh.
        if outcome != "failed":
            cache.record_sync_run(site, started_at, finished_at, {
                "categories": categories,
                "selected_categories": list(selected),
                "changes": len(changes),
                "warnings": warnings,
                "outcome": outcome,
            })
        cache.commit()
        resolved_path = str(cache.path)

    return _finish({
        "site": site,
        "synced_at": started_at,
        "categories": categories,
        "selected_categories": list(selected),
        "changes": changes,
        "warnings": warnings,
        "cache_path": resolved_path,
    })


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
    "FEEDBACK_HASH_CHARS",
    "STORE_FEEDBACK_ENV",
    "SYNC_CATEGORIES",
    "SYNC_CATEGORIES_ENV",
    "SYNC_LOOKAHEAD_DAYS",
    "UNATTENDED_SYNC_CATEGORIES",
    "collect_snapshots",
    "default_cache_path",
    "feedback_hash",
    "get_recent_changes",
    "last_sync_at",
    "parse_sync_categories",
    "resolve_store_feedback",
    "resolve_sync_categories",
    "run_sync",
    "sync_outcome",
]
