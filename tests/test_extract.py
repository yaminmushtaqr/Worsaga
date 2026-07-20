"""Tests for extract_material_content and the `worsaga extract` CLI command."""

import json
from unittest.mock import MagicMock, patch

import pytest

from worsaga.client import DownloadError
from worsaga.materials import extract_material_content, get_section_materials


# ── Shared fixtures ─────────────────────────────────────────────


def _make_file(filename, mimetype="text/plain", size=1024):
    return {
        "type": "file",
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
                "name": "Lecture Notes",
                "modname": "resource",
                "url": "https://moodle.example.com/mod/resource/view.php?id=2010",
                "contents": [_make_file("week3_notes.txt")],
            },
            {
                "id": 2011,
                "name": "Seminar Notes",
                "modname": "resource",
                "url": "https://moodle.example.com/mod/resource/view.php?id=2011",
                "contents": [_make_file("week3_seminar.txt")],
            },
        ],
    },
]

COURSE_ID = 42
BASE_URL = "https://moodle.example.com"

SAMPLE_TEXT = (
    "Market equilibrium balances supply and demand over time.\n"
    "Prices adjust until the quantity supplied equals the quantity demanded.\n"
)


def _materials():
    return get_section_materials(
        SAMPLE_SECTIONS, COURSE_ID, 3, base_url=BASE_URL,
    )


def _mock_client(file_bytes=SAMPLE_TEXT.encode()):
    client = MagicMock()
    client.base_url = BASE_URL
    client.get_course_contents.return_value = SAMPLE_SECTIONS
    client.download_file.return_value = file_bytes
    return client


# ── extract_material_content ────────────────────────────────────


class TestExtractMaterialContent:
    def test_extracts_txt_material(self):
        material = _materials()[0]
        client = _mock_client()

        result = extract_material_content(client, material)

        client.download_file.assert_called_once_with(material["file_url"])
        assert result["filename"] == "week3_notes.txt"
        assert result["file_type"] == "txt"
        assert result["page_count"] == 1
        assert len(result["pages"]) == 1
        assert "Market equilibrium" in result["pages"][0]["text"]

    def test_result_carries_material_context(self):
        material = _materials()[0]
        result = extract_material_content(_mock_client(), material)

        assert result["course_id"] == COURSE_ID
        assert result["section_name"] == "Week 3: Markets and Pricing"
        assert result["module_name"] == "Lecture Notes"
        assert result["mime_type"] == "text/plain"
        assert result["file_size"] == 1024
        assert "view.php" in result["view_url"]

    def test_pdf_produces_per_page_entries(self):
        from worsaga.demo import _render_pdf

        line = "Substantive lecture content about market equilibrium. "
        pdf = _render_pdf([[line * 5], [line * 5]])
        material = dict(_materials()[0], file_name="week3_slides.pdf")

        result = extract_material_content(_mock_client(pdf), material)

        assert result["file_type"] == "pdf"
        assert result["page_count"] == 2
        assert [p["page"] for p in result["pages"]] == [1, 2]
        assert all(p["markdown"] for p in result["pages"])

    def test_no_file_url_raises_invalid_url(self):
        with pytest.raises(DownloadError) as exc_info:
            extract_material_content(
                _mock_client(), {"file_url": "", "file_name": "x.txt"},
            )
        assert exc_info.value.code == "invalid_url"

    def test_none_bytes_raises_empty(self):
        material = _materials()[0]
        with pytest.raises(DownloadError) as exc_info:
            extract_material_content(_mock_client(None), material)
        assert exc_info.value.code == "empty"

    def test_download_error_propagates(self):
        material = _materials()[0]
        client = _mock_client()
        client.download_file.side_effect = DownloadError(
            "oversize", "'week3_notes.txt' exceeds the limit; skipped.",
        )
        with pytest.raises(DownloadError) as exc_info:
            extract_material_content(client, material)
        assert exc_info.value.code == "oversize"

    def test_clean_default_strips_noise_keeps_captions(self):
        text = (
            "Figure 1: Supply and demand curves\n"
            "Page 3\n"
            "Prices adjust until markets clear across every trading period.\n"
        )
        material = _materials()[0]
        result = extract_material_content(_mock_client(text.encode()), material)

        page_text = result["pages"][0]["text"]
        assert "Figure 1" in page_text  # educational content preserved
        assert "Page 3" not in page_text  # boilerplate stripped

    def test_clean_false_returns_raw_text(self):
        text = "Page 3\nActual content line for the reader to study.\n"
        material = _materials()[0]
        result = extract_material_content(
            _mock_client(text.encode()), material, clean=False,
        )
        assert "Page 3" in result["pages"][0]["text"]

    def test_max_chars_truncates_with_warning(self):
        material = _materials()[0]
        result = extract_material_content(
            _mock_client(), material, max_chars=20,
        )
        assert len(result["pages"][0]["text"]) <= 20
        assert any("truncated" in w for w in result["warnings"])

    def test_no_token_or_file_url_in_result(self):
        material = _materials()[0]
        result = extract_material_content(_mock_client(), material)
        dumped = json.dumps(result)
        assert "file_url" not in result
        assert "pluginfile" not in dumped
        assert "token" not in dumped.lower()


