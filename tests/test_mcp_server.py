"""Tests for worsaga's MCP server tool surface.

Verifies that every tool returns native dict/list structures rather than
JSON-encoded strings, and that error shapes for ``download_material`` are
preserved as structured dicts.
"""

import json

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
    def test_list_courses_returns_compact_records(self):
        client = _FakeClient(courses=[
            {
                "id": 1, "shortname": "ECON101", "fullname": "Econ",
                "category": 2, "startdate": 1_725_148_800,
                "enddate": 1_744_675_200,
                # Bulky raw fields that must not survive normalization.
                "summary": "<p style='x'>HTML summary</p>",
                "enrolledusercount": 214,
            },
        ])
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.list_courses()

        assert isinstance(result, list)
        assert result == [{
            "id": 1, "shortname": "ECON101", "fullname": "Econ",
            "category": 2, "start_at": 1_725_148_800, "end_at": 1_744_675_200,
        }]
        # HTML summary and enrolment counts are dropped.
        assert "summary" not in result[0]
        assert "enrolledusercount" not in result[0]

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

    def test_get_course_contents_returns_compact_sections(self):
        sections = [{
            "id": 1, "section": 1, "name": "Week 1",
            "summary": "<div><p style='x'>Intro &amp; setup.</p></div>",
            "modules": [{
                "id": 10, "name": "Lecture 1", "modname": "resource",
                "contents": [{
                    "type": "file", "filename": "w1.pdf",
                    "fileurl": "https://moodle.example.com/pluginfile.php/1/w1.pdf",
                    "filesize": 2048, "mimetype": "application/pdf",
                    "timemodified": 1700000000,
                }],
            }],
        }]
        client = _FakeClient(contents=sections)
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.get_course_contents(42)

        assert isinstance(result, list)
        section = result[0]
        assert section["section_id"] == 1
        assert section["section_num"] == 1
        assert section["section_name"] == "Week 1"
        # Section summary is HTML-stripped plain text.
        assert section["summary"] == "Intro & setup."
        module = section["modules"][0]
        assert module["module_id"] == 10
        assert module["module_type"] == "resource"
        assert "view_url" in module
        file_record = module["files"][0]
        assert file_record["file_name"] == "w1.pdf"
        assert file_record["file_size"] == 2048
        assert "dedupe_key" in file_record
        # Raw authenticated URLs never appear.
        assert "file_url" not in file_record
        assert "fileurl" not in str(result).lower()

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


# ── Week-not-found structured errors (MCP) ─────────────────────────


def _econ_demo_course_id():
    from worsaga.demo import DemoMoodleClient

    for course in DemoMoodleClient().get_courses():
        if course.get("shortname") == "ECON101":
            return course["id"]
    raise AssertionError("demo dataset must include ECON101")


class TestWeekNotFoundMcp:
    """Nonsense/unmatched weeks must return an agent-branchable structured
    error, never a fabricated success payload. Empty-but-matched stays a
    valid success. Exercised against the built-in demo dataset."""

    NONSENSE = "qwertyuiop_not_a_real_week"

    def _demo_client(self):
        from worsaga.demo import DemoMoodleClient

        return DemoMoodleClient()

    def test_get_weekly_summary_unmatched_week(self):
        course_id = _econ_demo_course_id()
        with patch.object(mcp_server, "_get_client",
                          return_value=self._demo_client()):
            result = mcp_server.get_weekly_summary(course_id, self.NONSENSE)

        assert result["error_code"] == "week_not_found"
        assert self.NONSENSE in result["error"]
        assert any("Week 3" in n for n in result["available_sections"])
        # A fabricated summary would carry bullets/method; the error must not.
        assert "bullets" not in result
        assert "method" not in result

    def test_get_weekly_summary_empty_but_valid_week(self):
        course_id = _econ_demo_course_id()
        with patch.object(mcp_server, "_get_client",
                          return_value=self._demo_client()):
            result = mcp_server.get_weekly_summary(course_id, "revision")

        assert "error" not in result
        assert result["bullets"]
        assert "Revision" in result["section_name"]

    def test_export_study_pack_unmatched_week(self, tmp_path):
        course_id = _econ_demo_course_id()
        with patch.object(mcp_server, "_get_client",
                          return_value=self._demo_client()), \
             patch.object(mcp_server, "default_downloads_dir",
                          return_value=tmp_path):
            result = mcp_server.export_study_pack(course_id, self.NONSENSE)

        assert result["error_code"] == "week_not_found"
        assert self.NONSENSE in result["error"]
        assert "path" not in result
        # Nothing may be written for an unmatched week.
        assert list(tmp_path.iterdir()) == []

    def test_get_week_materials_unmatched_week(self):
        course_id = _econ_demo_course_id()
        with patch.object(mcp_server, "_get_client",
                          return_value=self._demo_client()):
            result = mcp_server.get_week_materials(course_id, self.NONSENSE)

        assert isinstance(result, dict)
        assert result["error_code"] == "week_not_found"
        assert any("Week 3" in n for n in result["available_sections"])
        # No token/URL leakage on the error path.
        assert "token" not in str(result).lower()
        assert "fileurl" not in str(result).lower()

    def test_get_week_materials_empty_but_valid_week(self):
        course_id = _econ_demo_course_id()
        with patch.object(mcp_server, "_get_client",
                          return_value=self._demo_client()):
            result = mcp_server.get_week_materials(course_id, "revision")

        # A matched section with no downloadable files is a valid empty list.
        assert isinstance(result, list)
        assert result == []

    def test_get_week_materials_valid_week_returns_list(self):
        course_id = _econ_demo_course_id()
        with patch.object(mcp_server, "_get_client",
                          return_value=self._demo_client()):
            result = mcp_server.get_week_materials(course_id, "3")

        assert isinstance(result, list)
        assert result
        assert any(m["file_name"].endswith(".pdf") for m in result)


