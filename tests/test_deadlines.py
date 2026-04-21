"""Tests for deadline normalisation and ordering."""

import logging
import time

import pytest

from worsaga.client import MoodleWriteAttemptError
from worsaga.deadlines import get_upcoming_deadlines, normalize_deadlines


class TestNormalizeDeadlines:
    def test_basic_normalization(self):
        raw = [
            {
                "name": "Essay &amp; Report",
                "course": "EC100",
                "due_iso": "2025-04-10T12:00:00+00:00",
                "type": "assignment",
            },
        ]
        result = normalize_deadlines(raw)
        assert len(result) == 1
        assert result[0]["title"] == "Essay & Report"  # HTML-unescaped
        assert result[0]["module"] == "EC100"
        assert result[0]["due_date"] == "2025-04-10T12:00:00+00:00"
        assert result[0]["type"] == "assignment"

    def test_missing_fields_get_defaults(self):
        raw = [{}]
        result = normalize_deadlines(raw)
        assert result[0]["title"] == "Untitled"
        assert result[0]["module"] == "Unknown"
        assert result[0]["type"] == "deadline"

    def test_empty_list(self):
        assert normalize_deadlines([]) == []


class TestDeadlineOrdering:
    """Verify that deadline lists are ordered by due_ts."""

    def _make_deadline(self, name: str, due_ts: float) -> dict:
        from datetime import datetime, timezone

        due_dt = datetime.fromtimestamp(due_ts, tz=timezone.utc)
        return {
            "name": name,
            "type": "assignment",
            "course": "TEST",
            "due_ts": due_ts,
            "due_str": due_dt.strftime("%b %d %H:%M UTC"),
            "due_iso": due_dt.isoformat(),
            "days_left": 1,
        }

    def test_already_sorted(self):
        now = time.time()
        items = [
            self._make_deadline("First", now + 1000),
            self._make_deadline("Second", now + 2000),
        ]
        items.sort(key=lambda x: x["due_ts"])
        assert items[0]["name"] == "First"

    def test_reverse_order_sorted(self):
        now = time.time()
        items = [
            self._make_deadline("Later", now + 5000),
            self._make_deadline("Sooner", now + 1000),
        ]
        items.sort(key=lambda x: x["due_ts"])
        assert items[0]["name"] == "Sooner"
        assert items[1]["name"] == "Later"

    def test_deduplication_by_key(self):
        """Simulate the seen-set dedup logic used in get_upcoming_deadlines."""
        seen: set[tuple[str, int]] = set()
        results = []
        for dup in range(3):
            key = ("assign", 42)
            if key not in seen:
                seen.add(key)
                results.append({"id": 42})
        assert len(results) == 1


# ── Fixtures & helpers for get_upcoming_deadlines ────────────────


def _fake_assignment(assign_id: int, name: str, due: float) -> dict:
    return {"id": assign_id, "name": name, "duedate": int(due)}


def _fake_quiz(quiz_id: int, name: str, close: float, course_id: int) -> dict:
    return {
        "id": quiz_id, "name": name, "timeclose": int(close), "course": course_id,
    }


class _RecordingClient:
    """Minimal stand-in for MoodleClient that records API calls."""

    def __init__(
        self,
        *,
        courses=None,
        assignments_result=None,
        quizzes_result=None,
        assignments_exc=None,
        quizzes_exc=None,
    ):
        self._courses = courses or []
        self._assignments_result = assignments_result or {"courses": []}
        self._quizzes_result = quizzes_result or {"quizzes": []}
        self._assignments_exc = assignments_exc
        self._quizzes_exc = quizzes_exc
        self.assignments_calls = []
        self.quizzes_calls = []
        self.single_assignment_calls = []

    def get_courses(self):
        return self._courses

    def get_assignments(self, course_id):
        """Kept to surface accidental N+1 regressions in tests."""
        self.single_assignment_calls.append(course_id)
        raise AssertionError(
            "get_upcoming_deadlines must not call get_assignments per-course; "
            "use get_assignments_by_courses instead."
        )

    def get_assignments_by_courses(self, course_ids):
        self.assignments_calls.append(list(course_ids))
        if self._assignments_exc is not None:
            raise self._assignments_exc
        return self._assignments_result

    def get_quizzes(self, course_ids=None):
        self.quizzes_calls.append(list(course_ids) if course_ids else [])
        if self._quizzes_exc is not None:
            raise self._quizzes_exc
        return self._quizzes_result


