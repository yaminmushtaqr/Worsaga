"""Tests for the shared course-identifier resolver.

The matching *semantics* (exact short-code, unambiguous prefix, ambiguity,
not-found) are also exercised via the CLI re-export in
``tests/test_cli.py::TestResolveCourseId``; these focus on the shared
module's own surface — int passthrough, the structured ambiguity error, and
resolving against a pre-fetched course list.
"""

from unittest.mock import MagicMock

import pytest

from worsaga.courses import (
    CourseAmbiguousError,
    CourseResolutionError,
    resolve_course_id,
)


def _client(courses):
    client = MagicMock()
    client.get_courses.return_value = courses
    return client


def test_integer_argument_resolves_when_enrolled():
    client = _client([{"id": 42, "shortname": "ECON101"}])
    assert resolve_course_id(client, 42) == 42


def test_digit_string_resolves_when_enrolled():
    client = _client([{"id": 42, "shortname": "ECON101"}])
    assert resolve_course_id(client, "42") == 42


def test_numeric_id_outside_enrolment_is_refused():
    # A numeric id used to be trusted verbatim and handed straight to
    # Moodle; it is now checked against the enrolment list first.
    client = _client([{"id": 10, "shortname": "ECON101"}])
    with pytest.raises(CourseResolutionError, match="not enrolled"):
        resolve_course_id(client, 999999)
    with pytest.raises(CourseResolutionError, match="not enrolled"):
        resolve_course_id(client, "999999")


def test_numeric_id_checked_against_prefetched_courses_without_fetch():
    client = _client([])
    courses = [{"id": 42, "shortname": "ECON101"}]
    assert resolve_course_id(client, 42, courses=courses) == 42
    client.get_courses.assert_not_called()


def test_exact_shortcode_is_case_insensitive():
    client = _client([{"id": 10, "shortname": "ECON101", "fullname": "Economics"}])
    assert resolve_course_id(client, "econ101") == 10


def test_ambiguous_prefix_raises_with_candidate_dicts():
    courses = [
        {"id": 30, "shortname": "CS210_2526", "fullname": "AI (2025/26)"},
        {"id": 31, "shortname": "CS210_2425", "fullname": "AI (2024/25)"},
    ]
    with pytest.raises(CourseAmbiguousError) as exc_info:
        resolve_course_id(_client(courses), "CS210")

    exc = exc_info.value
    # It is also a CourseResolutionError, so "could not resolve" callers work.
    assert isinstance(exc, CourseResolutionError)
    assert exc.query == "CS210"
    assert {c["id"] for c in exc.candidates} == {30, 31}
    assert "ambiguous" in str(exc)
    assert "CS210_2526" in str(exc) and "CS210_2425" in str(exc)


def test_unknown_name_raises_resolution_error_with_query():
    with pytest.raises(CourseResolutionError, match="no enrolled course"):
        resolve_course_id(_client([{"id": 1, "shortname": "ECON101"}]), "NOPE")


def test_accepts_prefetched_courses_without_calling_client():
    client = _client([])  # would return [] if fetched
    courses = [{"id": 7, "shortname": "PSY110", "fullname": "Psychology"}]
    assert resolve_course_id(client, "PSY110", courses=courses) == 7
    client.get_courses.assert_not_called()
