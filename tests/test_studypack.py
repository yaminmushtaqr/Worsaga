"""Tests for Markdown study-pack building and export."""

import json
from unittest.mock import patch

from worsaga.cli import main
from worsaga.demo import DemoMoodleClient
from worsaga.studypack import (
    build_study_pack,
    study_pack_filename,
    write_study_pack,
)


def _econ_course_id():
    for course in DemoMoodleClient().get_courses():
        if course.get("shortname") == "ECON101":
            return course["id"]
    raise AssertionError("demo dataset must include ECON101")


class TestStudyPackFilename:
    def test_basic(self):
        assert study_pack_filename("ECON101", 3) == (
            "ECON101-week-3-study-pack.md"
        )

    def test_unsafe_characters_sanitized(self):
        name = study_pack_filename("a/b:c", "rev iew?")
        assert "/" not in name and ":" not in name and "?" not in name

    def test_empty_labels(self):
        assert study_pack_filename("", "") == "course-week-week-study-pack.md"


class TestBuildStudyPack:
    def test_demo_week3(self):
        client = DemoMoodleClient()
        result = build_study_pack(client, _econ_course_id(), 3)

        assert result["course_shortname"] == "ECON101"
        assert result["files"]
        assert result["bullets"]
        assert result["suggested_filename"] == "ECON101-week-3-study-pack.md"

        markdown = result["markdown"]
        assert markdown.startswith("# ECON101: Week 3")
        assert "## Study notes" in markdown
        assert "## Materials" in markdown
        # Extracted demo content makes it into the pack body.
        assert "elasticity" in markdown.lower()
        for entry in result["files"]:
            assert entry["file_name"] in markdown

    def test_no_token_or_file_url_leak(self):
        client = DemoMoodleClient()
        result = build_study_pack(client, _econ_course_id(), 3)
        text = json.dumps(result).lower()
        assert "file_url" not in text
        assert "fileurl" not in text
        assert "wstoken" not in text
        assert "token=" not in result["markdown"].lower()

    def test_progress_callback(self):
        client = DemoMoodleClient()
        seen = []
        build_study_pack(client, _econ_course_id(), 3, on_file=seen.append)
        assert seen and all(name.endswith(".pdf") for name in seen)

    def test_empty_week_falls_back(self):
        client = DemoMoodleClient()
        result = build_study_pack(client, _econ_course_id(), "nonexistent week")
        # No section match still yields a coherent pack with fallback notes.
        assert result["bullets"]
        assert "_No downloadable materials" in result["markdown"]


class TestWriteStudyPack:
    def test_writes_utf8(self, tmp_path):
        path = write_study_pack("# T\n\nCafé — notes\n",
                                tmp_path, "pack.md")
        assert path.read_text(encoding="utf-8").startswith("# T")

    def test_never_overwrites(self, tmp_path):
        first = write_study_pack("one", tmp_path, "pack.md")
        second = write_study_pack("two", tmp_path, "pack.md")
        assert first != second
        assert first.read_text(encoding="utf-8") == "one"
        assert second.read_text(encoding="utf-8") == "two"


class TestCliSurface:
    def test_study_pack_writes_file(self, tmp_path, capsys):
        main(["--demo", "study-pack", "ECON101", "--week", "3",
              "--output", str(tmp_path), "-q"])
        out = capsys.readouterr().out
        assert "Study pack written to" in out
        written = list(tmp_path.glob("*.md"))
        assert len(written) == 1
        content = written[0].read_text(encoding="utf-8")
        assert content.startswith("# ECON101: Week 3")

    def test_study_pack_stdout(self, capsys):
        main(["--demo", "study-pack", "ECON101", "--week", "3",
              "--stdout", "-q"])
        out = capsys.readouterr().out
        assert out.startswith("# ECON101: Week 3")

    def test_study_pack_json(self, tmp_path, capsys):
        main(["--demo", "--json", "study-pack", "ECON101", "--week", "3",
              "--output", str(tmp_path)])
        payload = json.loads(capsys.readouterr().out)
        assert "markdown" not in payload  # content lives in the file
        assert payload["path"].endswith(".md")
        assert payload["files"]
        text = json.dumps(payload).lower()
        assert "file_url" not in text
        assert "wstoken" not in text


class TestMcpSurface:
    def test_export_study_pack(self, tmp_path):
        from worsaga import mcp_server

        client = DemoMoodleClient()
        with patch.object(mcp_server, "_get_client", return_value=client), \
             patch.object(mcp_server, "default_downloads_dir",
                          return_value=tmp_path):
            result = mcp_server.export_study_pack(_econ_course_id(), "3")

        assert "error" not in result
        assert "markdown" not in result
        pack = tmp_path / result["path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        assert pack.exists()
        assert pack.read_text(encoding="utf-8").startswith("# ECON101")

    def test_export_include_markdown(self, tmp_path):
        from worsaga import mcp_server

        client = DemoMoodleClient()
        with patch.object(mcp_server, "_get_client", return_value=client), \
             patch.object(mcp_server, "default_downloads_dir",
                          return_value=tmp_path):
            result = mcp_server.export_study_pack(
                _econ_course_id(), "3", include_markdown=True,
            )
        assert result["markdown"].startswith("# ECON101")

    def test_export_rejects_escape(self, tmp_path):
        from worsaga import mcp_server

        client = DemoMoodleClient()
        with patch.object(mcp_server, "_get_client", return_value=client), \
             patch.object(mcp_server, "default_downloads_dir",
                          return_value=tmp_path):
            result = mcp_server.export_study_pack(
                _econ_course_id(), "3", output_dir="..",
            )
        assert result.get("error_code") == "invalid_output_dir"