class TestAssignmentBatching:
    def test_assignments_fetched_in_one_batched_call(self):
        now = time.time()
        client = _RecordingClient(
            courses=[
                {"id": 1, "shortname": "EC100"},
                {"id": 2, "shortname": "MA100"},
                {"id": 3, "shortname": "ST100"},
            ],
            assignments_result={"courses": []},
        )
        get_upcoming_deadlines(client, lookahead_days=14)

        assert len(client.assignments_calls) == 1, (
            "assignments must be fetched in a single batched call, not N+1"
        )
        assert sorted(client.assignments_calls[0]) == [1, 2, 3]
        assert client.single_assignment_calls == []

    def test_batched_response_spans_all_courses(self):
        now = time.time()
        soon = now + 86400  # 1 day out
        client = _RecordingClient(
            courses=[
                {"id": 1, "shortname": "EC100"},
                {"id": 2, "shortname": "MA100"},
            ],
            assignments_result={
                "courses": [
                    {"id": 1, "assignments": [_fake_assignment(10, "EC Essay", soon)]},
                    {"id": 2, "assignments": [_fake_assignment(20, "MA PS", soon + 60)]},
                ],
            },
        )
        result = get_upcoming_deadlines(client, lookahead_days=14)

        names = {d["name"] for d in result if d["type"] == "assignment"}
        assert names == {"EC Essay", "MA PS"}
        by_name = {d["name"]: d for d in result}
        assert by_name["EC Essay"]["course"] == "EC100"
        assert by_name["MA PS"]["course"] == "MA100"

    def test_batched_assignment_output_shape_preserved(self):
        """Normalised entry keys must be unchanged by batching."""
        now = time.time()
        soon = now + 86400
        client = _RecordingClient(
            courses=[{"id": 1, "shortname": "EC100"}],
            assignments_result={
                "courses": [
                    {"id": 1, "assignments": [_fake_assignment(10, "Essay", soon)]},
                ],
            },
        )
        result = get_upcoming_deadlines(client, lookahead_days=14)
        assert len(result) == 1
        entry = result[0]
        assert set(entry.keys()) == {
            "name", "type", "course", "due_ts", "due_str", "due_iso", "days_left",
        }
        assert entry["type"] == "assignment"
        assert entry["course"] == "EC100"


class TestPartialFailureVisibility:
    """Failures must be logged, not silently swallowed."""

    def test_assignment_fetch_failure_is_logged(self, caplog):
        now = time.time()
        soon = now + 3600
        client = _RecordingClient(
            courses=[{"id": 1, "shortname": "EC100"}],
            assignments_exc=RuntimeError("Moodle API error: 500"),
            quizzes_result={
                "quizzes": [_fake_quiz(5, "Pop Quiz", soon, 1)],
            },
        )

        with caplog.at_level(logging.WARNING, logger="worsaga.deadlines"):
            result = get_upcoming_deadlines(client, lookahead_days=14)

        assignment_warnings = [
            r for r in caplog.records
            if r.name == "worsaga.deadlines"
            and "assignment fetch failed" in r.getMessage()
        ]
        assert assignment_warnings, "assignment failure must emit a warning"
        assert assignment_warnings[0].levelno == logging.WARNING

        # Quizzes still flow through.
        assert [d["name"] for d in result] == ["Pop Quiz"]

    def test_quiz_fetch_failure_is_logged(self, caplog):
        now = time.time()
        soon = now + 3600
        client = _RecordingClient(
            courses=[{"id": 1, "shortname": "EC100"}],
            assignments_result={
                "courses": [
                    {"id": 1, "assignments": [_fake_assignment(10, "Essay", soon)]},
                ],
            },
            quizzes_exc=RuntimeError("Moodle API error: 500"),
        )

        with caplog.at_level(logging.WARNING, logger="worsaga.deadlines"):
            result = get_upcoming_deadlines(client, lookahead_days=14)

        quiz_warnings = [
            r for r in caplog.records
            if r.name == "worsaga.deadlines"
            and "quiz fetch failed" in r.getMessage()
        ]
        assert quiz_warnings, "quiz failure must emit a warning"

        # Assignments still flow through.
        assert [d["name"] for d in result] == ["Essay"]

    def test_both_fetches_failing_logs_both_and_returns_empty(self, caplog):
        client = _RecordingClient(
            courses=[{"id": 1, "shortname": "EC100"}],
            assignments_exc=RuntimeError("assign-boom"),
            quizzes_exc=RuntimeError("quiz-boom"),
        )

        with caplog.at_level(logging.WARNING, logger="worsaga.deadlines"):
            result = get_upcoming_deadlines(client, lookahead_days=14)

        messages = [r.getMessage() for r in caplog.records]
        assert any("assignment fetch failed" in m for m in messages)
        assert any("quiz fetch failed" in m for m in messages)
        assert result == []

    def test_no_warnings_on_happy_path(self, caplog):
        client = _RecordingClient(
            courses=[{"id": 1, "shortname": "EC100"}],
            assignments_result={"courses": []},
            quizzes_result={"quizzes": []},
        )
        with caplog.at_level(logging.WARNING, logger="worsaga.deadlines"):
            get_upcoming_deadlines(client, lookahead_days=14)
        assert caplog.records == []