# ── ISSUE 1: normalization/token hygiene for list/contents tools ──────

_LEAK_TOKEN = "wstoken_SUPERSECRET_deadbeef"


class TestListCoursesNoTokenLeak:
    """list_courses must normalise the raw payload — no HTML, no course
    image URL, and above all no embedded webservice token."""

    def _raw_courses(self):
        return [{
            "id": 1, "shortname": "ECON101", "fullname": "Economics",
            "category": 2, "startdate": 1_725_148_800, "enddate": 1_744_675_200,
            "enrolledusercount": 214,
            "summary": "<div style='x'><p>HTML <b>summary</b> body</p></div>",
            "summaryformat": 1,
            # On mobile-service Moodle the course image URL embeds the token.
            "overviewfiles": [{
                "filename": "course.jpg",
                "fileurl": (
                    "https://moodle.example.com/webservice/pluginfile.php/"
                    f"1/course/overviewfiles/0/course.jpg?token={_LEAK_TOKEN}"
                ),
            }],
        }]

    def test_no_token_or_html_in_list_courses(self):
        client = _FakeClient(courses=self._raw_courses())
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.list_courses()

        dumped = json.dumps(result)
        assert _LEAK_TOKEN not in dumped
        assert "token" not in dumped.lower()
        assert "pluginfile" not in dumped
        assert "<" not in dumped  # no HTML fragments
        assert result[0]["shortname"] == "ECON101"
        assert result[0]["start_at"] == 1_725_148_800
        assert "enrolledusercount" not in result[0]


class TestCourseContentsNoTokenLeak:
    """get_course_contents must route through the sanitize boundary: a
    token-bearing fileurl in the raw payload must never reach the output."""

    def _raw_sections(self):
        return [{
            "id": 1, "section": 1, "name": "Week 1",
            "summary": "<div style='color:red'><p>Read <b>ch. 1</b></p></div>",
            "modules": [{
                "id": 10, "name": "Lecture 1 slides", "modname": "resource",
                "description": "<p>slides</p>",
                "contents": [{
                    "type": "file", "filename": "w1.pdf",
                    "fileurl": (
                        "https://moodle.example.com/webservice/pluginfile.php/"
                        f"10/mod_resource/content/1/w1.pdf?token={_LEAK_TOKEN}"
                    ),
                    "filepath": "/", "filesize": 4096,
                    "mimetype": "application/pdf", "timemodified": 1700000000,
                }],
            }],
        }]

    def test_no_token_or_fileurl_in_course_contents(self):
        client = _FakeClient(contents=self._raw_sections())
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.get_course_contents(42)

        dumped = json.dumps(result)
        assert _LEAK_TOKEN not in dumped
        assert "token" not in dumped.lower()
        assert "file_url" not in dumped
        assert "fileurl" not in dumped.lower()
        assert "<" not in dumped  # section summary is stripped to plain text
        # The compact shape is still useful.
        section = result[0]
        assert section["summary"] == "Read ch. 1"
        assert section["modules"][0]["files"][0]["file_name"] == "w1.pdf"

    def test_course_contents_much_smaller_than_raw(self):
        raw = self._raw_sections()
        client = _FakeClient(contents=raw)
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.get_course_contents(42)
        assert len(json.dumps(result)) < len(json.dumps(raw))


