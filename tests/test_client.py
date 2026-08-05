"""Tests for the read-only Moodle client safeguards."""

import urllib.error

import pytest
from unittest.mock import patch

import json

from worsaga.client import (
    ALLOWED_FUNCTION_POLICIES,
    ALLOWED_FUNCTIONS,
    BLOCKED_PATTERNS,
    AssignmentNotFoundError,
    CourseNotFoundError,
    DownloadError,
    MoodleClient,
    MoodleRequestError,
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

    def test_allowed_functions_have_policy_metadata(self):
        assert set(ALLOWED_FUNCTIONS) == set(ALLOWED_FUNCTION_POLICIES)
        for fn, policy in ALLOWED_FUNCTION_POLICIES.items():
            assert policy["purpose"]
            assert policy["changes_user_state"] is False
            assert isinstance(policy["exposed"], bool), fn

    def test_unknown_function_is_blocked(self, client):
        with pytest.raises(MoodleWriteAttemptError, match="not on the Moodle read-only allowlist"):
            client.call("totally_unknown_function")

    def test_every_allowed_function_is_lowercase(self):
        for fn in ALLOWED_FUNCTIONS:
            assert fn == fn.lower(), f"{fn} should be lowercase"

    def test_enrolled_users_is_not_allowlisted(self, client):
        """core_enrol_get_enrolled_users exposes third-party PII and must
        stay off the allowlist until a real feature needs it."""
        assert "core_enrol_get_enrolled_users" not in ALLOWED_FUNCTIONS
        with pytest.raises(MoodleWriteAttemptError):
            client.call("core_enrol_get_enrolled_users", courseid=1)

    def test_call_sends_user_agent(self, client):
        seen = {}

        class _JsonResponse:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size=None):
                return b"[]"

        def _fake_urlopen(req, timeout=30):
            seen["ua"] = req.get_header("User-agent")
            return _JsonResponse()

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            client.call("core_enrol_get_users_courses", userid=1)

        from worsaga import __version__
        assert seen["ua"] == f"worsaga/{__version__}"


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
        "mod_forum_view_forum",
        "mod_forum_view_forum_discussion",
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


class TestClientConvenienceMethods:
    def test_get_assignment_submission_status(self, client):
        with patch.object(client, "call", return_value={}) as mock_call:
            client.get_assignment_submission_status(10)
        mock_call.assert_called_once_with(
            "mod_assign_get_submission_status",
            assignid=10,
        )

    def test_mod_assign_get_grades_is_not_allowlisted(self, client):
        # Removed in 0.6.0: the response can include other students'
        # grades for teacher-capable tokens and no feature needs it.
        from worsaga.client import ALLOWED_FUNCTIONS, MoodleWriteAttemptError

        assert "mod_assign_get_grades" not in ALLOWED_FUNCTIONS
        with pytest.raises(MoodleWriteAttemptError):
            client.call("mod_assign_get_grades", **{"assignmentids[0]": 10})

    def test_get_user_grade_items_defaults_to_config_userid(self, client):
        with patch.object(client, "call", return_value={}) as mock_call:
            client.get_user_grade_items(10)
        mock_call.assert_called_once_with(
            "gradereport_user_get_grade_items",
            courseid=10,
            userid=1,
        )

    def test_get_user_grade_items_accepts_explicit_userid(self, client):
        with patch.object(client, "call", return_value={}) as mock_call:
            client.get_user_grade_items(10, user_id=99)
        mock_call.assert_called_once_with(
            "gradereport_user_get_grade_items",
            courseid=10,
            userid=99,
        )

    def test_get_course_grades_defaults_to_config_userid(self, client):
        with patch.object(client, "call", return_value={}) as mock_call:
            client.get_course_grades(10)
        mock_call.assert_called_once_with(
            "core_grades_get_grades",
            courseid=10,
            **{"userids[0]": 1},
        )

    def test_get_course_grades_accepts_multiple_userids(self, client):
        with patch.object(client, "call", return_value={}) as mock_call:
            client.get_course_grades(10, user_ids=[7, 8])
        mock_call.assert_called_once_with(
            "core_grades_get_grades",
            courseid=10,
            **{"userids[0]": 7, "userids[1]": 8},
        )

    def test_get_forums_by_courses(self, client):
        with patch.object(client, "call", return_value={}) as mock_call:
            assert client.get_forums_by_courses([10, 11]) == {}
        mock_call.assert_called_once_with(
            "mod_forum_get_forums_by_courses",
            **{"courseids[0]": 10, "courseids[1]": 11},
        )

    def test_get_forums_by_courses_wraps_moodle_list_payload(self, client):
        forum = {"id": 5, "course": 10, "name": "Announcements"}
        with patch.object(client, "call", return_value=[forum]):
            assert client.get_forums_by_courses([10]) == {"forums": [forum]}

    def test_get_forum_discussions(self, client):
        with patch.object(client, "call", return_value={}) as mock_call:
            client.get_forum_discussions(5)
        mock_call.assert_called_once_with("mod_forum_get_forum_discussions", forumid=5)

    def test_get_popup_notifications(self, client):
        with patch.object(client, "call", return_value={}) as mock_call:
            client.get_popup_notifications(unread_only=True)
        mock_call.assert_called_once_with(
            "message_popup_get_popup_notifications",
            useridto=1,
            newestfirst=1,
            limit=100,
            offset=0,
        )

    def test_get_messages(self, client):
        with patch.object(client, "call", return_value={}) as mock_call:
            client.get_messages(since_time=123)
        assert mock_call.call_args.args == ("core_message_get_messages",)
        assert "currentuserid" not in mock_call.call_args.kwargs
        assert "timefrom" not in mock_call.call_args.kwargs
        assert mock_call.call_args.kwargs["useridto"] == 1
        assert mock_call.call_args.kwargs["useridfrom"] == 0
        assert mock_call.call_args.kwargs["read"] == 2

    def test_get_calendar_events(self, client):
        with patch.object(client, "call", return_value={}) as mock_call:
            client.get_calendar_events(course_ids=[10], timestart=1, timeend=2)
        mock_call.assert_called_once_with(
            "core_calendar_get_calendar_events",
            **{
                "events[courseids][0]": 10,
                "options[timestart]": 1,
                "options[timeend]": 2,
            },
        )

    def test_get_action_events_by_timesort(self, client):
        with patch.object(client, "call", return_value={}) as mock_call:
            client.get_action_events_by_timesort(1, 2, limit=50)
        mock_call.assert_called_once_with(
            "core_calendar_get_action_events_by_timesort",
            timesortfrom=1,
            timesortto=2,
            limitnum=50,
        )


