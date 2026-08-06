"""Tests for material selection, download, and the download CLI/MCP surface."""

import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from worsaga import secureio
from worsaga.materials import (
    MaterialSelectionError,
    _reserve_path,
    _sanitize_filename,
    candidate_summary,
    download_material,
    extract_materials,
    get_section_materials,
    sections_matching_week,
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
                    _make_file("markets_chapter.pdf", size=100000),
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


# ── sections_matching_week ──────────────────────────────────────


class TestSectionsMatchingWeek:
    def test_numeric_match(self):
        assert sections_matching_week(SAMPLE_SECTIONS, 3) == SAMPLE_SECTIONS

    def test_unmatched_numeric_is_empty(self):
        assert sections_matching_week(SAMPLE_SECTIONS, 99) == []

    def test_unmatched_string_is_empty(self):
        # An empty result is the week-not-found signal callers key on.
        assert sections_matching_week(SAMPLE_SECTIONS, "zzz_nonsense") == []

    def test_matched_but_no_files_still_matches(self):
        # Section matches by name even though it holds no downloadable files.
        empty = [{"id": 9, "name": "Week 5: Reading", "section": 5, "modules": []}]
        assert sections_matching_week(empty, 5) == empty


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
        assert result["file_name"] == "markets_chapter.pdf"

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
        assert result["file_name"] == "markets_chapter.pdf"

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

    def test_preexisting_part_file_is_never_touched(self, tmp_path):
        """A user's own <name>.part file must survive a download intact."""
        material = _materials()[0]
        sentinel = tmp_path / "week3_slides.pdf.part"
        sentinel.write_bytes(b"precious unrelated bytes")
        mock_client = MagicMock()
        mock_client.download_file.return_value = b"%PDF-new"

        result = download_material(mock_client, material, output_dir=tmp_path)

        assert sentinel.read_bytes() == b"precious unrelated bytes"
        assert Path(result["local_path"]).read_bytes() == b"%PDF-new"

    def test_no_temp_files_left_behind(self, tmp_path):
        material = _materials()[0]
        mock_client = MagicMock()
        mock_client.download_file.return_value = b"data"

        download_material(mock_client, material, output_dir=tmp_path)
        leftovers = [p.name for p in tmp_path.iterdir() if ".part" in p.name]
        assert leftovers == []

    def test_existing_file_is_not_overwritten(self, tmp_path):
        material = _materials()[0]
        existing = tmp_path / "week3_slides.pdf"
        existing.write_bytes(b"original file")
        mock_client = MagicMock()
        mock_client.download_file.return_value = b"second download"

        result = download_material(mock_client, material, output_dir=tmp_path)

        assert existing.read_bytes() == b"original file"
        assert Path(result["local_path"]).name == "week3_slides_1.pdf"
        assert Path(result["local_path"]).read_bytes() == b"second download"

    def test_default_output_dir_is_worsaga_downloads(self, tmp_path, monkeypatch):
        """No output_dir means Worsaga's downloads directory, not the CWD.

        The working directory is deliberately somewhere else here: a
        download that lands where the shell happened to be sitting is the
        behaviour this default replaced.
        """
        elsewhere = tmp_path / "somewhere-else"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        downloads = tmp_path / "worsaga-downloads"
        monkeypatch.setenv("WORSAGA_DOWNLOADS_DIR", str(downloads))
        material = _materials()[0]
        mock_client = MagicMock()
        mock_client.download_file.return_value = b"data"

        result = download_material(mock_client, material)
        assert Path(result["local_path"]).parent == downloads
        assert str(elsewhere) not in result["local_path"]

    def test_failed_write_leaves_no_partial_file(self, tmp_path):
        material = _materials()[0]
        mock_client = MagicMock()
        mock_client.download_file.return_value = b"data"

        with patch("worsaga.materials.os.replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                download_material(mock_client, material, output_dir=tmp_path)

        # Neither the final file nor the temp file may remain.
        assert list(tmp_path.iterdir()) == []

    def test_download_error_propagates_with_code(self, tmp_path):
        from worsaga.client import DownloadError

        material = _materials()[0]
        mock_client = MagicMock()
        mock_client.download_file.side_effect = DownloadError(
            "oversize", "'week3_slides.pdf' exceeds the limit; skipped.",
        )

        with pytest.raises(DownloadError) as exc_info:
            download_material(mock_client, material, output_dir=tmp_path)

        assert exc_info.value.code == "oversize"
        assert list(tmp_path.iterdir()) == []

    def test_does_not_overwrite_existing_file(self, tmp_path):
        material = _materials()[0]
        existing = tmp_path / "week3_slides.pdf"
        existing.write_bytes(b"old-data")
        mock_client = MagicMock()
        mock_client.download_file.return_value = b"new-data"

        result = download_material(mock_client, material, output_dir=tmp_path)

        assert existing.read_bytes() == b"old-data"
        assert Path(result["local_path"]).name == "week3_slides_1.pdf"
        assert Path(result["local_path"]).read_bytes() == b"new-data"


# ── No-token-leak guarantees ────────────────────────────────────


class TestDownloadPermissions:
    """Downloaded course files are personal academic material: the
    reserved placeholder and the final file are owner-only."""

    def test_reserved_placeholder_is_owner_only(self, tmp_path, monkeypatch):
        modes = []
        real_open = os.open

        def spy(path, flags, mode=0o777, **kwargs):
            modes.append(mode)
            return real_open(path, flags, mode, **kwargs)

        monkeypatch.setattr(secureio.os, "open", spy)
        _reserve_path(tmp_path / "slides.pdf")
        assert modes == [0o600]

    @pytest.mark.skipif(
        os.name == "nt", reason="POSIX permissions are not applicable on Windows"
    )
    def test_placeholder_mode_on_posix(self, tmp_path):
        path = _reserve_path(tmp_path / "slides.pdf")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    @pytest.mark.skipif(
        os.name == "nt", reason="POSIX permissions are not applicable on Windows"
    )
    def test_downloaded_file_and_new_dir_on_posix(self, tmp_path):
        nested = tmp_path / "sub"
        mock_client = MagicMock()
        mock_client.download_file.return_value = b"data"
        result = download_material(
            mock_client, _materials()[0], output_dir=nested,
        )
        assert stat.S_IMODE(Path(result["local_path"]).stat().st_mode) == 0o600
        assert stat.S_IMODE(nested.stat().st_mode) == 0o700

    @pytest.mark.skipif(
        os.name == "nt", reason="POSIX permissions are not applicable on Windows"
    )
    def test_existing_output_dir_is_left_alone(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        shared.chmod(0o755)
        mock_client = MagicMock()
        mock_client.download_file.return_value = b"data"
        download_material(mock_client, _materials()[0], output_dir=shared)
        assert stat.S_IMODE(shared.stat().st_mode) == 0o755


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
        summary = candidate_summary(material, 0)
        assert "file_url" not in summary
        json_str = json.dumps(summary)
        assert "pluginfile" not in json_str

    def test_ambiguity_error_candidates_have_no_urls(self):
        mats = _materials()
        with pytest.raises(MaterialSelectionError) as exc_info:
            select_material(mats)
        for i, c in enumerate(exc_info.value.candidates):
            summary = candidate_summary(c, i)
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


# ── candidate_summary ───────────────────────────────────────────


class TestCandidateSummary:
    def test_includes_expected_fields(self):
        material = {
            "file_name": "slides.pdf",
            "module_name": "Lecture",
            "section_name": "Week 1",
            "mime_type": "application/pdf",
            "file_size": 1024,
            "view_url": "https://example.com/view",
        }
        summary = candidate_summary(material, 2)
        assert summary["index"] == 2
        assert summary["file_name"] == "slides.pdf"
        assert summary["module_name"] == "Lecture"
        assert summary["section_name"] == "Week 1"
        assert summary["mime_type"] == "application/pdf"
        assert summary["file_size"] == 1024
        assert summary["view_url"] == "https://example.com/view"

    def test_excludes_file_url(self):
        material = {
            "file_name": "slides.pdf",
            "file_url": "https://moodle.example.com/pluginfile.php/123/slides.pdf",
        }
        summary = candidate_summary(material, 0)
        assert "file_url" not in summary


# ── CLI download command ────────────────────────────────────────


class TestCmdDownloadParser:
    def test_download_command_parses(self):
        from worsaga.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["download", "ECON101", "--week", "3"])
        assert args.command == "download"
        assert args.course == "ECON101"
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
            {"id": 42, "shortname": "ECON101", "fullname": "Economics 100"}
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
        assert result["file_name"] == "markets_chapter.pdf"

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


class TestDefaultDestinationAndRepositoryWarning:
    """Where a CLI download lands, and when Worsaga says so.

    Downloads used to land in the working directory, which on a developer
    machine is frequently a git checkout — one ``git add -A`` away from
    publishing somebody else's copyrighted teaching material.
    """

    def _mock_client(self):
        client = MagicMock()
        client.base_url = "https://moodle.example.com"
        client.get_courses.return_value = [
            {"id": 42, "shortname": "ECON101", "fullname": "Economics 100"}
        ]
        client.get_course_contents.return_value = SAMPLE_SECTIONS
        client.download_file.return_value = b"%PDF-fake"
        return client

    @patch("worsaga.cli._client")
    def test_default_lands_in_the_downloads_dir_not_the_cwd(
        self, mock_client_fn, tmp_path, monkeypatch, capsys,
    ):
        from worsaga.cli import main

        mock_client_fn.return_value = self._mock_client()
        elsewhere = tmp_path / "cwd"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        downloads = tmp_path / "worsaga-downloads"
        monkeypatch.setenv("WORSAGA_DOWNLOADS_DIR", str(downloads))

        main(["--json", "download", "42", "--week", "3", "--match", "slides"])

        result = json.loads(capsys.readouterr().out)
        assert Path(result["local_path"]).parent == downloads
        assert list(elsewhere.iterdir()) == []

    @patch("worsaga.cli._client")
    def test_output_dot_still_writes_to_the_working_directory(
        self, mock_client_fn, tmp_path, monkeypatch, capsys,
    ):
        from worsaga.cli import main

        mock_client_fn.return_value = self._mock_client()
        monkeypatch.chdir(tmp_path)

        main([
            "--json", "download", "42", "--week", "3",
            "--match", "slides", "--output", ".",
        ])

        result = json.loads(capsys.readouterr().out)
        # Resolved to an absolute path, so what is printed is actionable.
        assert Path(result["local_path"]).parent == tmp_path.resolve()

    @patch("worsaga.cli._client")
    def test_human_output_states_the_full_resolved_destination(
        self, mock_client_fn, tmp_path, monkeypatch, capsys,
    ):
        from worsaga.cli import main

        mock_client_fn.return_value = self._mock_client()
        downloads = tmp_path / "worsaga-downloads"
        monkeypatch.setenv("WORSAGA_DOWNLOADS_DIR", str(downloads))

        main(["download", "42", "--week", "3", "--match", "slides"])

        out = capsys.readouterr().out
        assert "Saved:" in out
        # The whole point of moving the default: the user has to be able
        # to find the file afterwards.
        assert str(downloads) in out


class TestGitWorktreeDetection:
    def test_plain_directory_is_not_a_worktree(self, tmp_path):
        from worsaga.cli import _git_worktree_root

        assert _git_worktree_root(tmp_path) is None

    def test_directory_with_a_git_directory(self, tmp_path):
        from worsaga.cli import _git_worktree_root

        (tmp_path / ".git").mkdir()
        nested = tmp_path / "downloads" / "econ"
        nested.mkdir(parents=True)
        assert _git_worktree_root(nested) == tmp_path.resolve()

    def test_git_file_counts_too(self, tmp_path):
        """A linked worktree or submodule has .git as a *file*."""
        from worsaga.cli import _git_worktree_root

        (tmp_path / ".git").write_text("gitdir: ../.git/worktrees/x\n")
        assert _git_worktree_root(tmp_path) == tmp_path.resolve()


class TestRepositoryWarningSurface:
    def _mock_client(self):
        client = MagicMock()
        client.base_url = "https://moodle.example.com"
        client.get_courses.return_value = [
            {"id": 42, "shortname": "ECON101", "fullname": "Economics 100"}
        ]
        client.get_course_contents.return_value = SAMPLE_SECTIONS
        client.download_file.return_value = b"%PDF-fake"
        return client

    @patch("worsaga.cli._client")
    def test_warns_when_the_destination_is_the_repository_root(
        self, mock_client_fn, tmp_path, capsys,
    ):
        from worsaga.cli import main

        mock_client_fn.return_value = self._mock_client()
        (tmp_path / ".git").mkdir()

        main([
            "download", "42", "--week", "3", "--match", "slides",
            "--output", str(tmp_path),
        ])

        err = capsys.readouterr().err
        assert "is a git repository" in err
        assert "copyrighted" in err
        # A warning, never a refusal.
        assert (tmp_path / "week3_slides.pdf").exists()

    @patch("worsaga.cli._client")
    def test_warns_when_the_destination_is_nested_in_a_repository(
        self, mock_client_fn, tmp_path, capsys,
    ):
        from worsaga.cli import main

        mock_client_fn.return_value = self._mock_client()
        (tmp_path / ".git").mkdir()
        nested = tmp_path / "downloads" / "econ"

        main([
            "download", "42", "--week", "3", "--match", "slides",
            "--output", str(nested),
        ])

        err = capsys.readouterr().err
        # Names the repository root, not just the destination, so the user
        # can see which checkout they are about to add the file to.
        assert f"inside the git repository at {tmp_path.resolve()}" in err
        assert (nested / "week3_slides.pdf").exists()

    @patch("worsaga.cli._client")
    def test_no_warning_outside_a_repository(
        self, mock_client_fn, tmp_path, capsys,
    ):
        from worsaga.cli import main

        mock_client_fn.return_value = self._mock_client()

        main([
            "download", "42", "--week", "3", "--match", "slides",
            "--output", str(tmp_path),
        ])

        assert "git repository" not in capsys.readouterr().err

    @patch("worsaga.cli._client")
    def test_quiet_suppresses_the_warning(
        self, mock_client_fn, tmp_path, capsys,
    ):
        from worsaga.cli import main

        mock_client_fn.return_value = self._mock_client()
        (tmp_path / ".git").mkdir()

        main([
            "download", "42", "--week", "3", "--match", "slides",
            "--output", str(tmp_path), "-q",
        ])

        assert "git repository" not in capsys.readouterr().err
