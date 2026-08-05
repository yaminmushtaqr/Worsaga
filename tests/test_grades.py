"""Tests for grade normalization and retrieval."""

import time
from unittest.mock import Mock

import pytest

from worsaga.client import CourseNotFoundError
from worsaga.grades import (
    collect_grades,
    get_grade_summary,
    get_grades,
    normalize_grade_items,
)


def _payload(*items):
    return {"usergrades": [{"userid": 1, "gradeitems": list(items)}]}


def test_normalize_grade_items_rich_payload():
    result = normalize_grade_items(
        _payload({
            "id": 10,
            "itemname": "<b>Midterm</b>",
            "itemmodule": "assign",
            "graderaw": "85",
            "gradeformatted": "85.00",
            "percentageformatted": "85.0 %",
            "weightformatted": "40.0 %",
            "rangeformatted": "0-100",
            "feedback": "<p>Good work</p>",
            "hidden": 0,
        }),
        course_id=1,
        course_shortname="ECON101",
    )
    assert len(result) == 1
    record = result[0]
    assert record["item_id"] == 10
    assert record["item_name"] == "Midterm"
    assert record["category"] == "assign"
    assert record["percentage"] == 85.0
    assert record["weight"] == 40.0
    assert record["feedback"] == "Good work"
    assert record["status"] == "graded"
    assert record["graded"] is True


def test_normalize_grade_items_captures_graded_at_and_derives_iso():
    result = normalize_grade_items(
        _payload({
            "id": 10, "itemname": "Midterm", "gradeformatted": "85.00",
            "gradedategraded": 1_700_000_000,
        }),
        course_id=1,
        course_shortname="ECON101",
    )
    record = result[0]
    assert record["graded_at"] == 1_700_000_000
    assert record["graded_iso"] == "2023-11-14T22:13:20+00:00"
    assert record["graded_str"]


def test_normalize_grade_items_no_grade_date_leaves_derived_empty():
    result = normalize_grade_items(
        _payload({"id": 10, "itemname": "Essay", "gradeformatted": "-"}),
        course_id=1,
        course_shortname="ECON101",
    )
    assert result[0]["graded_at"] is None
    assert result[0]["graded_iso"] == ""


def test_normalize_grade_items_statuses():
    result = normalize_grade_items(
        _payload(
            {"id": 1, "itemname": "Hidden", "gradeformatted": "Hidden", "hidden": 1},
            {"id": 2, "itemname": "Missing", "gradeformatted": "-"},
            {"id": 3, "itemname": "Excluded", "gradeformatted": "70", "excluded": 1},
        ),
        course_id=1,
        course_shortname="ECON101",
    )
    statuses = {record["item_name"]: record["status"] for record in result}
    assert statuses == {
        "Hidden": "unreleased",
        "Missing": "missing",
        "Excluded": "excluded",
    }


def test_normalize_grade_items_blank_placeholders_are_unknown():
    result = normalize_grade_items(
        _payload({"id": 1, "itemname": "Submission link", "rangeformatted": "0-100"}),
        course_id=1,
        course_shortname="ECON101",
    )
    assert result[0]["status"] == "unknown"
    assert result[0]["graded"] is False


def test_normalize_grade_items_names_course_total():
    result = normalize_grade_items(
        _payload({"id": 1, "itemname": None, "itemtype": "course"}),
        course_id=1,
        course_shortname="ECON101",
    )
    assert result[0]["item_name"] == "Course total"


def test_normalize_grade_items_empty_and_bad_shapes():
    assert normalize_grade_items({}, course_id=1) == []
    assert normalize_grade_items({"usergrades": {}}, course_id=1) == []


class FakeGradeClient:
    def __init__(self):
        self.courses = [
            {"id": 2, "shortname": "PSY110"},
            {"id": 1, "shortname": "ECON101"},
        ]
        self.payloads = {
            1: _payload({"id": 2, "itemname": "B", "gradeformatted": "60"}),
            2: _payload({"id": 1, "itemname": "A", "gradeformatted": "70"}),
        }

    def get_courses(self):
        return self.courses

    def get_user_grade_items(self, course_id):
        return self.payloads[course_id]