class _FakeResponse:
    def __init__(self, payload: bytes, headers: dict | None = None):
        self.payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size=None):
        if size is None:
            return self.payload
        return self.payload[:size]


class TestDownloadFile:
    def test_download_file_reads_files_within_default_cap(self, client):
        payload = b"x" * (11 * 1024 * 1024)
        with patch("urllib.request.urlopen", return_value=_FakeResponse(payload)):
            data = client.download_file("https://moodle.example.com/pluginfile.php/123/file.pptx")
        assert data == payload
        assert len(data) == len(payload)

    def test_download_file_oversize_body_is_skipped_not_truncated(self, client):
        payload = b"abcdef"
        with patch("urllib.request.urlopen", return_value=_FakeResponse(payload)):
            with pytest.raises(DownloadError) as exc_info:
                client.download_file(
                    "https://moodle.example.com/pluginfile.php/123/file.txt",
                    max_bytes=3,
                )
        assert exc_info.value.code == "oversize"

    def test_download_file_oversize_content_length_precheck(self, client):
        response = _FakeResponse(b"", headers={"Content-Length": str(60 * 1024 * 1024)})
        with patch("urllib.request.urlopen", return_value=response):
            with pytest.raises(DownloadError) as exc_info:
                client.download_file(
                    "https://moodle.example.com/pluginfile.php/123/big.pdf",
                )
        assert exc_info.value.code == "oversize"

    def test_download_file_uncapped_when_explicitly_requested(self, client):
        payload = b"abcdef"
        with patch("urllib.request.urlopen", return_value=_FakeResponse(payload)):
            data = client.download_file(
                "https://moodle.example.com/pluginfile.php/123/file.txt",
                max_bytes=None,
            )
        assert data == payload

    def test_download_file_adds_token_only_to_moodle_file_url(self, client):
        seen = {}

        def _fake_urlopen(req, timeout=30):
            seen["url"] = req.full_url
            return _FakeResponse(b"ok")

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            data = client.download_file(
                "https://moodle.example.com/pluginfile.php/123/file.txt?forcedownload=1",
            )

        assert data == b"ok"
        assert "forcedownload=1" in seen["url"]
        assert "token=fake" in seen["url"]

    def test_download_file_sends_user_agent(self, client):
        seen = {}

        def _fake_urlopen(req, timeout=30):
            seen["ua"] = req.get_header("User-agent")
            return _FakeResponse(b"ok")

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            client.download_file(
                "https://moodle.example.com/pluginfile.php/123/file.txt",
            )

        from worsaga import __version__
        assert seen["ua"] == f"worsaga/{__version__}"

    def test_download_file_rejects_external_host_before_tokenizing(self, client):
        with patch("urllib.request.urlopen") as mock_urlopen:
            with pytest.raises(DownloadError) as exc_info:
                client.download_file(
                    "https://evil.example.com/pluginfile.php/123/file.txt",
                )

        assert exc_info.value.code == "invalid_url"
        mock_urlopen.assert_not_called()

    def test_download_file_rejects_non_file_moodle_url(self, client):
        with patch("urllib.request.urlopen") as mock_urlopen:
            with pytest.raises(DownloadError) as exc_info:
                client.download_file(
                    "https://moodle.example.com/mod/url/view.php?id=123",
                )

        assert exc_info.value.code == "invalid_url"
        mock_urlopen.assert_not_called()

    @pytest.mark.parametrize("status,code", [(401, "auth"), (403, "auth"), (404, "not_found"), (500, "network")])
    def test_download_file_categorizes_http_errors(self, client, status, code):
        error = urllib.error.HTTPError(
            "https://moodle.example.com/pluginfile.php/123/file.txt?token=fake",
            status, "boom", hdrs=None, fp=None,
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with pytest.raises(DownloadError) as exc_info:
                client.download_file(
                    "https://moodle.example.com/pluginfile.php/123/file.txt",
                )
        assert exc_info.value.code == code
        # Redaction: no token, no URL in the message or the chained cause.
        assert "token=" not in str(exc_info.value)
        assert "pluginfile" not in str(exc_info.value)
        assert exc_info.value.__cause__ is None

    def test_download_file_categorizes_network_errors(self, client):
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with pytest.raises(DownloadError) as exc_info:
                client.download_file(
                    "https://moodle.example.com/pluginfile.php/123/file.txt",
                )
        assert exc_info.value.code == "network"

    def test_download_file_empty_body_is_structured_error(self, client):
        with patch("urllib.request.urlopen", return_value=_FakeResponse(b"")):
            with pytest.raises(DownloadError) as exc_info:
                client.download_file(
                    "https://moodle.example.com/pluginfile.php/123/file.txt",
                )
        assert exc_info.value.code == "empty"


def _exception_response(*, errorcode: str, message: str) -> "_FakeResponse":
    """A _FakeResponse carrying a Moodle web-service exception payload."""
    payload = json.dumps({
        "exception": "dml_missing_record_exception",
        "errorcode": errorcode,
        "message": message,
    }).encode()
    return _FakeResponse(payload)


class TestDomainNotFoundErrors:
    """Moodle "record not found" failures become friendly, classifiable
    domain exceptions instead of raw DB wording."""

    def test_course_contents_missing_record_raises_course_not_found(self, client):
        response = _exception_response(
            errorcode="invalidrecord",
            message="Can't find data record in database table course.",
        )
        with patch("urllib.request.urlopen", return_value=response):
            with pytest.raises(CourseNotFoundError) as exc_info:
                client.get_course_contents(999999)
        assert exc_info.value.course_id == 999999
        assert "999999" in str(exc_info.value)
        assert "not enrolled or does not exist" in str(exc_info.value)
        # The raw Moodle DB wording is replaced, not surfaced.
        assert "data record" not in str(exc_info.value)

    def test_course_not_found_detected_by_errorcode_only(self, client):
        # A localised (non-English) message must still be classified via
        # the stable errorcode.
        response = _exception_response(
            errorcode="invalidcourseid",
            message="No se puede encontrar el registro",
        )
        with patch("urllib.request.urlopen", return_value=response):
            with pytest.raises(CourseNotFoundError):
                client.get_course_contents(42)

    def test_grade_items_missing_record_raises_course_not_found(self, client):
        response = _exception_response(
            errorcode="invalidrecord",
            message="Can't find data record in database table course.",
        )
        with patch("urllib.request.urlopen", return_value=response):
            with pytest.raises(CourseNotFoundError) as exc_info:
                client.get_user_grade_items(555)
        assert exc_info.value.course_id == 555

    def test_submission_status_missing_record_raises_assignment_not_found(self, client):
        response = _exception_response(
            errorcode="invalidrecord",
            message="Can't find data record in database table assign.",
        )
        with patch("urllib.request.urlopen", return_value=response):
            with pytest.raises(AssignmentNotFoundError) as exc_info:
                client.get_assignment_submission_status(777)
        assert exc_info.value.assignment_id == 777
        # AssignmentNotFoundError stays a ValueError for backward compat.
        assert isinstance(exc_info.value, ValueError)

    def test_non_missing_record_moodle_error_stays_request_error(self, client):
        # An auth/permission failure must keep raising, not be swallowed as
        # a not-found domain error.
        response = _exception_response(
            errorcode="accessexception",
            message="Access control exception",
        )
        with patch("urllib.request.urlopen", return_value=response):
            with pytest.raises(MoodleRequestError) as exc_info:
                client.get_course_contents(1)
        assert not isinstance(exc_info.value, CourseNotFoundError)
        assert "Moodle API error" in str(exc_info.value)
        assert exc_info.value.errorcode == "accessexception"
