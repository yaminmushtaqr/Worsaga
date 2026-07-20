"""Tests for metadata sync and change detection."""

import json
import time
from pathlib import Path

import pytest

from worsaga.client import MoodleWriteAttemptError
from worsaga.demo import DemoMoodleClient
from worsaga.sync import (
    SYNC_CATEGORIES,
    collect_snapshots,
    get_recent_changes,
    run_sync,
)

NOW = int(time.time())
FUTURE = NOW + 5 * 86400
RECENT = NOW - 2 * 86400


class _FakeClient:
    """Mutable fake Moodle client covering every synced category."""

    base_url = "https://moodle.example.com"

    def __init__(self):
        self.courses = [
            {"id": 201, "shortname": "TEST101", "fullname": "Testing 101"},
        ]
        self.assignments = {"courses": [
            {"id": 201, "shortname": "TEST101", "assignments": [
                {"id": 1, "name": "Problem Set 1", "duedate": FUTURE},
            ]},
        ]}
        self.quizzes = {"quizzes": []}
        self.contents = [
            {"id": 11, "section": 1, "name": "Week 1", "modules": [
                {"id": 10, "name": "Week 1 slides", "modname": "resource",
                 "contents": [{
                     "type": "file", "filename": "week1.pdf", "filepath": "/",
                     "fileurl": f"{self.base_url}/webservice/pluginfile.php/10/week1.pdf",
                     "filesize": 100, "mimetype": "application/pdf",
                     "timemodified": RECENT,
                 }]},
            ]},
        ]
        self.grades = {"usergrades": [{"gradeitems": [
            {"id": 9, "itemname": "Problem Set 1", "gradeformatted": "70.00",
             "percentageformatted": "70.00 %", "grademin": 0, "grademax": 100},
            {"id": 8, "itemname": "Problem Set 2", "gradeformatted": "-",
             "grademin": 0, "grademax": 100},
        ]}]}
        self.forums = {"forums": [
            {"id": 6, "course": 201, "name": "Announcements", "type": "news",
             "numdiscussions": 1},
        ]}
        self.discussions = {6: [
            {"discussion": 301, "name": "Welcome", "userfullname": "Dr Fake",
             "created": RECENT, "timemodified": RECENT, "numunread": 0},
        ]}
        self.fail_forums = False

    def get_courses(self):
        return self.courses

    def get_assignments_by_courses(self, course_ids):
        return self.assignments

    def get_quizzes(self, course_ids=None):
        return self.quizzes

    def get_course_contents(self, course_id):
        return self.contents

    def get_user_grade_items(self, course_id, user_id=None):
        return self.grades

    def get_forums_by_courses(self, course_ids):
        if self.fail_forums:
            raise RuntimeError("forums are down")
        return self.forums

    def get_forum_discussions(self, forum_id):
        if self.fail_forums:
            raise RuntimeError("forums are down")
        return {"discussions": self.discussions.get(forum_id, [])}


@pytest.fixture
def cache_path(tmp_path):
    return tmp_path / "cache.db"


