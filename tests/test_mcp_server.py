"""Tests for worsaga's MCP server tool surface.

Verifies that every tool returns native dict/list structures rather than
JSON-encoded strings, and that error shapes for ``download_material`` are
preserved as structured dicts.
"""

import pytest
from unittest.mock import patch

pytest.importorskip("mcp")

from worsaga import mcp_server  # noqa: E402
from worsaga.materials import MaterialSelectionError  # noqa: E402


class _FakeClient:
    """Minimal stand-in for MoodleClient used across MCP tool tests."""

    base_url = "https://moodle.example.com"

    def __init__(
        self,
        *,
        courses=None,
        contents=None,
        file_bytes=None,
        grade_payload=None,
        assignment_payload=None,
        assignment_status=None,
        forums_payload=None,
        discussions_payload=None,
        notifications_payload=None,
        messages_payload=None,
        calendar_payload=None,
    ):
        self._courses = courses or []
        self._contents = contents or []
        self._file_bytes = file_bytes
        self._grade_payload = grade_payload or {"usergrades": []}
        self._assignment_payload = assignment_payload or {"courses": []}
        self._assignment_status = assignment_status or {}
        self._forums_payload = forums_payload or {"forums": []}
        self._discussions_payload = discussions_payload or {"discussions": []}
        self._notifications_payload = notifications_payload or {"notifications": []}
        self._messages_payload = messages_payload or {"messages": []}
        self._calendar_payload = calendar_payload or {"events": []}

    def get_courses(self):
        return self._courses

    def get_course_contents(self, course_id):
        return self._contents

    def download_file(self, fileurl, *, max_bytes=None):
        return self._file_bytes

    def get_user_grade_items(self, course_id):
        return self._grade_payload

    def get_assignments_by_courses(self, course_ids):
        return self._assignment_payload

    def get_assignment_submission_status(self, assignment_id):
        return self._assignment_status.get(assignment_id, {})

    def get_assignment_grades(self, assignment_ids):
        return {"assignments": []}

    def get_forums_by_courses(self, course_ids):
        return self._forums_payload

    def get_forum_discussions(self, forum_id):
        return self._discussions_payload

    def get_popup_notifications(self, unread_only=False):
        return self._notifications_payload

    def get_messages(self, since_time=None):
        return self._messages_payload

    def get_calendar_events(self, course_ids=None, timestart=None, timeend=None):
        return self._calendar_payload


@pytest.fixture(autouse=True)
def _reset_client_cache():
    """Drop the module-level client so tests patch cleanly."""
    mcp_server._client = None
    yield
    mcp_server._client = None


# ── Native structure returns (no json.dumps) ───────────────────────