# ── ISSUE 2: course/assignment not-found structured errors (MCP) ──────


def _demo():
    from worsaga.demo import DemoMoodleClient

    return DemoMoodleClient()


class TestCourseNotFoundMcp:
    """Every tool taking a course/assignment id returns an agent-branchable
    structured error for a bad id, not an isError string of raw DB wording.
    Exercised against the demo dataset (course 999999 is not enrolled)."""

    BAD = 999999

    def _assert_course_not_found(self, result):
        assert isinstance(result, dict)
        assert result["error_code"] == "course_not_found"
        assert str(self.BAD) in result["error"]
        # Raw Moodle DB wording never surfaces; no token leakage.
        assert "data record" not in result["error"].lower()
        assert "token" not in str(result).lower()

    def test_get_course_contents_course_not_found(self):
        with patch.object(mcp_server, "_get_client", return_value=_demo()):
            self._assert_course_not_found(mcp_server.get_course_contents(self.BAD))

    def test_get_grades_course_not_found(self):
        with patch.object(mcp_server, "_get_client", return_value=_demo()):
            self._assert_course_not_found(mcp_server.get_grades(self.BAD))

    def test_get_grade_summary_course_not_found(self):
        with patch.object(mcp_server, "_get_client", return_value=_demo()):
            self._assert_course_not_found(mcp_server.get_grade_summary(self.BAD))

    def test_get_week_materials_course_not_found(self):
        with patch.object(mcp_server, "_get_client", return_value=_demo()):
            self._assert_course_not_found(
                mcp_server.get_week_materials(self.BAD, "3")
            )

    def test_search_course_content_course_not_found(self):
        with patch.object(mcp_server, "_get_client", return_value=_demo()):
            self._assert_course_not_found(
                mcp_server.search_course_content(self.BAD, "regression")
            )

    def test_get_weekly_summary_course_not_found(self):
        with patch.object(mcp_server, "_get_client", return_value=_demo()):
            self._assert_course_not_found(
                mcp_server.get_weekly_summary(self.BAD, "3")
            )

    def test_extract_material_course_not_found(self):
        with patch.object(mcp_server, "_get_client", return_value=_demo()):
            self._assert_course_not_found(
                mcp_server.extract_material(self.BAD, "3")
            )

    def test_download_material_course_not_found(self, tmp_path):
        with patch.object(mcp_server, "_get_client", return_value=_demo()), \
             patch.object(mcp_server, "default_downloads_dir",
                          return_value=tmp_path):
            self._assert_course_not_found(
                mcp_server.download_material(self.BAD, "3")
            )

    def test_get_calendar_events_week_course_not_found(self):
        # Week filtering fetches course contents, which surfaces a bad
        # course id as course_not_found rather than raising.
        with patch.object(mcp_server, "_get_client", return_value=_demo()):
            self._assert_course_not_found(
                mcp_server.get_calendar_events(self.BAD, week="3")
            )

    def test_get_assignment_status_assignment_not_found(self):
        course_id = _econ_demo_course_id()
        with patch.object(mcp_server, "_get_client", return_value=_demo()):
            result = mcp_server.get_assignment_status(course_id, self.BAD)
        assert result["error_code"] == "assignment_not_found"
        assert str(self.BAD) in result["error"]

    def test_error_codes_are_documented_vocabulary(self):
        assert "course_not_found" in mcp_server.ERROR_CODES
        assert "assignment_not_found" in mcp_server.ERROR_CODES
        assert "week_not_found" in mcp_server.ERROR_CODES


# ── ISSUE 3: extract_material response is deterministically bounded ────