def test_non_enrolled_course_is_not_fabricated_into_a_target():
    """An unknown id used to become a synthetic ``{"id", "shortname"}`` target.

    That sent the unknown id to the gradebook endpoint and labelled whatever
    came back with the bare number.
    """
    client = FakeGradeClient()
    client.get_user_grade_items = Mock(side_effect=AssertionError("must not fetch"))
    with pytest.raises(CourseNotFoundError) as exc_info:
        collect_grades(client, course_id=999999)
    assert exc_info.value.course_id == 999999


def test_get_grades_across_courses_is_deterministic():
    records = get_grades(FakeGradeClient())
    assert [record["course_shortname"] for record in records] == ["ECON101", "PSY110"]
    assert [record["item_name"] for record in records] == ["B", "A"]


def test_get_grades_one_course_uses_course_shortname():
    records = get_grades(FakeGradeClient(), course_id=2)
    assert len(records) == 1
    assert records[0]["course_shortname"] == "PSY110"


def test_get_grades_one_course_reraises_fetch_error():
    client = FakeGradeClient()
    client.get_user_grade_items = Mock(side_effect=RuntimeError("denied"))
    with pytest.raises(RuntimeError, match="denied"):
        get_grades(client, course_id=1)


class _DelayedGradeClient:
    """Fake whose per-course fetch has staggered latency and one failure.

    Course fetch latency is reverse-proportional to input order, so the
    concurrent fan-out completes courses in roughly the opposite order to
    the input — exercising order preservation and progress correctness
    under real threading.
    """

    def __init__(self):
        # Five courses; CC303 (id 3) is not gradable for this account.
        self.courses = [
            {"id": 1, "shortname": "AA101"},
            {"id": 2, "shortname": "BB202"},
            {"id": 3, "shortname": "CC303"},
            {"id": 4, "shortname": "DD404"},
            {"id": 5, "shortname": "EE505"},
        ]

    def get_courses(self):
        return self.courses

    def get_user_grade_items(self, course_id):
        # Later courses answer sooner, so completion order != input order.
        time.sleep(0.02 * (6 - course_id))
        if course_id == 3:
            raise RuntimeError("Cannot view grades")
        return {"usergrades": [{"gradeitems": [
            {"id": course_id, "itemname": f"Item {course_id}",
             "gradeformatted": "70.00", "grademin": 0, "grademax": 100},
        ]}]}


def test_collect_grades_parallel_order_warning_attribution_and_progress():
    client = _DelayedGradeClient()
    seen = []
    result = collect_grades(
        client, on_progress=lambda d, t, lbl: seen.append((d, t, lbl)),
    )

    # Results are reassembled deterministically (sorted by shortname) despite
    # out-of-order completion; the failed course contributes no records.
    assert [r["course_shortname"] for r in result["grades"]] == [
        "AA101", "BB202", "DD404", "EE505",
    ]

    # The permission failure is a non-fatal warning attributed to its own
    # course — not smeared onto whichever course happened to finish first.
    assert len(result["warnings"]) == 1
    warning = result["warnings"][0]
    assert warning["course_id"] == 3
    assert warning["course_shortname"] == "CC303"
    assert "Cannot view grades" in warning["message"]

    # Progress counts every course exactly once, monotonically, with the
    # right total, regardless of completion order.
    assert [done for done, _, _ in seen] == [1, 2, 3, 4, 5]
    assert {total for _, total, _ in seen} == {5}
    assert sorted(label for _, _, label in seen) == [
        "AA101", "BB202", "CC303", "DD404", "EE505",
    ]


def test_grade_summary_counts_statuses_and_totals():
    client = FakeGradeClient()
    client.payloads[1] = _payload(
        {"id": 1, "itemname": "Course total", "itemtype": "course", "gradeformatted": "65"},
        {"id": 2, "itemname": "Essay", "gradeformatted": "-"},
    )
    summary = get_grade_summary(client, course_id=1)
    assert summary["course_id"] == 1
    assert summary["total_items"] == 2
    assert summary["status_counts"] == {"graded": 1, "missing": 1}
    assert summary["course_totals"][0]["item_name"] == "Course total"
    assert summary["warnings"] == []