class TestNativeReturns:
    def test_list_courses_returns_list(self):
        client = _FakeClient(courses=[
            {"id": 1, "shortname": "ECON101", "fullname": "Econ"},
        ])
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.list_courses()

        assert isinstance(result, list)
        assert result == [{"id": 1, "shortname": "ECON101", "fullname": "Econ"}]

    def test_get_deadlines_returns_list(self):
        client = _FakeClient()
        with patch.object(mcp_server, "_get_client", return_value=client), \
             patch.object(
                 mcp_server, "get_upcoming_deadlines",
                 return_value=[{"name": "Essay", "days_left": 3}],
             ):
            result = mcp_server.get_deadlines(lookahead_days=7)

        assert isinstance(result, list)
        assert result == [{"name": "Essay", "days_left": 3}]

    def test_get_grades_returns_list(self):
        client = _FakeClient(
            courses=[{"id": 1, "shortname": "ECON101"}],
            grade_payload={
                "usergrades": [{
                    "gradeitems": [{"id": 1, "itemname": "Essay", "gradeformatted": "70"}],
                }],
            },
        )
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.get_grades()

        assert isinstance(result, list)
        assert result[0]["item_name"] == "Essay"

    def test_get_grade_summary_returns_dict(self):
        client = _FakeClient(
            courses=[{"id": 1, "shortname": "ECON101"}],
            grade_payload={
                "usergrades": [{
                    "gradeitems": [{"id": 1, "itemname": "Essay", "gradeformatted": "-"}],
                }],
            },
        )
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.get_grade_summary()

        assert isinstance(result, dict)
        assert result["status_counts"] == {"missing": 1}

    def test_get_assignments_returns_list(self):
        client = _FakeClient(
            courses=[{"id": 1, "shortname": "ECON101"}],
            assignment_payload={
                "courses": [{
                    "id": 1,
                    "assignments": [{"id": 10, "cmid": 99, "name": "Essay"}],
                }],
            },
            assignment_status={10: {"lastattempt": {"submission": {"status": "submitted"}}}},
        )
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.get_assignments()

        assert isinstance(result, list)
        assert result[0]["name"] == "Essay"

    def test_get_assignment_status_returns_dict(self):
        client = _FakeClient(
            courses=[{"id": 1, "shortname": "ECON101"}],
            assignment_payload={
                "courses": [{
                    "id": 1,
                    "assignments": [{"id": 10, "cmid": 99, "name": "Essay"}],
                }],
            },
            assignment_status={10: {"lastattempt": {"submission": {"status": "submitted"}}}},
        )
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.get_assignment_status(1, 10)

        assert isinstance(result, dict)
        assert result["id"] == 10

    def test_get_course_forums_returns_list(self):
        client = _FakeClient(forums_payload={"forums": [{"id": 5, "course": 1, "name": "Announcements"}]})
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.get_course_forums(1)
        assert isinstance(result, list)
        assert result[0]["forum_id"] == 5

    def test_get_forum_discussions_returns_list(self):
        client = _FakeClient(
            forums_payload={"forums": [{"id": 5, "course": 1, "name": "Announcements"}]},
            discussions_payload={"discussions": [{"discussion": 9, "name": "Update"}]},
        )
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.get_forum_discussions(1)
        assert isinstance(result, list)
        assert result[0]["discussion_id"] == 9

    def test_get_latest_updates_returns_list(self):
        client = _FakeClient(
            courses=[{"id": 1, "shortname": "ECON101"}],
            forums_payload={"forums": [{"id": 5, "course": 1, "name": "Announcements"}]},
            discussions_payload={"discussions": [{"discussion": 9, "name": "Update", "timemodified": 9999999999}]},
        )
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.get_latest_updates(since_days=7)
        assert isinstance(result, list)
        assert result[0]["name"] == "Update"

    def test_get_notifications_returns_list(self):
        client = _FakeClient(notifications_payload={"notifications": [{"id": 1, "subject": "Notice"}]})
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.get_notifications()
        assert isinstance(result, list)
        assert result[0]["subject"] == "Notice"

    def test_get_messages_returns_list(self):
        client = _FakeClient(messages_payload={"messages": [{"id": 1, "smallmessage": "Hi"}]})
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.get_messages()
        assert isinstance(result, list)
        assert result[0]["subject"] == "Hi"

    def test_get_digest_returns_dict(self):
        client = _FakeClient()
        with patch.object(mcp_server, "_get_client", return_value=client), \
             patch.object(mcp_server, "_get_digest", return_value={"warnings": []}):
            result = mcp_server.get_digest()
        assert isinstance(result, dict)

    def test_get_calendar_events_returns_list(self):
        client = _FakeClient(calendar_payload={"events": [{"id": 1, "name": "Deadline"}]})
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.get_calendar_events()
        assert isinstance(result, list)
        assert result[0]["name"] == "Deadline"

    def test_get_calendar_events_accepts_week_filter(self):
        client = _FakeClient(
            contents=[{"name": "Week 3", "modules": []}],
            calendar_payload={
                "events": [
                    {"id": 1, "name": "Week 3 quiz"},
                    {"id": 2, "name": "Week 4 quiz"},
                ],
            },
        )
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.get_calendar_events(course_id=42, week="3")
        assert [row["id"] for row in result] == [1]

    def test_get_course_contents_returns_list(self):
        sections = [{"id": 1, "name": "Week 1", "modules": []}]
        client = _FakeClient(contents=sections)
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.get_course_contents(42)

        assert isinstance(result, list)
        assert result == sections

    def test_get_week_materials_returns_list(self):
        sections = [
            {
                "id": 1,
                "name": "Week 3",
                "section": 3,
                "modules": [
                    {
                        "id": 10,
                        "name": "Lecture 3",
                        "modname": "resource",
                        "contents": [
                            {
                                "type": "file",
                                "filename": "w3.pdf",
                                "fileurl": "https://moodle.example.com/pluginfile.php/1/w3.pdf",
                                "filesize": 2048,
                                "mimetype": "application/pdf",
                                "timemodified": 0,
                            },
                        ],
                    },
                ],
            },
        ]
        client = _FakeClient(contents=sections)
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.get_week_materials(42, "3")

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["file_name"] == "w3.pdf"
        assert result[0]["course_id"] == 42
        # Raw authenticated Moodle URLs stay out of MCP responses.
        assert "file_url" not in result[0]
        assert "view_url" in result[0]

    def test_search_course_content_returns_list(self):
        sections = [
            {
                "id": 1,
                "name": "Week 2: Regression",
                "section": 2,
                "modules": [
                    {"id": 5, "name": "Regression basics", "modname": "resource"},
                ],
            },
        ]
        client = _FakeClient(contents=sections)
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.search_course_content(42, "regression")

        assert isinstance(result, list)
        assert any(r["module_id"] == 5 for r in result)

    def test_get_weekly_summary_returns_dict(self):
        # No modules → summary falls through to fallback bullets.
        sections = [{"id": 1, "name": "Week 1", "section": 1, "modules": []}]
        client = _FakeClient(contents=sections)
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.get_weekly_summary(42, 1)

        assert isinstance(result, dict)
        for key in (
            "bullets", "method", "section_type",
            "file_count", "section_name", "week", "course_id", "formatted",
        ):
            assert key in result
        assert result["week"] == 1
        assert result["course_id"] == 42
        assert isinstance(result["bullets"], list)
        assert isinstance(result["formatted"], str)

    def test_get_weekly_summary_accepts_string_week_query(self):
        sections = [{"id": 1, "name": "Revision", "section": 9, "modules": []}]
        client = _FakeClient(contents=sections)
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.get_weekly_summary(42, "revision")

        assert result["week"] == "revision"
        assert result["section_name"] == "Revision"