class TestRunSync:
    def test_first_sync_is_baseline_with_no_changes(self, cache_path):
        result = run_sync(_FakeClient(), cache_path=cache_path)
        assert result["changes"] == []
        assert result["warnings"] == []
        assert set(result["categories"]) == set(SYNC_CATEGORIES)
        for stats in result["categories"].values():
            assert stats["synced"] is True
            assert stats["baseline"] is True
        assert result["categories"]["deadlines"]["items"] == 1
        assert result["categories"]["files"]["items"] == 1
        assert result["categories"]["grades"]["items"] == 2
        assert result["categories"]["forums"]["items"] == 1

    def test_resync_without_changes_is_quiet(self, cache_path):
        client = _FakeClient()
        run_sync(client, cache_path=cache_path)
        result = run_sync(client, cache_path=cache_path)
        assert result["changes"] == []
        for stats in result["categories"].values():
            assert stats["baseline"] is False
            assert stats["new"] == 0
            assert stats["updated"] == 0

    def test_detects_every_change_kind(self, cache_path):
        client = _FakeClient()
        run_sync(client, cache_path=cache_path)

        # New deadline + moved deadline.
        client.assignments["courses"][0]["assignments"].append(
            {"id": 2, "name": "Problem Set 2", "duedate": FUTURE + 86400},
        )
        client.assignments["courses"][0]["assignments"][0]["duedate"] = FUTURE + 3600
        # New file + updated file.
        client.contents[0]["modules"][0]["contents"][0]["timemodified"] = RECENT + 60
        client.contents[0]["modules"].append(
            {"id": 12, "name": "Week 1 notes", "modname": "resource",
             "contents": [{
                 "type": "file", "filename": "notes.pdf", "filepath": "/",
                 "fileurl": f"{client.base_url}/webservice/pluginfile.php/12/notes.pdf",
                 "filesize": 50, "mimetype": "application/pdf",
                 "timemodified": RECENT,
             }]},
        )
        # Grade released for a previously ungraded item.
        client.grades["usergrades"][0]["gradeitems"][1]["gradeformatted"] = "88.00"
        # New discussion + updated discussion.
        client.discussions[6][0]["timemodified"] = RECENT + 120
        client.discussions[6].append(
            {"discussion": 302, "name": "Week 2 preview", "userfullname": "Dr Fake",
             "created": NOW, "timemodified": NOW, "numunread": 1},
        )

        result = run_sync(client, cache_path=cache_path)
        kinds = sorted(change["kind"] for change in result["changes"])
        assert kinds == [
            "deadline_changed",
            "file_updated",
            "forum_discussion_updated",
            "grade_updated",
            "new_deadline",
            "new_file",
            "new_forum_discussion",
        ]

        moved = next(
            c for c in result["changes"] if c["kind"] == "deadline_changed"
        )
        assert moved["before"]["due_ts"] == FUTURE
        assert moved["after"]["due_ts"] == FUTURE + 3600
        assert moved["course_shortname"] == "TEST101"

        graded = next(
            c for c in result["changes"] if c["kind"] == "grade_updated"
        )
        assert graded["title"] == "Problem Set 2"
        assert graded["after"]["grade_display"] == "88.00"

    def test_new_empty_grade_item_is_not_a_change(self, cache_path):
        client = _FakeClient()
        run_sync(client, cache_path=cache_path)
        client.grades["usergrades"][0]["gradeitems"].append(
            {"id": 7, "itemname": "Problem Set 3", "gradeformatted": "-",
             "grademin": 0, "grademax": 100},
        )
        result = run_sync(client, cache_path=cache_path)
        assert result["changes"] == []
        assert result["categories"]["grades"]["new"] == 1

    def test_failed_category_becomes_warning_and_skips(self, cache_path):
        client = _FakeClient()
        run_sync(client, cache_path=cache_path)

        client.fail_forums = True
        result = run_sync(client, cache_path=cache_path)
        assert result["categories"]["forums"]["synced"] is False
        assert any(w.startswith("forums:") for w in result["warnings"])
        assert result["changes"] == []

        # Recovery must not replay cached items as new changes.
        client.fail_forums = False
        result = run_sync(client, cache_path=cache_path)
        assert result["categories"]["forums"]["synced"] is True
        assert result["categories"]["forums"]["baseline"] is False
        assert result["changes"] == []

    def test_write_attempt_error_propagates(self, cache_path):
        client = _FakeClient()

        def _raise(course_ids):
            raise MoodleWriteAttemptError("blocked")

        client.get_forums_by_courses = _raise
        with pytest.raises(MoodleWriteAttemptError):
            run_sync(client, cache_path=cache_path)

    def test_result_is_json_serializable(self, cache_path):
        result = run_sync(_FakeClient(), cache_path=cache_path)
        json.dumps(result)


class TestGetRecentChanges:
    def test_returns_recorded_changes_with_filters(self, cache_path):
        client = _FakeClient()
        run_sync(client, cache_path=cache_path)
        client.assignments["courses"][0]["assignments"].append(
            {"id": 2, "name": "Problem Set 2", "duedate": FUTURE + 86400},
        )
        client.grades["usergrades"][0]["gradeitems"][0]["gradeformatted"] = "75.00"
        run_sync(client, cache_path=cache_path)

        changes = get_recent_changes(client.base_url, cache_path=cache_path)
        assert sorted(c["kind"] for c in changes) == [
            "grade_updated", "new_deadline",
        ]
        only_deadlines = get_recent_changes(
            client.base_url, cache_path=cache_path, category="deadlines",
        )
        assert [c["kind"] for c in only_deadlines] == ["new_deadline"]
        assert get_recent_changes(
            "https://other.example.com", cache_path=cache_path,
        ) == []

    def test_unknown_category_raises(self, cache_path):
        with pytest.raises(ValueError, match="unknown category"):
            get_recent_changes("site", cache_path=cache_path, category="bogus")


