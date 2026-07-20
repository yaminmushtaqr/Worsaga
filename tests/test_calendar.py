"""Tests for calendar normalization."""

from worsaga.calendar import (
    filter_calendar_events_by_week,
    get_calendar_events,
    normalize_calendar_events,
)


def test_normalize_calendar_events_shape_and_sort():
    result = normalize_calendar_events({
        "events": [
            {"id": 2, "name": "Later", "timestart": 20, "courseid": 1},
            {"id": 1, "name": "<b>Soon</b>", "timestart": 10, "eventtype": "due"},
        ],
    })
    assert [row["id"] for row in result] == [1, 2]
    assert result[0]["name"] == "Soon"
    assert result[0]["source"] == "calendar"
    assert "start_iso" in result[0]


def test_filter_calendar_events_by_week_matches_explicit_week_text():
    events = normalize_calendar_events({
        "events": [
            {"id": 1, "name": "Week 3 quiz", "timestart": 10},
            {"id": 2, "name": "Week 4 quiz", "timestart": 20},
            {"id": 3, "name": "Essay deadline", "timestart": 30},
        ],
    })

    result = filter_calendar_events_by_week(events, "3")

    assert [row["id"] for row in result] == [1]


def test_filter_calendar_events_by_week_uses_matching_section_module_names():
    events = normalize_calendar_events({
        "events": [
            {"id": 1, "name": "Problem set due", "timestart": 10},
            {"id": 2, "name": "Unrelated seminar", "timestart": 20},
        ],
    })
    sections = [
        {
            "name": "Week 3 - Regression",
            "modules": [{"name": "Problem set due"}],
        },
    ]

    result = filter_calendar_events_by_week(events, "3", sections=sections)

    assert [row["id"] for row in result] == [1]


def test_get_calendar_events_filters_by_week_with_course_sections():
    class FakeClient:
        def __init__(self):
            self.course_ids = None
            self.contents_course_id = None

        def get_calendar_events(self, course_ids=None, timestart=None, timeend=None):
            self.course_ids = course_ids
            return {
                "events": [
                    {"id": 1, "name": "Week 3 quiz", "timestart": 10},
                    {"id": 2, "name": "Week 4 quiz", "timestart": 20},
                ],
            }

        def get_course_contents(self, course_id):
            self.contents_course_id = course_id
            return [{"name": "Week 3", "modules": []}]

    client = FakeClient()

    result = get_calendar_events(client, course_id=42, days=7, week="3")

    assert client.course_ids == [42]
    assert client.contents_course_id == 42
    assert [row["id"] for row in result] == [1]
