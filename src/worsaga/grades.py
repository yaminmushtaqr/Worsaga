"""Grade retrieval and normalization."""

from __future__ import annotations

from collections import Counter
from typing import Any

from worsaga.client import MoodleClient, MoodleWriteAttemptError
from worsaga.models import as_bool, as_float, as_int, clean_text, grade_record


def _parse_percentage(item: dict[str, Any]) -> float | None:
    for key in ("percentageformatted", "percentage", "percent"):
        parsed = as_float(item.get(key))
        if parsed is not None:
            return parsed
    return None


def _parse_weight(item: dict[str, Any]) -> float | None:
    for key in ("weightformatted", "weight", "aggregationcoef2", "aggregationcoef"):
        parsed = as_float(item.get(key))
        if parsed is not None:
            return parsed
    return None


def _range_text(item: dict[str, Any]) -> str:
    direct = item.get("rangeformatted") or item.get("range")
    if direct:
        return clean_text(direct)
    minimum = item.get("grademin")
    maximum = item.get("grademax")
    if minimum not in (None, "") or maximum not in (None, ""):
        return f"{minimum or 0}-{maximum or ''}"
    return ""


def _grade_display(item: dict[str, Any]) -> str:
    for key in ("gradeformatted", "grade_display", "grade", "graderaw"):
        value = item.get(key)
        if value not in (None, ""):
            return clean_text(value)
    return ""


def _grade_raw(item: dict[str, Any]) -> str:
    return clean_text(item.get("graderaw", item.get("grade", "")))


def _derive_status(
    item: dict[str, Any],
    grade_display: str,
    hidden: bool | None,
) -> tuple[str, bool]:
    excluded = as_bool(item.get("excluded"), False)
    contributes = as_bool(item.get("contributes_to_total"), None)
    if contributes is False:
        excluded = True

    lowered = grade_display.strip().lower()
    if hidden is True or lowered in {"hidden", "not released", "unreleased"}:
        return "unreleased", False
    if excluded:
        return "excluded", bool(grade_display)
    if lowered in {"-", "not graded", "not submitted"}:
        return "missing", False
    if not grade_display:
        return "unknown", False
    return "graded", True


def normalize_grade_items(
    payload: dict[str, Any],
    *,
    course_id: int,
    course_shortname: str = "",
) -> list[dict[str, Any]]:
    """Normalize Moodle grade report payloads into Worsaga grade records."""
    records: list[dict[str, Any]] = []
    usergrades = payload.get("usergrades", [])
    if not isinstance(usergrades, list):
        return records

    for usergrade in usergrades:
        items = usergrade.get("gradeitems", []) if isinstance(usergrade, dict) else []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            item_name = (
                item.get("itemname")
                or item.get("name")
                or ("Course total" if item.get("itemtype") == "course" else "")
                or item.get("itemmodule")
                or item.get("itemtype")
                or "Grade item"
            )
            grade_display = _grade_display(item)
            hidden = as_bool(
                item.get("hidden", item.get("gradeishidden", item.get("ishidden"))),
                None,
            )
            status, graded = _derive_status(item, grade_display, hidden)
            contributes = as_bool(item.get("contributes_to_total"), None)
            if contributes is None and item.get("excluded") not in (None, ""):
                contributes = not bool(as_bool(item.get("excluded"), False))

            records.append(
                grade_record(
                    course_id=course_id,
                    course_shortname=course_shortname,
                    item_id=as_int(item.get("id", item.get("itemid"))),
                    item_name=str(item_name),
                    category=str(
                        item.get("categoryname")
                        or item.get("itemmodule")
                        or item.get("itemtype")
                        or ""
                    ),
                    grade_raw=_grade_raw(item),
                    grade_display=grade_display,
                    percentage=_parse_percentage(item),
                    weight=_parse_weight(item),
                    range_text=_range_text(item),
                    feedback=item.get("feedback") or item.get("feedbackformatted") or "",
                    graded=graded,
                    hidden=hidden,
                    contributes_to_total=contributes,
                    status=status,
                )
            )
    return records


def _course_targets(client: MoodleClient, course_id: int | None) -> list[dict[str, Any]]:
    courses = client.get_courses()
    if course_id is None:
        return courses
    for course in courses:
        if as_int(course.get("id")) == course_id:
            return [course]
    return [{"id": course_id, "shortname": str(course_id), "fullname": ""}]


def collect_grades(
    client: MoodleClient,
    course_id: int | None = None,
) -> dict[str, Any]:
    """Return normalized grade records plus non-fatal warnings."""
    records: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for course in _course_targets(client, course_id):
        cid = as_int(course.get("id"), 0) or 0
        shortname = str(course.get("shortname") or cid)
        try:
            payload = client.get_user_grade_items(cid)
        except MoodleWriteAttemptError:
            raise
        except Exception as exc:
            if course_id is not None:
                raise
            warnings.append({
                "course_id": cid,
                "course_shortname": shortname,
                "message": str(exc),
            })
            continue
        records.extend(
            normalize_grade_items(
                payload if isinstance(payload, dict) else {},
                course_id=cid,
                course_shortname=shortname,
            )
        )
    records.sort(key=lambda r: (r["course_shortname"], r["item_name"], r["item_id"] or 0))
    return {"grades": records, "warnings": warnings}


def get_grades(client: MoodleClient, course_id: int | None = None) -> list[dict[str, Any]]:
    """Return normalized grade records for one course or all enrolled courses."""
    return collect_grades(client, course_id=course_id)["grades"]


def get_grade_summary(client: MoodleClient, course_id: int | None = None) -> dict[str, Any]:
    """Return aggregate grade status counts and course total entries."""
    result = collect_grades(client, course_id=course_id)
    records = result["grades"]
    counts = Counter(record.get("status", "unknown") for record in records)
    totals = [
        record
        for record in records
        if record.get("item_name", "").strip().lower() in {"course total", "total"}
        or record.get("category", "").strip().lower() == "course"
    ]
    return {
        "course_id": course_id,
        "total_items": len(records),
        "status_counts": dict(sorted(counts.items())),
        "course_totals": totals,
        "warnings": result["warnings"],
    }
