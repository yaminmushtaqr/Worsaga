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
    ):
        self._courses = courses or []
        self._contents = contents or []
        self._file_bytes = file_bytes

    def get_courses(self):
        return self._courses

    def get_course_contents(self, course_id):
        return self._contents

    def download_file(self, fileurl, *, max_bytes=None):
        return self._file_bytes


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
            {"id": 1, "shortname": "EC100", "fullname": "Econ"},
        ])
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.list_courses()

        assert isinstance(result, list)
        assert result == [{"id": 1, "shortname": "EC100", "fullname": "Econ"}]

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
        assert "file_url" in result[0]
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
        with patch.object(mcp_server, "_get_client", return_value=client):
            result = mcp_server.download_material(
                42, "1", index=0, output_dir=str(tmp_path),
            )

        assert isinstance(result, dict)
        assert result["file_name"] == "notes_a.pdf"
        assert result["bytes_written"] == len(b"hello-bytes")
        assert result["local_path"].endswith("notes_a.pdf")
        # Token/authentication details must stay out of the return shape.
        assert "token" not in result
        assert "fileurl" not in result


# ── FastMCP registration invariants ───────────────────────────────


class TestFastMCPRegistration:
    """Lock in that every exposed tool is declared with a non-string return type
    so MCP clients receive structured content, not JSON strings to re-parse."""

    TOOL_NAMES = (
        "list_courses",
        "get_deadlines",
        "get_course_contents",
        "get_week_materials",
        "search_course_content",
        "get_weekly_summary",
        "download_material",
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
