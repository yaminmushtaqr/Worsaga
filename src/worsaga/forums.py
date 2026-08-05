"""Forum and update readers."""

from __future__ import annotations

import logging
import time
from typing import Any

from worsaga.client import MoodleClient, MoodleWriteAttemptError
from worsaga.concurrency import ProgressCallback, run_parallel
from worsaga.models import as_bool, as_int, clean_text, forum_discussion_record

logger = logging.getLogger(__name__)

ANNOUNCEMENT_NAMES = {
    "announcements",
    "news forum",
    "general announcements",
    "course announcements",
}


def is_announcement_forum(name: str) -> bool:
    """Return True for common Moodle announcement forum names."""
    return clean_text(name).strip().lower() in ANNOUNCEMENT_NAMES


def normalize_forums(
    payload: dict[str, Any] | list[Any],
    *,
    course_id: int,
) -> list[dict[str, Any]]:
    """Normalize Moodle forum container payloads."""
    forums = payload.get("forums", []) if isinstance(payload, dict) else payload
    records: list[dict[str, Any]] = []
    for forum in forums if isinstance(forums, list) else []:
        if not isinstance(forum, dict):
            continue
        fid = as_int(forum.get("id"), 0) or 0
        name = clean_text(forum.get("name"))
        records.append({
            "course_id": as_int(forum.get("course"), course_id) or course_id,
            "forum_id": fid,
            "name": name,
            "type": str(forum.get("type") or ""),
            "intro": clean_text(forum.get("intro"), limit=180),
            "discussion_count": as_int(forum.get("numdiscussions")),
            "is_announcement": is_announcement_forum(name),
        })
    records.sort(key=lambda r: (not r["is_announcement"], r["name"].lower(), r["forum_id"]))
    return records


def normalize_forum_discussions(
    payload: dict[str, Any],
    *,
    course_id: int,
    forum_id: int,
    forum_name: str = "",
    base_url: str = "",
) -> list[dict[str, Any]]:
    """Normalize Moodle forum discussion payloads."""
    discussions = payload.get("discussions", []) if isinstance(payload, dict) else []
    records: list[dict[str, Any]] = []
    for discussion in discussions if isinstance(discussions, list) else []:
        if not isinstance(discussion, dict):
            continue
        discussion_id = as_int(
            discussion.get("discussion", discussion.get("id", discussion.get("discussionid"))),
            0,
        ) or 0
        modified = as_int(
            discussion.get("timemodified", discussion.get("modified", discussion.get("modifiedat")))
        )
        created = as_int(discussion.get("created", discussion.get("timecreated")))
        view_url = (
            f"{base_url}/mod/forum/discuss.php?d={discussion_id}"
            if base_url and discussion_id
            else ""
        )
        records.append(
            forum_discussion_record(
                course_id=course_id,
                forum_id=forum_id,
                forum_name=forum_name,
                discussion_id=discussion_id,
                name=discussion.get("name") or discussion.get("subject") or "",
                author=discussion.get("userfullname") or discussion.get("author") or "",
                created_at=created,
                modified_at=modified,
                unread_count=as_int(
                    discussion.get("numunread", discussion.get("unreadcount"))
                ),
                pinned=as_bool(discussion.get("pinned"), None),
                locked=as_bool(discussion.get("locked"), None),
                view_url=view_url,
            )
        )
    records.sort(key=lambda r: r["modified_at"] or r["created_at"] or 0, reverse=True)
    return records


def get_course_forums(client: MoodleClient, course_id: int) -> list[dict[str, Any]]:
    """Return forum containers for a course."""
    payload = client.get_forums_by_courses([course_id])
    return normalize_forums(payload, course_id=course_id)