class TestDemoSync:
    def test_demo_baseline_then_quiet(self, cache_path):
        client = DemoMoodleClient(now=NOW)
        first = run_sync(client, cache_path=cache_path)
        assert first["changes"] == []
        assert first["warnings"] == []
        for stats in first["categories"].values():
            assert stats["synced"] is True
            assert stats["items"] > 0

        second = run_sync(DemoMoodleClient(now=NOW), cache_path=cache_path)
        assert second["changes"] == []

    def test_demo_cache_contains_no_tokens_or_file_urls(self, cache_path):
        run_sync(DemoMoodleClient(), cache_path=cache_path)
        raw = Path(cache_path).read_bytes()
        assert b"wstoken" not in raw
        assert b"file_url" not in raw
        assert b"fileurl" not in raw

    def test_demo_dataset_shift_produces_changes(self, cache_path):
        base = NOW - (NOW % 3600)
        run_sync(DemoMoodleClient(now=base), cache_path=cache_path)
        result = run_sync(DemoMoodleClient(now=base + 7200), cache_path=cache_path)
        kinds = {change["kind"] for change in result["changes"]}
        assert "deadline_changed" in kinds
        assert "file_updated" in kinds
        assert "forum_discussion_updated" in kinds


class TestSnapshotSafety:
    def test_snapshots_contain_no_file_urls(self):
        snapshots, scopes, warnings = collect_snapshots(_FakeClient())
        assert warnings == []
        assert scopes["grades"] == [201]
        text = json.dumps(snapshots)
        assert "file_url" not in text
        assert "fileurl" not in text
        assert "wstoken" not in text


class TestBaselineCorrectness:
    def test_empty_category_still_finishes_baselining(self, cache_path):
        """The first item in an initially empty category must be a change."""
        client = _FakeClient()
        client.forums = {"forums": []}
        client.discussions = {}
        first = run_sync(client, cache_path=cache_path)
        assert first["categories"]["forums"]["items"] == 0
        assert first["categories"]["forums"]["baseline"] is True

        client.forums = {"forums": [
            {"id": 6, "course": 201, "name": "Announcements", "type": "news",
             "numdiscussions": 1},
        ]}
        client.discussions = {6: [
            {"discussion": 301, "name": "First ever post",
             "userfullname": "Dr Fake", "created": RECENT,
             "timemodified": RECENT, "numunread": 0},
        ]}
        second = run_sync(client, cache_path=cache_path)
        assert second["categories"]["forums"]["baseline"] is False
        assert [c["kind"] for c in second["changes"]] == ["new_forum_discussion"]


class _TwoCourseClient(_FakeClient):
    """Second course whose gradebook can be toggled to fail."""

    def __init__(self):
        super().__init__()
        self.courses.append(
            {"id": 202, "shortname": "TEST202", "fullname": "Testing 202"},
        )
        self.grades_202 = {"usergrades": [{"gradeitems": [
            {"id": 21, "itemname": "Lab Report", "gradeformatted": "64.00",
             "grademin": 0, "grademax": 100},
        ]}]}
        self.fail_202_grades = True

    def get_user_grade_items(self, course_id, user_id=None):
        if int(course_id) == 202:
            if self.fail_202_grades:
                raise RuntimeError("gradebook denied")
            return self.grades_202
        return self.grades


class TestGradesScope:
    def test_recovered_course_is_adopted_without_spurious_changes(self, cache_path):
        client = _TwoCourseClient()
        first = run_sync(client, cache_path=cache_path)
        assert first["categories"]["grades"]["synced"] is True
        assert any(w.startswith("grades: TEST202") for w in first["warnings"])

        # Course 202 becomes readable: its graded item must be adopted
        # silently, not reported as a grade_updated with before: null.
        client.fail_202_grades = False
        second = run_sync(client, cache_path=cache_path)
        assert second["changes"] == []
        assert second["categories"]["grades"]["adopted"] == 1
        assert second["warnings"] == []

        # From now on the course is in scope: real updates are reported.
        client.grades_202["usergrades"][0]["gradeitems"][0]["gradeformatted"] = "71.00"
        third = run_sync(client, cache_path=cache_path)
        assert [c["kind"] for c in third["changes"]] == ["grade_updated"]
        assert third["changes"][0]["course_shortname"] == "TEST202"


