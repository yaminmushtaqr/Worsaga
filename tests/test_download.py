"""Tests for material selection, download, and the download CLI/MCP surface."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from worsaga.materials import (
    MaterialSelectionError,
    _candidate_summary,
    _sanitize_filename,
    download_material,
    extract_materials,
    get_section_materials,
    select_material,
)


# ── Shared fixtures ─────────────────────────────────────────────


def _make_file(filename, mimetype="application/pdf", size=1024, ftype="file"):
    return {
        "type": ftype,
        "filename": filename,
        "fileurl": f"https://moodle.example.com/pluginfile.php/0/{filename}",
        "filesize": size,
        "mimetype": mimetype,
        "timemodified": 1700000000,
    }


SAMPLE_SECTIONS = [
    {
        "id": 101,
        "name": "Week 3: Markets and Pricing",
        "section": 3,
        "modules": [
            {
                "id": 2010,
                "name": "Lecture Slides",
                "modname": "resource",
                "url": "https://moodle.example.com/mod/resource/view.php?id=2010",
                "contents": [
                    _make_file("week3_slides.pdf", size=2048000),
                ],
            },
            {
                "id": 2011,
                "name": "Seminar Notes",
                "modname": "resource",
                "url": "https://moodle.example.com/mod/resource/view.php?id=2011",
                "contents": [
                    _make_file("week3_seminar.pdf", size=512000),
                ],
            },
            {
                "id": 2012,
                "name": "Extra Reading",
                "modname": "resource",
                "url": "https://moodle.example.com/mod/resource/view.php?id=2012",
                "contents": [
                    _make_file("pricing_chapter.pdf", size=100000),
                ],
            },
        ],
    },
]

COURSE_ID = 42
BASE_URL = "https://moodle.example.com"


def _materials():
    """Return a flat list of material records for week 3."""
    return get_section_materials(
        SAMPLE_SECTIONS, COURSE_ID, 3, base_url=BASE_URL,
    )


# ── select_material ─────────────────────────────────────────────


class TestSelectMaterial:
    def test_single_material_auto_selected(self):
        mats = _materials()[:1]
        result = select_material(mats)
        assert result["file_name"] == "week3_slides.pdf"

    def test_ambiguous_raises_with_candidates(self):
        mats = _materials()
        assert len(mats) == 3
        with pytest.raises(MaterialSelectionError) as exc_info:
            select_material(mats)
        assert len(exc_info.value.candidates) == 3
        assert "3 materials match" in str(exc_info.value)

    def test_match_narrows_to_one(self):
        mats = _materials()
        result = select_material(mats, match="seminar")
        assert result["file_name"] == "week3_seminar.pdf"

    def test_match_on_module_name(self):
        mats = _materials()
        result = select_material(mats, match="Extra Reading")
        assert result["file_name"] == "pricing_chapter.pdf"

    def test_match_no_results_raises(self):
        mats = _materials()
        with pytest.raises(MaterialSelectionError) as exc_info:
            select_material(mats, match="nonexistent")
        assert len(exc_info.value.candidates) == 0
        assert "No materials matching" in str(exc_info.value)

    def test_match_still_ambiguous(self):
        mats = _materials()
        # "week3" matches two files: week3_slides.pdf and week3_seminar.pdf
        with pytest.raises(MaterialSelectionError) as exc_info:
            select_material(mats, match="week3")
        assert len(exc_info.value.candidates) == 2

    def test_index_selects_directly(self):
        mats = _materials()
        result = select_material(mats, index=1)
        assert result["file_name"] == "week3_seminar.pdf"

    def test_index_zero(self):
        mats = _materials()
        result = select_material(mats, index=0)
        assert result["file_name"] == "week3_slides.pdf"

    def test_index_last(self):
        mats = _materials()
        result = select_material(mats, index=2)
        assert result["file_name"] == "pricing_chapter.pdf"

    def test_index_out_of_range_raises(self):
        mats = _materials()
        with pytest.raises(MaterialSelectionError) as exc_info:
            select_material(mats, index=99)
        assert "out of range" in str(exc_info.value)
        assert len(exc_info.value.candidates) == 3

    def test_negative_index_raises(self):
        mats = _materials()
        with pytest.raises(MaterialSelectionError):
            select_material(mats, index=-1)

    def test_match_plus_index(self):
        mats = _materials()
        # "week3" matches two files; index=1 picks the second
        result = select_material(mats, match="week3", index=1)
        assert result["file_name"] == "week3_seminar.pdf"

    def test_empty_materials_raises(self):
        with pytest.raises(MaterialSelectionError) as exc_info:
            select_material([])
        assert len(exc_info.value.candidates) == 0

    def test_match_case_insensitive(self):
        mats = _materials()
        result = select_material(mats, match="SEMINAR")
        assert result["file_name"] == "week3_seminar.pdf"


# ── download_material ───────────────────────────────────────────


class TestDownloadMaterial:
    def test_downloads_and_saves_file(self, tmp_path):
        material = _materials()[0]
        mock_client = MagicMock()
        mock_client.download_file.return_value = b"%PDF-fake-content"

        result = download_material(mock_client, material, output_dir=tmp_path)

        mock_client.download_file.assert_called_once_with(material["file_url"])
        assert result["file_name"] == "week3_slides.pdf"
        assert result["module_name"] == "Lecture Slides"
        assert result["section_name"] == "Week 3: Markets and Pricing"
        assert result["mime_type"] == "application/pdf"
        assert result["bytes_written"] == len(b"%PDF-fake-content")
        assert Path(result["local_path"]).exists()
        assert Path(result["local_path"]).read_bytes() == b"%PDF-fake-content"

    def test_returns_view_url_when_present(self, tmp_path):
        material = _materials()[0]
        assert "view_url" in material
        mock_client = MagicMock()
        mock_client.download_file.return_value = b"data"

        result = download_material(mock_client, material, output_dir=tmp_path)
        assert "view_url" in result
        assert "view.php" in result["view_url"]

    def test_no_view_url_when_absent(self, tmp_path):
        mats = extract_materials(SAMPLE_SECTIONS, COURSE_ID)  # no base_url
        material = mats[0]
        assert "view_url" not in material
        mock_client = MagicMock()
        mock_client.download_file.return_value = b"data"

        result = download_material(mock_client, material, output_dir=tmp_path)
        assert "view_url" not in result

    def test_download_failure_raises(self, tmp_path):
        material = _materials()[0]
        mock_client = MagicMock()
        mock_client.download_file.return_value = None

        with pytest.raises(RuntimeError, match="Download failed"):
            download_material(mock_client, material, output_dir=tmp_path)

    def test_no_file_url_raises(self, tmp_path):
        material = {"file_url": "", "file_name": "test.pdf"}
        mock_client = MagicMock()

        with pytest.raises(RuntimeError, match="no file_url"):
            download_material(mock_client, material, output_dir=tmp_path)

    def test_creates_output_dir(self, tmp_path):
        nested = tmp_path / "sub" / "dir"
        material = _materials()[0]
        mock_client = MagicMock()
        mock_client.download_file.return_value = b"data"

        result = download_material(mock_client, material, output_dir=nested)
        assert nested.exists()
        assert Path(result["local_path"]).exists()

    def test_default_output_dir_is_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        material = _materials()[0]
        mock_client = MagicMock()
        mock_client.download_file.return_value = b"data"

        result = download_material(mock_client, material)
        assert str(tmp_path) in result["local_path"]


# ── No-token-leak guarantees ────────────────────────────────────


class TestNoTokenLeak:
    """Verify tokens never appear in returned metadata or JSON output."""

    FAKE_TOKEN = "abc123secrettoken"

    def test_download_result_has_no_token(self, tmp_path):
        material = _materials()[0]
        mock_client = MagicMock()
        mock_client.download_file.return_value = b"data"
        mock_client._config = MagicMock()
        mock_client._config.token = self.FAKE_TOKEN

        result = download_material(mock_client, material, output_dir=tmp_path)
        result_json = json.dumps(result)
        assert self.FAKE_TOKEN not in result_json

    def test_candidate_summary_has_no_file_url(self):
        material = _materials()[0]
        material["_index"] = 0
        summary = _candidate_summary(material)
        assert "file_url" not in summary
        json_str = json.dumps(summary)
        assert "pluginfile" not in json_str

    def test_ambiguity_error_candidates_have_no_urls(self):
        mats = _materials()
        with pytest.raises(MaterialSelectionError) as exc_info:
            select_material(mats)
        for i, c in enumerate(exc_info.value.candidates):
            summary = _candidate_summary({**c, "_index": i})
            assert "file_url" not in summary

    def test_materials_json_output_does_not_get_tokenized(self):
        """The materials command should return raw file_url but NOT a tokenized URL."""
        mats = _materials()
        for m in mats:
            # file_url should be the raw URL, not containing token= param
            assert "token=" not in m.get("file_url", "")


# ── _sanitize_filename ──────────────────────────────────────────


class TestSanitizeFilename:
    def test_normal_filename_unchanged(self):
        assert _sanitize_filename("slides.pdf") == "slides.pdf"

    def test_spaces_replaced(self):
        assert _sanitize_filename("my file.pdf") == "my_file.pdf"

    def test_special_chars_replaced(self):
        result = _sanitize_filename("file@#$.pdf")
        assert "@" not in result
        assert "#" not in result
        assert "$" not in result
        assert result.endswith(".pdf")

    def test_preserves_hyphens_and_underscores(self):
        assert _sanitize_filename("my-file_v2.pdf") == "my-file_v2.pdf"


# ── _candidate_summary ──────────────────────────────────────────


class TestCandidateSummary:
    def test_includes_expected_fields(self):
        material = {
            "_index": 2,
            "file_name": "slides.pdf",
            "module_name": "Lecture",
            "section_name": "Week 1",
            "mime_type": "application/pdf",
            "file_size": 1024,
            "view_url": "https://example.com/view",
        }
        summary = _candidate_summary(material)
        assert summary["index"] == 2
        assert summary["file_name"] == "slides.pdf"
        assert summary["module_name"] == "Lecture"
        assert summary["section_name"] == "Week 1"
        assert summary["mime_type"] == "application/pdf"
        assert summary["file_size"] == 1024
        assert summary["view_url"] == "https://example.com/view"

    def test_excludes_file_url(self):
        material = {
            "_index": 0,
            "file_name": "slides.pdf",
            "file_url": "https://moodle.example.com/pluginfile.php/123/slides.pdf",
        }
        summary = _candidate_summary(material)
        assert "file_url" not in summary


# ── CLI download command ────────────────────────────────────────


class TestCmdDownloadParser:
    def test_download_command_parses(self):
        from worsaga.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["download", "EC100", "--week", "3"])
        assert args.command == "download"
        assert args.course == "EC100"
        assert args.week == "3"
        assert args.match is None
        assert args.index is None
        assert args.output is None

    def test_download_with_all_options(self):
        from worsaga.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args([
            "download", "42", "--week", "3",
            "--match", "slides", "--index", "0",
            "--output", "/tmp/downloads",
        ])
        assert args.course == "42"
        assert args.week == "3"
        assert args.match == "slides"
        assert args.index == 0
        assert args.output == "/tmp/downloads"


class TestCmdDownloadExecution:
    """Test cmd_download with mocked client."""

    def _mock_client(self):
        client = MagicMock()
        client.base_url = "https://moodle.example.com"
        client.get_courses.return_value = [
            {"id": 42, "shortname": "EC100", "fullname": "Economics 100"}
        ]
        client.get_course_contents.return_value = SAMPLE_SECTIONS
        client.download_file.return_value = b"%PDF-fake"
        return client

    @patch("worsaga.cli._client")
    def test_download_single_match_json(self, mock_client_fn, tmp_path, capsys):
        client = self._mock_client()
        mock_client_fn.return_value = client

        from worsaga.cli import main

        main([
            "--json", "download", "42", "--week", "3",
            "--match", "seminar", "--output", str(tmp_path),
        ])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result["file_name"] == "week3_seminar.pdf"
        assert result["module_name"] == "Seminar Notes"
        assert "local_path" in result
        assert "token" not in captured.out.lower()

    @patch("worsaga.cli._client")
    def test_download_ambiguous_json(self, mock_client_fn, capsys):
        client = self._mock_client()
        mock_client_fn.return_value = client

        from worsaga.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["--json", "download", "42", "--week", "3"])
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert "error" in result
        assert "candidates" in result
        assert len(result["candidates"]) == 3
        # Candidates should not contain file_url
        for c in result["candidates"]:
            assert "file_url" not in c

    @patch("worsaga.cli._client")
    def test_download_by_index_json(self, mock_client_fn, tmp_path, capsys):
        client = self._mock_client()
        mock_client_fn.return_value = client

        from worsaga.cli import main

        main([
            "--json", "download", "42", "--week", "3",
            "--index", "2", "--output", str(tmp_path),
        ])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result["file_name"] == "pricing_chapter.pdf"

    @patch("worsaga.cli._client")
    def test_download_no_materials_json(self, mock_client_fn, capsys):
        client = self._mock_client()
        client.get_course_contents.return_value = [
            {"id": 200, "name": "Week 99: Empty", "section": 99, "modules": []},
        ]
        mock_client_fn.return_value = client

        from worsaga.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["--json", "download", "42", "--week", "99"])
        assert exc_info.value.code == 1

    @patch("worsaga.cli._client")
    def test_download_human_output(self, mock_client_fn, tmp_path, capsys):
        client = self._mock_client()
        mock_client_fn.return_value = client

        from worsaga.cli import main

        main([
            "download", "42", "--week", "3",
            "--match", "slides", "--output", str(tmp_path),
        ])

        captured = capsys.readouterr()
        assert "Saved:" in captured.out
        assert "week3_slides.pdf" in captured.out
