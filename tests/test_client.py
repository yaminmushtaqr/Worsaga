"""Tests for the read-only Moodle client safeguards."""

import pytest
from unittest.mock import patch

from worsaga.client import (
    ALLOWED_FUNCTIONS,
    BLOCKED_PATTERNS,
    MoodleClient,
    MoodleWriteAttemptError,
)
from worsaga.config import MoodleConfig


@pytest.fixture()
def client():
    """Client with dummy credentials — never hits the network."""
    cfg = MoodleConfig(url="https://moodle.example.com", token="fake", userid=1)
    return MoodleClient(config=cfg)


# ── Allowlist enforcement ──────────────────────────────────────────


class TestAllowlist:
    def test_allowed_function_is_on_list(self):
        assert "core_enrol_get_users_courses" in ALLOWED_FUNCTIONS

    def test_unknown_function_is_blocked(self, client):
        with pytest.raises(MoodleWriteAttemptError, match="not on the Moodle read-only allowlist"):
            client.call("totally_unknown_function")

    def test_every_allowed_function_is_lowercase(self):
        for fn in ALLOWED_FUNCTIONS:
            assert fn == fn.lower(), f"{fn} should be lowercase"


# ── Blocked-pattern enforcement ────────────────────────────────────


class TestBlockedPatterns:
    """Verify that write-like function names are rejected
    even if somehow present on the allowlist."""

    WRITE_FUNCTIONS = [
        "mod_assign_submit_grading_form",
        "mod_assign_save_submission",
        "core_files_upload",
        "mod_forum_add_discussion_post",
        "core_message_send_instant_messages",
        "mod_quiz_start_attempt",
        "mod_quiz_process_attempt",
        "core_calendar_create_calendar_events",
        "core_calendar_delete_calendar_events",
        "core_course_update_courses",
        "core_user_update_users",
        "mod_assign_lock_submissions",
        "mod_assign_unlock_submissions",
        "mod_assign_grade_submission",
    ]

    @pytest.mark.parametrize("fn", WRITE_FUNCTIONS)
    def test_write_function_blocked(self, client, fn):
        with pytest.raises(MoodleWriteAttemptError, match="BLOCKED"):
            client.call(fn)

    def test_blocked_patterns_are_nonempty(self):
        assert len(BLOCKED_PATTERNS) > 0

    def test_no_allowed_function_matches_blocked_pattern(self):
        """Sanity check: none of the allowlisted functions should
        match any blocked pattern."""
        for fn in ALLOWED_FUNCTIONS:
            fn_lower = fn.lower()
            for pattern in BLOCKED_PATTERNS:
                assert pattern not in fn_lower, (
                    f"Allowed function '{fn}' matches blocked pattern '{pattern}'"
                )


# ── Config-based construction ──────────────────────────────────────


class TestClientConstruction:
    def test_client_from_config(self):
        cfg = MoodleConfig(url="https://example.com/moodle/", token="t", userid=42)
        c = MoodleClient(config=cfg)
        assert c.base_url == "https://example.com/moodle"  # trailing slash stripped
        assert c.userid == 42


class _FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size=None):
        if size is None:
            return self.payload
        return self.payload[:size]


class TestDownloadFile:
    def test_download_file_reads_full_response_by_default(self, client):
        payload = b"x" * (11 * 1024 * 1024)
        with patch("urllib.request.urlopen", return_value=_FakeResponse(payload)):
            data = client.download_file("https://moodle.example.com/pluginfile.php/123/file.pptx")
        assert data == payload
        assert len(data) == len(payload)

    def test_download_file_honors_explicit_max_bytes(self, client):
        payload = b"abcdef"
        with patch("urllib.request.urlopen", return_value=_FakeResponse(payload)):
            data = client.download_file(
                "https://moodle.example.com/pluginfile.php/123/file.txt",
                max_bytes=3,
            )
        assert data == b"abc"