class TestStrictDeadlines:
    def test_assignment_fetch_failure_fails_the_category(self, cache_path):
        client = _FakeClient()
        original = client.get_assignments_by_courses

        def _boom(course_ids):
            raise RuntimeError("assignments endpoint down")

        client.get_assignments_by_courses = _boom
        result = run_sync(client, cache_path=cache_path)
        assert result["categories"]["deadlines"]["synced"] is False
        assert any(w.startswith("deadlines:") for w in result["warnings"])

        # Recovery is the category's true baseline — still no spurious events.
        client.get_assignments_by_courses = original
        result = run_sync(client, cache_path=cache_path)
        assert result["categories"]["deadlines"]["synced"] is True
        assert result["categories"]["deadlines"]["baseline"] is True
        assert result["changes"] == []


class TestConcurrentSyncs:
    def test_no_duplicate_change_events(self, cache_path):
        import threading

        client = _FakeClient()
        run_sync(client, cache_path=cache_path)
        client.grades["usergrades"][0]["gradeitems"][0]["gradeformatted"] = "99.00"

        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def worker():
            try:
                barrier.wait()
                run_sync(client, cache_path=cache_path)
            except Exception as exc:  # pragma: no cover - failure detail
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []

        recorded = get_recent_changes(client.base_url, cache_path=cache_path)
        grade_events = [c for c in recorded if c["kind"] == "grade_updated"]
        assert len(grade_events) == 1


class TestSincePrecision:
    def test_since_ts_is_not_rounded_to_days(self, cache_path):
        from worsaga.cache import CacheStore

        with CacheStore(cache_path) as store:
            store.record_change(
                "site", "grades", "k1",
                {"kind": "grade_updated", "title": "old"}, now=100,
            )
            store.record_change(
                "site", "grades", "k2",
                {"kind": "grade_updated", "title": "new"}, now=200,
            )
        changes = get_recent_changes("site", cache_path=cache_path, since_ts=150)
        assert [c["title"] for c in changes] == ["new"]


class TestCliSurface:
    def test_parser_accepts_sync_and_changes(self):
        from worsaga.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["sync", "--days", "30"])
        assert args.command == "sync"
        assert args.days == 30
        args = parser.parse_args(["changes", "--since", "48h",
                                  "--category", "grades"])
        assert args.command == "changes"
        assert args.category == "grades"

    def test_demo_sync_then_changes(self, monkeypatch, tmp_path, capsys):
        from worsaga.cli import main

        # Freeze the demo dataset so back-to-back syncs never straddle an
        # hour boundary (demo timestamps are truncated to the hour).
        import worsaga.demo as demo_module

        frozen = demo_module.build_demo_dataset(now=NOW)
        monkeypatch.setattr(
            demo_module, "build_demo_dataset", lambda now=None: frozen,
        )

        monkeypatch.setenv("WORSAGA_CACHE_PATH", str(tmp_path / "cli.db"))
        main(["--demo", "sync"])
        out = capsys.readouterr().out
        assert "Baseline established" in out
        assert "deadlines" in out

        main(["--demo", "sync"])
        out = capsys.readouterr().out
        assert "No changes since the last sync." in out

        main(["--demo", "changes"])
        out = capsys.readouterr().out
        assert "No changes recorded" in out

    def test_demo_sync_json_shape(self, monkeypatch, tmp_path, capsys):
        from worsaga.cli import main

        monkeypatch.setenv("WORSAGA_CACHE_PATH", str(tmp_path / "cli.db"))
        main(["--demo", "--json", "sync"])
        payload = json.loads(capsys.readouterr().out)
        assert set(payload["categories"]) == set(SYNC_CATEGORIES)
        assert payload["changes"] == []
        assert "wstoken" not in json.dumps(payload)

        main(["--demo", "--json", "changes"])
        assert json.loads(capsys.readouterr().out) == []


class TestMcpSurface:
    @pytest.fixture(autouse=True)
    def _demo_client(self, monkeypatch, tmp_path):
        mcp_server = pytest.importorskip("worsaga.mcp_server")
        monkeypatch.setenv("WORSAGA_CACHE_PATH", str(tmp_path / "mcp.db"))
        monkeypatch.setattr(mcp_server, "_client", DemoMoodleClient())
        self.mcp_server = mcp_server
        yield
        mcp_server._client = None

    def test_sync_now_returns_native_dict(self):
        result = self.mcp_server.sync_now()
        assert isinstance(result, dict)
        assert set(result["categories"]) == set(SYNC_CATEGORIES)
        assert result["changes"] == []

    def test_get_changes_returns_native_list(self):
        self.mcp_server.sync_now()
        assert self.mcp_server.get_changes() == []

    def test_get_changes_bad_category_is_structured_error(self):
        result = self.mcp_server.get_changes(category="bogus")
        assert len(result) == 1
        assert "unknown category" in result[0]["error"]