def _section_with_big_txt(char_count):
    text = "\n".join(
        f"unique economics note number {i} on supply demand and elasticity"
        for i in range(char_count // 60 + 1)
    )
    return _section_with_txt_material(text_name="big.txt"), text.encode()


class TestExtractMaterialBounding:
    def test_markdown_omitted_by_default(self):
        sections = _section_with_txt_material()
        client = _FakeClient(contents=sections, file_bytes=b"Short study note.")
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.extract_material(42, "1", index=0)
        assert result["pages"]
        assert "markdown" not in result["pages"][0]
        assert result["pages"][0]["text"]

    def test_markdown_included_on_request(self):
        sections = _section_with_txt_material()
        client = _FakeClient(contents=sections, file_bytes=b"Short study note.")
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.extract_material(
                42, "1", index=0, include_markdown=True,
            )
        assert "markdown" in result["pages"][0]

    def test_response_bounded_default(self):
        sections, blob = _section_with_big_txt(400_000)
        client = _FakeClient(contents=sections, file_bytes=blob)
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.extract_material(42, "1", index=0)
        assert len(json.dumps(result)) <= mcp_server.MAX_EXTRACT_RESPONSE_CHARS
        # No markdown duplication on the default path.
        assert all("markdown" not in p for p in result["pages"])

    def test_response_bounded_with_markdown(self):
        sections, blob = _section_with_big_txt(400_000)
        client = _FakeClient(contents=sections, file_bytes=blob)
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.extract_material(
                42, "1", index=0, include_markdown=True,
            )
        dumped = json.dumps(result)
        assert len(dumped) <= mcp_server.MAX_EXTRACT_RESPONSE_CHARS
        assert _LEAK_TOKEN not in dumped

    def test_oversize_response_warns_about_truncation(self):
        sections, blob = _section_with_big_txt(400_000)
        client = _FakeClient(contents=sections, file_bytes=blob)
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.extract_material(
                42, "1", index=0, include_markdown=True,
            )
        assert len(json.dumps(result)) <= mcp_server.MAX_EXTRACT_RESPONSE_CHARS
        assert any("truncated" in w.lower() for w in result["warnings"])


# ── Course-argument resolution: int|str short-codes (MCP) ─────────────


class TestCourseArgResolution:
    """Course-taking tools accept a numeric id *or* a course short-code
    (exact match or unambiguous prefix), returning structured errors for
    unknown and ambiguous names — so an agent need not call list_courses
    first."""

    def _courses(self):
        return [
            {"id": 10, "shortname": "ECON101", "fullname": "Economics"},
            {"id": 20, "shortname": "PSY110", "fullname": "Psychology"},
        ]

    def _grade_payload(self):
        return {"usergrades": [{"courseid": 10, "gradeitems": [
            {"id": 1, "itemname": "Essay", "gradeformatted": "70.00",
             "percentageformatted": "70.0 %"},
        ]}]}

    def test_shortname_digit_and_numeric_id_are_identical(self):
        client = _FakeClient(
            courses=self._courses(), grade_payload=self._grade_payload(),
        )
        with patch.object(mcp_server, "_get_client", return_value=client):
            by_name = mcp_server.get_grades("ECON101")
            by_id = mcp_server.get_grades(10)
            by_digit = mcp_server.get_grades("10")
        assert by_name == by_id == by_digit
        assert by_name and by_name[0]["item_name"] == "Essay"

    def test_shortname_is_case_insensitive(self):
        client = _FakeClient(
            courses=self._courses(), grade_payload=self._grade_payload(),
        )
        with patch.object(mcp_server, "_get_client", return_value=client):
            assert mcp_server.get_grades("econ101") == mcp_server.get_grades(10)

    def test_unknown_name_returns_course_not_found(self):
        client = _FakeClient(courses=self._courses())
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.get_grades("NOTACOURSE")
        assert result["error_code"] == "course_not_found"
        assert "NOTACOURSE" in result["error"]
        assert "token" not in str(result).lower()

    def test_ambiguous_prefix_returns_candidate_list(self):
        courses = [
            {"id": 30, "shortname": "CS210_2526", "fullname": "AI 25/26"},
            {"id": 31, "shortname": "CS210_2425", "fullname": "AI 24/25"},
        ]
        with patch.object(mcp_server, "_get_client",
                          return_value=_FakeClient(courses=courses)):
            result = mcp_server.get_grades("CS210")
        assert result["error_code"] == "course_ambiguous"
        assert {c["id"] for c in result["candidates"]} == {30, 31}
        assert all(
            {"id", "shortname", "fullname"} <= set(c)
            for c in result["candidates"]
        )
        assert "ambiguous" in result["error"]

    def test_none_still_means_all_courses(self):
        client = _FakeClient(
            courses=self._courses(), grade_payload=self._grade_payload(),
        )
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.get_grades()
        assert isinstance(result, list)

    def test_resolution_shared_across_tools(self):
        client = _FakeClient(
            courses=self._courses(),
            forums_payload={"forums": [
                {"id": 5, "course": 10, "name": "Announcements"}]},
        )
        with patch.object(mcp_server, "_get_client", return_value=client):
            by_name = mcp_server.get_course_forums("ECON101")
            by_id = mcp_server.get_course_forums(10)
        assert by_name == by_id
        assert by_name and by_name[0]["forum_id"] == 5

    def test_ambiguous_code_is_documented_vocabulary(self):
        assert "course_ambiguous" in mcp_server.ERROR_CODES


# ── get_connection_info: read-only auth/site/user check (MCP) ──────────


class TestGetConnectionInfo:
    """A cheap, read-only "am I connected?" tool: correct compact shape,
    demo flag honoured, no token in the output, and auth/network failures
    surfaced as structured error dicts."""

    def test_demo_shape_and_no_token_word(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_DEMO", "1")
        with patch.object(mcp_server, "_get_client", return_value=_demo()):
            result = mcp_server.get_connection_info()
        assert result["authenticated"] is True
        assert result["demo_mode"] is True
        assert result["config_source"] == "demo"
        assert result["config_path"] is None
        assert result["user_id"] == 7
        assert result["user_display_name"] == "Demo Student"
        assert result["site_url"] == "https://moodle.demo.invalid"
        assert result["worsaga_version"]
        # No token, and no webservice path, in the base site URL.
        assert "token" not in json.dumps(result).lower()
        assert "webservice" not in result["site_url"]

    def test_no_secret_token_leaks_from_live_client(self, monkeypatch):
        monkeypatch.delenv("WORSAGA_DEMO", raising=False)

        class _TokenClient:
            is_demo = False
            base_url = "https://moodle.example.com"
            _token = _LEAK_TOKEN  # secret that must never surface

            def call(self, wsfunction, **params):
                return {
                    "sitename": "Example University", "userid": 5,
                    "fullname": "Jane Doe", "username": "jdoe",
                    "siteurl": "https://moodle.example.com",
                }

        with patch.object(mcp_server, "_get_client", return_value=_TokenClient()):
            result = mcp_server.get_connection_info()
        assert _LEAK_TOKEN not in json.dumps(result)
        assert result["authenticated"] is True
        assert result["demo_mode"] is False
        assert result["user_display_name"] == "Jane Doe"
        assert result["config_source"] in {"env", "file", "unset"}

    def test_auth_failure_returns_auth_error_code(self, monkeypatch):
        monkeypatch.delenv("WORSAGA_DEMO", raising=False)
        from worsaga.client import MoodleRequestError

        class _AuthFail:
            base_url = "https://moodle.example.com"

            def call(self, wsfunction, **params):
                raise MoodleRequestError(
                    "Moodle API error: Invalid token supplied",
                    errorcode="invalidtoken",
                )

        with patch.object(mcp_server, "_get_client", return_value=_AuthFail()):
            result = mcp_server.get_connection_info()
        assert result["error_code"] == "auth"
        assert _LEAK_TOKEN not in json.dumps(result)

    def test_network_failure_returns_network_error_code(self, monkeypatch):
        monkeypatch.delenv("WORSAGA_DEMO", raising=False)
        import urllib.error

        class _NetFail:
            base_url = "https://moodle.example.com"

            def call(self, wsfunction, **params):
                raise urllib.error.URLError("connection timed out")

        with patch.object(mcp_server, "_get_client", return_value=_NetFail()):
            result = mcp_server.get_connection_info()
        assert result["error_code"] == "network"

    def test_missing_config_returns_auth_error_code(self, monkeypatch):
        monkeypatch.delenv("WORSAGA_DEMO", raising=False)

        def _raise():
            raise ValueError("Moodle URL not configured. Run 'worsaga setup'.")

        with patch.object(mcp_server, "_get_client", side_effect=_raise):
            result = mcp_server.get_connection_info()
        assert result["error_code"] == "auth"
        assert "configured" in result["error"]


def test_module_docstring_lists_every_registered_tool():
    """The module docstring's tool enumeration must match tools/list exactly,
    so the documented surface never drifts from the registered one."""
    import re

    registered = set(mcp_server.mcp._tool_manager._tools)
    listed = set(re.findall(r"^ {4}- (\w+)$", mcp_server.__doc__ or "", re.M))
    assert listed == registered
    assert "get_connection_info" in registered
    assert len(registered) == 26
