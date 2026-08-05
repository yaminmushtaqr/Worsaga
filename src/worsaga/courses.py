"""Shared course-identifier resolution for the CLI and MCP surfaces.

A course argument may be a raw integer id, a digit-string, an exact
short-code (case-insensitive), or an unambiguous short-code prefix. Both
the CLI (``worsaga grades ECON101``) and the MCP tools
(``get_grades("ECON101")``) resolve it through :func:`resolve_course_id`
so the matching rules stay identical across surfaces.

The two failure modes are raised as exceptions so each surface can present
them in its own idiom — the CLI as a human ``Error:`` line, the MCP tools
as a structured ``{"error", "error_code"}`` dict:

- :class:`CourseResolutionError` — no enrolled course matched the query.
- :class:`CourseAmbiguousError` — a prefix matched more than one course
  (a subclass of :class:`CourseResolutionError`, carrying the matched
  course dicts as ``candidates`` for a structured candidate list).
"""

from __future__ import annotations

from typing import Any


class CourseResolutionError(ValueError):
    """Raised when a course identifier cannot be resolved to one course."""


class CourseAmbiguousError(CourseResolutionError):
    """Raised when a non-numeric course query matches more than one course.

    Subclasses :class:`CourseResolutionError` so callers that only care
    about "could not resolve" keep working, while callers that want to
    offer the alternatives can read :attr:`candidates` (the matched raw
    course dicts) and :attr:`query` (the original argument).
    """

    def __init__(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        message: str | None = None,
    ):
        self.query = query
        self.candidates = candidates
        if message is None:
            shortnames = ", ".join(
                sorted(str(c.get("shortname", "?")) for c in candidates)
            )
            message = f"'{query}' is ambiguous — matches: {shortnames}"
        super().__init__(message)


def _prefix_base(shortname: str) -> str:
    """Return the code stem before the first ``_`` or ``-`` separator.

    ``ECON101_2526`` and ``PSY110-2526`` both stem to their bare course
    code, so a bare code resolves the current-year offering.
    """
    for sep in ("_", "-"):
        if sep in shortname:
            return shortname.split(sep, 1)[0]
    return shortname


def _require_enrolled(
    client: Any,
    course_id: int,
    courses: list[dict[str, Any]] | None,
) -> int:
    """Return *course_id* only when it is one of the enrolled courses.

    A numeric id used to be trusted verbatim, so any id a caller supplied
    reached Moodle — including courses this account is not in. The enrolled
    set is memoised on the client, so a flow that already listed courses
    (or passed *courses* in) pays nothing for the check.
    """
    if courses is None:
        courses = client.get_courses()
    for course in courses:
        try:
            if int(course.get("id")) == course_id:
                return course_id
        except (TypeError, ValueError):
            continue
    raise CourseResolutionError(
        f"Course {course_id} not found (not enrolled or does not exist)."
    )


def resolve_course_id(
    client: Any,
    raw: int | str,
    *,
    courses: list[dict[str, Any]] | None = None,
) -> int:
    """Resolve *raw* to a Moodle course id.

    Resolution order, mirrored exactly by the CLI and the MCP tools:

    1. An ``int`` (or all-digit string) resolves to itself, but only after
       it is confirmed to be one of the enrolled courses.
    2. A case-insensitive **exact** short-code match wins.
    3. Otherwise an **unambiguous prefix** match on the short-code stem
       (the part before the first ``_``/``-``): the query equals the stem,
       is one of the ``/``-separated codes in the stem, or is a prefix of
       the stem no more than two characters shorter.

    Pass *courses* to resolve against an already-fetched course list rather
    than calling ``client.get_courses()`` again.

    Raises :class:`CourseAmbiguousError` when a prefix matches more than one
    course, or :class:`CourseResolutionError` when nothing matches.
    """
    # A real integer id (bool is an int subclass but never a course id).
    if isinstance(raw, int) and not isinstance(raw, bool):
        return _require_enrolled(client, raw, courses)
    text = str(raw).strip()
    try:
        numeric = int(text)
    except ValueError:
        pass
    else:
        return _require_enrolled(client, numeric, courses)

    if courses is None:
        courses = client.get_courses()
    needle = text.lower()

    # 1. Exact short-code match (case-insensitive) always wins.
    for course in courses:
        if str(course.get("shortname", "")).lower() == needle:
            return course["id"]

    # 2. Unambiguous prefix match against the short-code stem.
    prefix_matches: list[dict[str, Any]] = []
    for course in courses:
        shortname = str(course.get("shortname", "")).lower()
        base = _prefix_base(shortname)
        if needle == base:
            prefix_matches.append(course)
        elif needle in base.split("/"):
            prefix_matches.append(course)
        elif base.startswith(needle) and len(base) - len(needle) <= 2:
            prefix_matches.append(course)

    seen: set[Any] = set()
    unique_matches: list[dict[str, Any]] = []
    for course in prefix_matches:
        if course["id"] not in seen:
            seen.add(course["id"])
            unique_matches.append(course)
    prefix_matches = unique_matches

    if len(prefix_matches) == 1:
        return prefix_matches[0]["id"]

    if len(prefix_matches) > 1:
        raise CourseAmbiguousError(text, prefix_matches)

    available = ", ".join(sorted(str(c.get("shortname", "?")) for c in courses))
    raise CourseResolutionError(
        f"no enrolled course matching '{text}'.\n"
        f"Available short-codes: {available}"
    )
