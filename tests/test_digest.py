"""Tests for live digest aggregation."""

from unittest.mock import patch

import pytest

from worsaga.demo import DemoMoodleClient
from worsaga.digest import get_digest


class FakeDigestClient:
    pass


class CountingClient(DemoMoodleClient):
    """Offline client that counts the reads each orchestrator makes."""

    def __init__(self):
        super().__init__()
        self.counts: dict[str, int] = {}

    def _count(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1

    def get_courses(self):
        self._count("get_courses")
        return super().get_courses()

    def get_assignments_by_courses(self, course_ids):
        self._count("get_assignments_by_courses")
        return super().get_assignments_by_courses(course_ids)

    def get_forums_by_courses(self, course_ids):
        self._count("get_forums_by_courses")
        return super().get_forums_by_courses(course_ids)

    def get_quizzes(self, course_ids=None):
        self._count("get_quizzes")
        return super().get_quizzes(course_ids)

    def get_user_grade_items(self, course_id):
        self._count("get_user_grade_items")
        return super().get_user_grade_items(course_id)


class TestRequestReduction:
    def test_digest_lists_the_enrolled_courses_once(self):
        client = CountingClient()
        get_digest(client, since_days=1)
        # Deadlines, assignments, and updates each used to discover the
        # course list for themselves: three identical requests for a list
        # that has to be the same across all five sources anyway.
        assert client.counts["get_courses"] == 1

    def test_digest_still_covers_every_source(self):
        client = CountingClient()
        digest = get_digest(client, since_days=30)
        assert digest["warnings"] == []
        assert digest["deadlines"]
        assert digest["assignments"]
        # The shared list did not narrow what any source could see.
        assert client.counts["get_assignments_by_courses"] >= 1
        assert client.counts["get_forums_by_courses"] >= 1

    def test_sync_lists_the_enrolled_courses_once(self, tmp_path):
        from worsaga.sync import run_sync

        client = CountingClient()
        result = run_sync(client, cache_path=tmp_path / "cache.db")
        assert result["outcome"] == "success"
        # Course discovery, grades, files, and forums shared one list.
        assert client.counts["get_courses"] == 1

    def test_a_failed_course_list_falls_back_per_source(self):
        class _NoCourses(DemoMoodleClient):
            def get_courses(self):
                raise RuntimeError("course list unavailable")

        digest = get_digest(_NoCourses(), since_days=1)
        # Each source reports its own failure exactly as it did before.
        assert digest["deadlines"] == []
        assert any("course list unavailable" in w for w in digest["warnings"])

    @pytest.mark.parametrize("source", [
        "get_upcoming_deadlines", "get_assignments", "get_latest_updates",
    ])
    def test_each_course_taking_source_accepts_a_shared_list(self, source):
        import inspect

        import worsaga.digest as digest_module

        signature = inspect.signature(getattr(digest_module, source))
        assert "courses" in signature.parameters


def test_digest_combines_sources_and_warnings():
    with patch("worsaga.digest.get_upcoming_deadlines", return_value=[{"name": "Due"}]), \
         patch("worsaga.digest.get_assignments", side_effect=RuntimeError("assignments denied")), \
         patch("worsaga.digest.get_latest_updates", return_value=[{"name": "Announcement"}]), \
         patch("worsaga.digest.get_notifications", return_value=[]), \
         patch("worsaga.digest.get_messages", return_value=[{"subject": "Hi"}]):
        digest = get_digest(FakeDigestClient(), since_days=1)

    assert digest["deadlines"] == [{"name": "Due"}]
    assert digest["assignments"] == []
    assert digest["updates"] == [{"name": "Announcement"}]
    assert digest["messages"] == [{"subject": "Hi"}]
    assert digest["warnings"] == ["assignments: assignments denied"]