# ── CLI parser ──────────────────────────────────────────────────


class TestCmdExtractParser:
    def test_extract_command_parses(self):
        from worsaga.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["extract", "ECON101", "--week", "3"])
        assert args.command == "extract"
        assert args.course == "ECON101"
        assert args.week == "3"
        assert args.match is None
        assert args.index is None
        assert args.raw is False
        assert args.max_chars is None

    def test_extract_with_all_options(self):
        from worsaga.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args([
            "extract", "42", "--week", "3",
            "--match", "notes", "--index", "0",
            "--raw", "--max-chars", "5000",
        ])
        assert args.match == "notes"
        assert args.index == 0
        assert args.raw is True
        assert args.max_chars == 5000


# ── CLI execution ───────────────────────────────────────────────


class TestCmdExtractExecution:
    @patch("worsaga.cli._client")
    def test_extract_single_match_json(self, mock_client_fn, capsys):
        mock_client_fn.return_value = _mock_client()

        from worsaga.cli import main

        main(["--json", "extract", "42", "--week", "3", "--match", "Lecture"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result["filename"] == "week3_notes.txt"
        assert result["page_count"] == 1
        assert "Market equilibrium" in result["pages"][0]["text"]
        assert "file_url" not in result
        assert "token" not in captured.out.lower()

    @patch("worsaga.cli._client")
    def test_extract_ambiguous_json(self, mock_client_fn, capsys):
        mock_client_fn.return_value = _mock_client()

        from worsaga.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["--json", "extract", "42", "--week", "3"])
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert "error" in result
        assert len(result["candidates"]) == 2
        for c in result["candidates"]:
            assert "file_url" not in c

    @patch("worsaga.cli._client")
    def test_extract_no_materials_json(self, mock_client_fn, capsys):
        client = _mock_client()
        client.get_course_contents.return_value = [
            {"id": 200, "name": "Week 99: Empty", "section": 99, "modules": []},
        ]
        mock_client_fn.return_value = client

        from worsaga.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["--json", "extract", "42", "--week", "99"])
        assert exc_info.value.code == 1

    @patch("worsaga.cli._client")
    def test_extract_download_error_json(self, mock_client_fn, capsys):
        client = _mock_client()
        client.download_file.side_effect = DownloadError(
            "oversize", "'week3_notes.txt' exceeds the limit; skipped.",
        )
        mock_client_fn.return_value = client

        from worsaga.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["--json", "extract", "42", "--week", "3", "--index", "0"])
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result["error_code"] == "oversize"

    @patch("worsaga.cli._client")
    def test_extract_human_output(self, mock_client_fn, capsys):
        mock_client_fn.return_value = _mock_client()

        from worsaga.cli import main

        main(["extract", "42", "--week", "3", "--index", "0", "-q"])

        captured = capsys.readouterr()
        assert "# week3_notes.txt - Week 3: Markets and Pricing" in captured.out
        assert "--- Page 1 ---" in captured.out
        assert "Market equilibrium" in captured.out


# ── Demo mode end-to-end ────────────────────────────────────────


class TestDemoExtract:
    def test_demo_extract_json(self, capsys):
        from worsaga.cli import main

        main([
            "--demo", "--json", "extract", "ECON101",
            "--week", "3", "--index", "0",
        ])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert "error" not in result
        assert result["page_count"] >= 1
        assert result["pages"][0]["text"]
        assert "file_url" not in result