# ── Structured error shapes for download_material ────────────────


def _section_with_materials(*entries):
    """Build a section dict holding *entries* as file contents."""
    return [
        {
            "id": 1,
            "name": "Week 1",
            "section": 1,
            "modules": [
                {
                    "id": idx + 10,
                    "name": f"Module {idx}",
                    "modname": "resource",
                    "contents": [
                        {
                            "type": "file",
                            "filename": f"{name}.pdf",
                            "fileurl": f"https://moodle.example.com/{name}.pdf",
                            "filesize": 1024,
                            "mimetype": "application/pdf",
                            "timemodified": 0,
                        },
                    ],
                }
                for idx, name in enumerate(entries)
            ],
        },
    ]


class TestDownloadMaterialErrorShapes:
    def test_no_materials_returns_error_dict(self):
        client = _FakeClient(contents=[])
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.download_material(42, "99")

        assert isinstance(result, dict)
        assert "No materials found for week '99'." in result["error"]
        assert result["candidates"] == []

    def test_ambiguous_match_returns_candidate_list(self):
        sections = _section_with_materials("notes_a", "notes_b")
        client = _FakeClient(contents=sections)
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.download_material(42, "1")

        assert isinstance(result, dict)
        assert "error" in result
        assert isinstance(result["candidates"], list)
        assert len(result["candidates"]) == 2
        # Candidate summaries must not carry any token-bearing file URLs.
        for c in result["candidates"]:
            assert "token" not in str(c).lower()
            assert c["file_name"].endswith(".pdf")
            assert "index" in c

    def test_selection_error_without_candidates(self):
        sections = _section_with_materials("notes_a")
        client = _FakeClient(contents=sections)

        def _raise_no_match(materials, *, match=None, index=None):
            raise MaterialSelectionError("No materials matching 'nope'.", [])

        with patch.object(mcp_server, "_get_client", return_value=client), \
             patch.object(mcp_server, "_select_material", side_effect=_raise_no_match):
            result = mcp_server.download_material(42, "1", match="nope")

        assert isinstance(result, dict)
        assert result["error"] == "No materials matching 'nope'."
        assert result["candidates"] == []

    def test_runtime_failure_returns_error_dict(self):
        sections = _section_with_materials("notes_a")
        client = _FakeClient(contents=sections, file_bytes=None)
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.download_material(42, "1", index=0)

        assert isinstance(result, dict)
        assert "error" in result
        assert "Download failed" in result["error"]
        assert "candidates" not in result

    def test_successful_download_returns_plain_dict(self, tmp_path):
        sections = _section_with_materials("notes_a")
        client = _FakeClient(contents=sections, file_bytes=b"hello-bytes")
        with patch.object(mcp_server, "_get_client", return_value=client), \
             patch.object(
                 mcp_server, "default_downloads_dir", return_value=tmp_path,
             ):
            result = mcp_server.download_material(42, "1", index=0)

        assert isinstance(result, dict)
        assert result["file_name"] == "notes_a.pdf"
        assert result["bytes_written"] == len(b"hello-bytes")
        assert result["local_path"].endswith("notes_a.pdf")
        assert result["local_path"].startswith(str(tmp_path))
        # Token/authentication details must stay out of the return shape.
        assert "token" not in result
        assert "fileurl" not in result

    def test_relative_output_dir_stays_inside_downloads_root(self, tmp_path):
        sections = _section_with_materials("notes_a")
        client = _FakeClient(contents=sections, file_bytes=b"hello-bytes")
        with patch.object(mcp_server, "_get_client", return_value=client), \
             patch.object(
                 mcp_server, "default_downloads_dir", return_value=tmp_path,
             ):
            result = mcp_server.download_material(
                42, "1", index=0, output_dir="econ/week1",
            )

        assert "error" not in result
        assert (tmp_path / "econ" / "week1" / "notes_a.pdf").exists()

    def test_absolute_output_dir_rejected(self, tmp_path):
        sections = _section_with_materials("notes_a")
        client = _FakeClient(contents=sections, file_bytes=b"hello-bytes")
        outside = tmp_path / "outside"
        with patch.object(mcp_server, "_get_client", return_value=client), \
             patch.object(
                 mcp_server, "default_downloads_dir",
                 return_value=tmp_path / "root",
             ):
            result = mcp_server.download_material(
                42, "1", index=0, output_dir=str(outside),
            )

        assert result["error_code"] == "invalid_output_dir"
        assert not outside.exists()

    def test_traversal_output_dir_rejected(self, tmp_path):
        sections = _section_with_materials("notes_a")
        client = _FakeClient(contents=sections, file_bytes=b"hello-bytes")
        with patch.object(mcp_server, "_get_client", return_value=client), \
             patch.object(
                 mcp_server, "default_downloads_dir",
                 return_value=tmp_path / "root",
             ):
            result = mcp_server.download_material(
                42, "1", index=0, output_dir="../escape",
            )

        assert result["error_code"] == "invalid_output_dir"
        assert not (tmp_path / "escape").exists()


