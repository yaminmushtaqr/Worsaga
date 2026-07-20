"""Tests for normalized Worsaga record factories."""

from worsaga.models import (
    assignment_record,
    clean_text,
    course_record,
    grade_record,
    notification_record,
)


def test_course_record_is_stable_minimal_shape():
    record = course_record({
        "id": "42",
        "shortname": "ECON101",
        "fullname": "Economics",
        "visible": 1,
    })
    assert record == {
        "id": 42,
        "shortname": "ECON101",
        "fullname": "Economics",
    }


def test_assignment_record_contains_expected_keys():
    record = assignment_record(
        course_id=1,
        course_shortname="ECON101",
        assignment={
            "id": "10",
            "cmid": "99",
            "name": "<b>Essay</b>",
            "duedate": "1700000000",
        },
        submitted=False,
        submission_status="new",
        overdue=True,
    )
    assert record["id"] == 10
    assert record["cmid"] == 99
    assert record["name"] == "Essay"
    assert record["submitted"] is False
    assert record["overdue"] is True
    assert "view_url" in record


def test_grade_record_cleans_html_and_keeps_status():
    record = grade_record(
        course_id=1,
        course_shortname="ECON101",
        item_id=7,
        item_name="<span>Midterm</span>",
        grade_display="85.0 %",
        graded=True,
        status="graded",
    )
    assert record["item_name"] == "Midterm"
    assert record["grade_display"] == "85.0 %"
    assert record["graded"] is True
    assert record["status"] == "graded"


def test_notification_preview_is_compact_plain_text():
    body = "<p>" + ("word " * 80) + "</p>"
    record = notification_record(
        notification_id="n1",
        notification_type="notification",
        subject="Update",
        body=body,
    )
    assert record["subject"] == "Update"
    assert "<p>" not in record["body_preview"]
    assert record["body_preview"].endswith("...")


def test_clean_text_unescapes_and_collapses_whitespace():
    assert clean_text("A&nbsp; <b>B</b>\n C") == "A B C"