class TestWriteAttemptPropagation:
    """Read-only guarantee must stay observable: MoodleWriteAttemptError bubbles."""

    def test_assignment_write_attempt_propagates(self):
        client = _RecordingClient(
            courses=[{"id": 1, "shortname": "EC100"}],
            assignments_exc=MoodleWriteAttemptError(
                "BLOCKED: write-ish function",
            ),
        )
        with pytest.raises(MoodleWriteAttemptError):
            get_upcoming_deadlines(client, lookahead_days=14)

    def test_quiz_write_attempt_propagates(self):
        client = _RecordingClient(
            courses=[{"id": 1, "shortname": "EC100"}],
            assignments_result={"courses": []},
            quizzes_exc=MoodleWriteAttemptError("BLOCKED: quiz write"),
        )
        with pytest.raises(MoodleWriteAttemptError):
            get_upcoming_deadlines(client, lookahead_days=14)


class TestMixedSorting:
    def test_assignments_and_quizzes_sorted_together(self):
        now = time.time()
        client = _RecordingClient(
            courses=[{"id": 1, "shortname": "EC100"}],
            assignments_result={
                "courses": [
                    {"id": 1, "assignments": [
                        _fake_assignment(10, "A-late", now + 7 * 86400),
                    ]},
                ],
            },
            quizzes_result={
                "quizzes": [
                    _fake_quiz(20, "Q-early", now + 1 * 86400, 1),
                ],
            },
        )
        result = get_upcoming_deadlines(client, lookahead_days=14)
        assert [d["name"] for d in result] == ["Q-early", "A-late"]

    def test_out_of_window_items_filtered(self):
        now = time.time()
        client = _RecordingClient(
            courses=[{"id": 1, "shortname": "EC100"}],
            assignments_result={
                "courses": [
                    {"id": 1, "assignments": [
                        _fake_assignment(10, "In-window", now + 86400),
                        _fake_assignment(11, "Past", now - 86400),
                        _fake_assignment(12, "Far-future", now + 30 * 86400),
                    ]},
                ],
            },
        )
        result = get_upcoming_deadlines(client, lookahead_days=14)
        assert [d["name"] for d in result] == ["In-window"]


class TestGetAssignmentsByCoursesClient:
    """The client helper that backs the batched deadlines fetch."""

    def test_empty_course_ids_short_circuits(self):
        """Empty list must not make a network call and must return a valid shape."""
        from worsaga.client import MoodleClient
        from worsaga.config import MoodleConfig

        cfg = MoodleConfig(url="https://moodle.example.com", token="fake", userid=1)
        client = MoodleClient(config=cfg)

        # call() would fail without a fake urlopen — confirming no request happens.
        result = client.get_assignments_by_courses([])
        assert result == {"courses": []}

    def test_builds_indexed_courseids_params(self, monkeypatch):
        from worsaga.client import MoodleClient
        from worsaga.config import MoodleConfig

        cfg = MoodleConfig(url="https://moodle.example.com", token="fake", userid=1)
        client = MoodleClient(config=cfg)

        captured = {}

        def fake_call(wsfunction, **params):
            captured["wsfunction"] = wsfunction
            captured["params"] = params
            return {"courses": []}

        monkeypatch.setattr(client, "call", fake_call)
        client.get_assignments_by_courses([7, 8, 9])

        assert captured["wsfunction"] == "mod_assign_get_assignments"
        assert captured["params"] == {
            "courseids[0]": 7, "courseids[1]": 8, "courseids[2]": 9,
        }
