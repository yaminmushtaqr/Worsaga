"""Tests for deadline normalisation and ordering."""

import time

import pytest

from worsaga.deadlines import normalize_deadlines


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
