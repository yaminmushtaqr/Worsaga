"""Tests for assignment status normalization and retrieval."""

from unittest.mock import Mock

import pytest

from worsaga.assignments import (
    get_assignment_status,
    get_assignments,
    normalize_assignment,
    normalize_assignments,
)


NOW = 1_700_000_000


def _assignment(**overrides):
    data = {
        "id": 10,
        "cmid": 99,
        "name": "Essay",
        "duedate": NOW + 86400,
        "cutoffdate": 0,
        "allowsubmissionsfromdate": 0,
    }
    data.update(overrides)
    return data


def test_normalize_assignment_submitted_status():
    record = normalize_assignment(
        _assignment(),
        course_id=1,
        course_shortname="ECON101",
        base_url="https://moodle.example.com",
        status_payload={
            "lastattempt": {"submission": {"status": "submitted"}},
            "feedback": {"grade": {"grade": "70"}},
        },
        now=NOW,
    )
    assert record["submitted"] is True
    assert record["submission_status"] == "submitted"
    assert record["graded"] is True
    assert record["feedback_available"] is True
    assert record["status"] == "graded"
    assert record["days_left"] == 1
    assert record["view_url"].endswith("/mod/assign/view.php?id=99")


def test_normalize_assignment_overdue_unsubmitted_is_missing():
    record = normalize_assignment(
        _assignment(duedate=NOW - 1),
        course_id=1,
        course_shortname="ECON101",
        status_payload={"lastattempt": {"submission": {"status": "new"}}},
        now=NOW,
    )
    assert record["submitted"] is False
    assert record["overdue"] is True
    assert record["status"] == "missing"


def test_normalize_assignment_future_unsubmitted_is_not_submitted():
    record = normalize_assignment(
        _assignment(duedate=NOW + 86400),
        course_id=1,
        course_shortname="ECON101",
        status_payload={"lastattempt": {"submission": {"status": "new"}}},
        now=NOW,
    )
    assert record["submitted"] is False
    assert record["overdue"] is False
    assert record["status"] == "not_submitted"


def test_normalize_assignment_no_due_date_is_not_overdue():
    record = normalize_assignment(
        _assignment(duedate=0),
        course_id=1,
        course_shortname="ECON101",
        status_payload={},
        now=NOW,
    )
    assert record["due_at"] is None
    assert record["days_left"] is None
    assert record["overdue"] is False


def test_normalize_assignments_handles_missing_keys():
    records = normalize_assignments(
        {"courses": [{"id": 1, "assignments": [{"id": 1, "name": "A"}]}]},
        course_map={1: "ECON101"},
        now=NOW,
    )
    assert len(records) == 1
    assert records[0]["name"] == "A"
    assert records[0]["course_shortname"] == "ECON101"


class FakeAssignmentClient:
    base_url = "https://moodle.example.com"

    def __init__(self):
        self.courses = [{"id": 1, "shortname": "ECON101"}]
        self.payload = {"courses": [{"id": 1, "assignments": [_assignment()]}]}
        self.statuses = {10: {"lastattempt": {"submission": {"status": "submitted"}}}}

    def get_courses(self):
        return self.courses

    def get_assignments_by_courses(self, course_ids):
        assert course_ids == [1]
        return self.payload

    def get_assignment_submission_status(self, assignment_id):
        return self.statuses[assignment_id]

    def get_assignment_grades(self, assignment_ids):
        return {"assignments": []}


def test_get_assignments_fetches_statuses():
    records = get_assignments(FakeAssignmentClient(), course_id=1, now=NOW)
    assert len(records) == 1
    assert records[0]["submitted"] is True


def test_get_assignments_degrades_when_status_missing():
    client = FakeAssignmentClient()
    client.get_assignment_submission_status = Mock(side_effect=RuntimeError("no permission"))
    records = get_assignments(client, course_id=1, now=NOW)
    assert len(records) == 1
    assert records[0]["submitted"] is None


def test_get_assignment_status_returns_one_record():
    record = get_assignment_status(FakeAssignmentClient(), course_id=1, assignment_id=10)
    assert record["id"] == 10


def test_get_assignment_status_raises_for_missing_assignment():
    with pytest.raises(ValueError, match="No assignment"):
        get_assignment_status(FakeAssignmentClient(), course_id=1, assignment_id=99)
