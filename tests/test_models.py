"""Tests for normalized Worsaga record factories."""

from worsaga.models import (
    assignment_record,
    clean_text,
    course_module_file_record,
    course_module_record,
    course_record,
    course_section_record,
    forum_discussion_record,
    grade_record,
    notification_record,
)

# A fixed UTC epoch: 2023-11-14T22:13:20+00:00.
_TS = 1_700_000_000
_TS_ISO = "2023-11-14T22:13:20+00:00"


def test_course_record_is_stable_compact_shape():
    record = course_record({
        "id": "42",
        "shortname": "ECON101",
        "fullname": "Economics",
        "visible": 1,
        # Bulky fields that must be dropped.
        "summary": "<p style='x'>lots of HTML</p>",
        "enrolledusercount": 214,
        "overviewfiles": [{"fileurl": "https://m/pluginfile.php/x?token=SECRET"}],
        "category": 2,
        "startdate": 1_725_148_800,
        "enddate": 1_744_675_200,
    })
    assert record == {
        "id": 42,
        "shortname": "ECON101",
        "fullname": "Economics",
        "category": 2,
        "start_at": 1_725_148_800,
        "end_at": 1_744_675_200,
    }
    # No token-bearing course image or HTML summary survives.
    assert "SECRET" not in str(record)
    assert "summary" not in record


def test_course_record_missing_schedule_fields_are_none():
    record = course_record({"id": 5, "shortname": "CS210", "fullname": "AI"})
    assert record["category"] is None
    assert record["start_at"] is None
    assert record["end_at"] is None


def test_course_contents_records_are_compact_and_token_free():
    section = course_section_record(
        section_id=1103,
        section_num=3,
        section_name="Week 3 - Elasticity",
        summary="<div><p style='font-size:2em'>Elasticity &amp; revenue.</p></div>",
        modules=[
            course_module_record(
                module_id=5103,
                module_name="<b>Week 3 slides</b>",
                module_type="resource",
                view_url="https://m/mod/resource/view.php?id=5103",
                files=[
                    course_module_file_record(
                        file_name="w3.pdf",
                        file_size="2048",
                        mime_type="application/pdf",
                        time_modified="1700000000",
                        dedupe_key="5103:w3.pdf:/|/pluginfile.php/5103/w3.pdf",
                    ),
                ],
            ),
        ],
    )
    # Summary is plain text (HTML stripped, entities decoded).
    assert section["summary"] == "Elasticity & revenue."
    assert "<" not in section["summary"]
    assert section["section_name"] == "Week 3 - Elasticity"
    module = section["modules"][0]
    assert module["module_name"] == "Week 3 slides"
    file_record = module["files"][0]
    assert file_record["file_size"] == 2048
    assert file_record["time_modified"] == 1700000000
    # No raw file_url / token anywhere.
    assert "file_url" not in file_record
    assert "token" not in str(section).lower()


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


def test_grade_record_derives_iso_and_local_from_graded_at():
    record = grade_record(
        course_id=1,
        course_shortname="ECON101",
        item_id=7,
        item_name="Midterm",
        grade_display="85.0 %",
        graded=True,
        graded_at=_TS,
        status="graded",
    )
    # Epoch kept for machine use; ISO is deterministic UTC; local display
    # is non-empty (its exact value is timezone-dependent, like due_str).
    assert record["graded_at"] == _TS
    assert record["graded_iso"] == _TS_ISO
    assert record["graded_str"]


def test_grade_record_without_graded_at_has_empty_derived_fields():
    record = grade_record(
        course_id=1, course_shortname="ECON101", item_id=7, item_name="X",
    )
    assert record["graded_at"] is None
    assert record["graded_iso"] == ""
    assert record["graded_str"] == ""


def test_forum_discussion_record_derives_iso_and_local_timestamps():
    record = forum_discussion_record(
        course_id=1,
        forum_id=5,
        forum_name="Announcements",
        discussion_id=9,
        name="Week 3 moves rooms",
        created_at=_TS,
        modified_at=_TS,
    )
    # Epochs preserved for backward compat.
    assert record["created_at"] == _TS
    assert record["modified_at"] == _TS
    # ISO (UTC) is deterministic; local display strings are non-empty.
    assert record["created_iso"] == _TS_ISO
    assert record["modified_iso"] == _TS_ISO
    assert record["created_str"] and record["modified_str"]


def test_notification_record_derives_iso_and_local_created():
    record = notification_record(
        notification_id="n1",
        notification_type="notification",
        subject="Update",
        created_at=_TS,
    )
    assert record["created_at"] == _TS
    assert record["created_iso"] == _TS_ISO
    assert record["created_str"]


def test_notification_record_without_created_at_has_empty_derived_fields():
    record = notification_record(
        notification_id="n1", notification_type="message", subject="Hi",
    )
    assert record["created_at"] is None
    assert record["created_iso"] == ""
    assert record["created_str"] == ""


def test_clean_text_unescapes_and_collapses_whitespace():
    assert clean_text("A&nbsp; <b>B</b>\n C") == "A B C"
