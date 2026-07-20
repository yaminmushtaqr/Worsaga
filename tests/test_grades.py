"""Tests for grade normalization and retrieval."""

from unittest.mock import Mock

import pytest

from worsaga.grades import get_grade_summary, get_grades, normalize_grade_items


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
