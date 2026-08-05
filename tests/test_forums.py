"""Tests for forums and updates."""

from unittest.mock import Mock, patch

import pytest

from worsaga.client import CourseNotFoundError, ForumNotFoundError
from worsaga.forums import (
    get_forum_discussions,
    get_latest_updates,
    is_announcement_forum,
    normalize_forum_discussions,
    normalize_forums,
)


def test_is_announcement_forum_matches_common_names():
    assert is_announcement_forum("Announcements")
    assert is_announcement_forum("News Forum")
    assert not is_announcement_forum("Seminar chat")


def test_normalize_forums_sorts_announcements_first():
    result = normalize_forums(
        {
            "forums": [
                {"id": 2, "course": 1, "name": "Seminar", "type": "general"},
                {"id": 1, "course": 1, "name": "Announcements", "type": "news"},
            ],
        },
        course_id=1,
    )
    assert [forum["forum_id"] for forum in result] == [1, 2]
    assert result[0]["is_announcement"] is True


def test_normalize_forums_accepts_raw_moodle_list_payload():
    result = normalize_forums(
        [{"id": 1, "course": 1, "name": "Announcements"}],
        course_id=1,
    )
    assert len(result) == 1
    assert result[0]["forum_id"] == 1


def test_normalize_forum_discussions_sorts_latest_first():
    result = normalize_forum_discussions(
        {
            "discussions": [
                {"discussion": 1, "name": "Old", "timemodified": 10},
                {"discussion": 2, "name": "New", "timemodified": 20, "numunread": 3},
            ],
        },
        course_id=1,
        forum_id=5,
        forum_name="Announcements",
        base_url="https://moodle.example.com",
    )
    assert [row["discussion_id"] for row in result] == [2, 1]
    assert result[0]["unread_count"] == 3
    assert result[0]["view_url"].endswith("discuss.php?d=2")


class FakeForumClient:
    base_url = "https://moodle.example.com"

    def __init__(self):
        self.courses = [{"id": 1, "shortname": "ECON101"}]

    def get_courses(self):
        return self.courses

    def get_forums_by_courses(self, course_ids):
        return {"forums": [{"id": 5, "course": 1, "name": "Announcements"}]}

    def get_forum_discussions(self, forum_id):
        return {"discussions": [{"discussion": 9, "name": "Update", "timemodified": 1000}]}


def test_get_forum_discussions_degrades_on_forum_failure():
    client = FakeForumClient()
    client.get_forum_discussions = Mock(side_effect=RuntimeError("denied"))
    assert get_forum_discussions(client, 1) == []


def test_forum_outside_the_course_is_not_fabricated():
    """An unknown forum id used to become a placeholder forum record.

    The fabricated entry carried the caller's id straight through to
    ``mod_forum_get_forum_discussions``; the course's forum list is already
    in hand, so the id is now checked against it first.
    """
    client = FakeForumClient()
    client.get_forum_discussions = Mock(side_effect=AssertionError("must not fetch"))
    with pytest.raises(ForumNotFoundError) as exc_info:
        get_forum_discussions(client, 1, forum_id=6666)
    assert exc_info.value.forum_id == 6666
    assert exc_info.value.course_id == 1


def test_latest_updates_refuses_a_non_enrolled_course():
    client = FakeForumClient()
    client.get_forums_by_courses = Mock(side_effect=AssertionError("must not fetch"))
    with pytest.raises(CourseNotFoundError):
        get_latest_updates(client, course_id=999999)


def test_get_latest_updates_filters_by_since_days():
    with patch("time.time", return_value=1000 + 86400):
        updates = get_latest_updates(FakeForumClient(), since_days=1)
    assert len(updates) == 1
    assert updates[0]["course_shortname"] == "ECON101"


def test_get_latest_updates_batches_forum_discovery():
    client = FakeForumClient()
    client.courses = [{"id": 1, "shortname": "ECON101"}, {"id": 2, "shortname": "STAT120"}]
    seen = []

    def _forums(course_ids):
        seen.append(course_ids)
        return {
            "forums": [
                {"id": 5, "course": 1, "name": "Announcements"},
                {"id": 6, "course": 2, "name": "Announcements"},
            ],
        }

    client.get_forums_by_courses = _forums
    with patch("time.time", return_value=1000 + 86400):
        updates = get_latest_updates(client, since_days=1)
    assert seen == [[1, 2]]
    assert len(updates) == 2