def get_forum_discussions(
    client: MoodleClient,
    course_id: int,
    forum_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return forum discussions for one forum or all course forums."""
    forums = get_course_forums(client, course_id)
    if forum_id is not None and all(forum["forum_id"] != forum_id for forum in forums):
        forums.append({
            "course_id": course_id,
            "forum_id": forum_id,
            "name": str(forum_id),
            "is_announcement": False,
        })
    if forum_id is not None:
        forums = [forum for forum in forums if forum["forum_id"] == forum_id]

    records: list[dict[str, Any]] = []
    for forum in forums:
        try:
            payload = client.get_forum_discussions(forum["forum_id"])
        except MoodleWriteAttemptError:
            raise
        except Exception as exc:
            logger.warning(
                "Moodle forum discussion fetch failed for forum %s: %s",
                forum["forum_id"],
                exc,
            )
            continue
        records.extend(
            normalize_forum_discussions(
                payload if isinstance(payload, dict) else {},
                course_id=course_id,
                forum_id=forum["forum_id"],
                forum_name=forum.get("name", ""),
                base_url=client.base_url,
            )
        )
    records.sort(key=lambda r: r["modified_at"] or r["created_at"] or 0, reverse=True)
    return records


def get_latest_updates(
    client: MoodleClient,
    course_id: int | None = None,
    *,
    since_days: int = 7,
    on_progress: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    """Return recent forum discussions across one or all courses.

    Forum containers are discovered in one batched call; each forum's
    discussions are then fetched concurrently (see
    :func:`worsaga.concurrency.run_parallel`), which is the slow part across
    many courses. Per-forum failures stay logged warnings and are skipped,
    and the final list is sorted newest-first, so output is deterministic
    regardless of completion order. ``on_progress`` (default silent) reports
    one completed forum at a time.
    """
    courses = client.get_courses()
    if course_id is not None:
        courses = [course for course in courses if as_int(course.get("id")) == course_id] or [
            {"id": course_id, "shortname": str(course_id)}
        ]
    cutoff = int(time.time()) - since_days * 86400
    course_ids = [
        cid for cid in (as_int(course.get("id"), 0) or 0 for course in courses)
        if cid
    ]
    course_names = {
        as_int(course.get("id"), 0) or 0: str(course.get("shortname") or course.get("id"))
        for course in courses
    }
    try:
        forums_payload = client.get_forums_by_courses(course_ids)
        forums = normalize_forums(forums_payload, course_id=0)
    except MoodleWriteAttemptError:
        raise
    except Exception as exc:
        logger.warning("Moodle forum discovery failed for updates: %s", exc)
        forums = []
        for cid in course_ids:
            try:
                forums.extend(normalize_forums(
                    client.get_forums_by_courses([cid]),
                    course_id=cid,
                ))
            except MoodleWriteAttemptError:
                raise
            except Exception as course_exc:
                logger.warning(
                    "Moodle forum discovery failed for course %s: %s",
                    cid,
                    course_exc,
                )

    def _fetch_forum(forum: dict[str, Any]) -> list[dict[str, Any]]:
        cid = as_int(forum.get("course_id"), 0) or 0
        try:
            payload = client.get_forum_discussions(forum["forum_id"])
        except MoodleWriteAttemptError:
            raise
        except Exception as exc:
            logger.warning(
                "Moodle update fetch failed for forum %s: %s",
                forum.get("forum_id"),
                exc,
            )
            return []
        discussions = normalize_forum_discussions(
            payload if isinstance(payload, dict) else {},
            course_id=cid,
            forum_id=forum["forum_id"],
            forum_name=forum.get("name", ""),
            base_url=client.base_url,
        )
        recent: list[dict[str, Any]] = []
        for discussion in discussions:
            modified = discussion.get("modified_at") or discussion.get("created_at") or 0
            if modified >= cutoff:
                discussion = dict(discussion)
                discussion["course_shortname"] = course_names.get(cid, str(cid))
                recent.append(discussion)
        return recent

    updates: list[dict[str, Any]] = []
    for per_forum in run_parallel(
        forums,
        _fetch_forum,
        # Prefix the course short-code: most Moodle forums are all named
        # "Announcements", so the forum name alone tells the user nothing
        # about which course is being processed.
        label_fn=lambda f: (
            f"{course_names.get(as_int(f.get('course_id'), 0) or 0, '')}: "
            f"{clean_text(f.get('name')) or f.get('forum_id') or ''}"
        ).lstrip(": "),
        on_progress=on_progress,
    ):
        updates.extend(per_forum)
    updates.sort(key=lambda r: r["modified_at"] or r["created_at"] or 0, reverse=True)
    return updates