# ── extract_material tool ─────────────────────────────────────────


def _section_with_txt_material(name="notes", text_name=None):
    """Build a section holding one plain-text file material."""
    filename = text_name or f"{name}.txt"
    return [
        {
            "id": 1,
            "name": "Week 1",
            "section": 1,
            "modules": [
                {
                    "id": 10,
                    "name": "Module notes",
                    "modname": "resource",
                    "contents": [
                        {
                            "type": "file",
                            "filename": filename,
                            "fileurl": f"https://moodle.example.com/{filename}",
                            "filesize": 1024,
                            "mimetype": "text/plain",
                            "timemodified": 0,
                        },
                    ],
                },
            ],
        },
    ]


class TestExtractMaterialTool:
    def test_no_materials_returns_error_dict(self):
        client = _FakeClient(contents=[])
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.extract_material(42, "99")

        assert isinstance(result, dict)
        assert "No materials found for week '99'." in result["error"]
        assert result["candidates"] == []

    def test_ambiguous_match_returns_candidate_list(self):
        sections = _section_with_materials("notes_a", "notes_b")
        client = _FakeClient(contents=sections)
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.extract_material(42, "1")

        assert "error" in result
        assert len(result["candidates"]) == 2
        for c in result["candidates"]:
            assert "token" not in str(c).lower()
            assert "index" in c

    def test_fetch_failure_returns_error_code(self):
        sections = _section_with_txt_material()
        client = _FakeClient(contents=sections, file_bytes=None)
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.extract_material(42, "1", index=0)

        assert "Download failed" in result["error"]
        assert result["error_code"] == "empty"
        assert "candidates" not in result

    def test_successful_extract_returns_pages(self):
        sections = _section_with_txt_material()
        text = b"Study content about market equilibrium and price adjustment."
        client = _FakeClient(contents=sections, file_bytes=text)
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.extract_material(42, "1", index=0)

        assert result["filename"] == "notes.txt"
        assert result["file_type"] == "txt"
        assert result["page_count"] == 1
        assert "market equilibrium" in result["pages"][0]["text"]
        # Token/authentication details must stay out of the return shape.
        assert "file_url" not in result
        assert "token" not in str(result).lower()

    def test_nothing_written_to_disk(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        sections = _section_with_txt_material()
        client = _FakeClient(contents=sections, file_bytes=b"study text here")
        with patch.object(mcp_server, "_get_client", return_value=client):
            mcp_server.extract_material(42, "1", index=0)

        assert list(tmp_path.iterdir()) == []


# ── FastMCP registration invariants ───────────────────────────────


class TestFastMCPRegistration:
    """Lock in that every exposed tool is declared with a non-string return type
    so MCP clients receive structured content, not JSON strings to re-parse."""

    TOOL_NAMES = (
        "list_courses",
        "get_deadlines",
        "get_grades",
        "get_grade_summary",
        "get_assignments",
        "get_assignment_status",
        "get_course_forums",
        "get_forum_discussions",
        "get_latest_updates",
        "get_notifications",
        "get_messages",
        "get_digest",
        "get_calendar_events",
        "get_course_contents",
        "get_week_materials",
        "search_course_content",
        "get_weekly_summary",
        "download_material",
        "extract_material",
    )

    @pytest.mark.parametrize("name", TOOL_NAMES)
    def test_tool_return_annotation_is_not_str(self, name):
        fn = getattr(mcp_server, name)
        annot = fn.__annotations__.get("return")
        assert annot is not None, f"{name} is missing a return annotation"
        assert annot is not str, (
            f"{name} returns str; MCP tools must return native dict/list."
        )

    @pytest.mark.parametrize("name", TOOL_NAMES)
    def test_tool_is_registered_with_fastmcp(self, name):
        assert name in mcp_server.mcp._tool_manager._tools
