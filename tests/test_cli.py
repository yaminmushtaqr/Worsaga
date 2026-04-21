"""Tests for the worsaga CLI."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from worsaga.cli import (
    CourseResolutionError,
    _build_parser,
    _normalize_contents,
    _normalize_courses,
    _resolve_course_id,
    main,
)


class TestParser:
    def test_courses_command(self):
        parser = _build_parser()
        args = parser.parse_args(["courses"])
        assert args.command == "courses"

    def test_deadlines_default_days(self):
        parser = _build_parser()
        args = parser.parse_args(["deadlines"])
        assert args.command == "deadlines"
        assert args.days == 14

    def test_deadlines_custom_days(self):
        parser = _build_parser()
        args = parser.parse_args(["deadlines", "--days", "7"])
        assert args.days == 7

    def test_contents_with_id(self):
        parser = _build_parser()
        args = parser.parse_args(["contents", "123"])
        assert args.command == "contents"
        assert args.course == "123"

    def test_contents_with_code(self):
        parser = _build_parser()
        args = parser.parse_args(["contents", "EC100"])
        assert args.course == "EC100"

    def test_materials_command(self):
        parser = _build_parser()
        args = parser.parse_args(["materials", "EC100"])
        assert args.command == "materials"
        assert args.course == "EC100"
        assert args.week is None

    def test_materials_with_week(self):
        parser = _build_parser()
        args = parser.parse_args(["materials", "EC100", "--week", "3"])
        assert args.week == "3"

    def test_summary_command(self):
        parser = _build_parser()
        args = parser.parse_args(["summary", "EC100", "--week", "3"])
        assert args.command == "summary"
        assert args.course == "EC100"
        assert args.week == "3"

    def test_summary_string_week(self):
        parser = _build_parser()
        args = parser.parse_args(["summary", "EC100", "--week", "revision"])
        assert args.week == "revision"

    def test_setup_command(self):
        parser = _build_parser()
        args = parser.parse_args(["setup"])
        assert args.command == "setup"

    def test_update_command(self):
        parser = _build_parser()
        args = parser.parse_args(["update"])
        assert args.command == "update"

    def test_setup_noninteractive_flags(self):
        parser = _build_parser()
        args = parser.parse_args([
            "setup", "--url", "https://m.example.com",
            "--token", "tok123", "--userid", "42",
        ])
        assert args.command == "setup"
        assert args.setup_url == "https://m.example.com"
        assert args.setup_token == "tok123"
        assert args.setup_userid == 42

    def test_toplevel_creds_before_setup(self):
        """Top-level --url/--token/--userid must survive into setup."""
        parser = _build_parser()
        args = parser.parse_args([
            "--url", "https://m.example.com",
            "--token", "tok",
            "--userid", "7",
            "setup",
        ])
        assert args.command == "setup"
        assert args.url == "https://m.example.com"
        assert args.token == "tok"
        assert args.userid == 7

    def test_setup_subcommand_creds_after_setup(self):
        """setup --url/--token/--userid should also parse correctly."""
        parser = _build_parser()
        args = parser.parse_args([
            "setup", "--url", "https://m.example.com",
            "--token", "tok", "--userid", "7",
        ])
        assert args.command == "setup"
        assert args.setup_url == "https://m.example.com"
        assert args.setup_token == "tok"
        assert args.setup_userid == 7
        # top-level values should be None (not provided)
        assert args.url is None
        assert args.token is None
        assert args.userid is None

    def test_json_flag(self):
        parser = _build_parser()
        args = parser.parse_args(["--json", "courses"])
        assert args.json is True

    def test_toplevel_credential_overrides(self):
        parser = _build_parser()
        args = parser.parse_args([
            "--url", "https://m.example.com",
            "--token", "tok",
            "--userid", "7",
            "--creds-path", "/tmp/creds.json",
            "courses",
        ])
        assert args.url == "https://m.example.com"
        assert args.token == "tok"
        assert args.userid == 7
        assert args.creds_path == "/tmp/creds.json"
        assert args.command == "courses"

    def test_contents_with_week(self):
        parser = _build_parser()
        args = parser.parse_args(["contents", "EC100", "--week", "3"])
        assert args.week == "3"

    def test_contents_with_string_week(self):
        parser = _build_parser()
        args = parser.parse_args(["contents", "EC100", "--week", "revision"])
        assert args.week == "revision"

    def test_no_command_shows_help(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code == 0

    def test_version_flag(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "worsaga" in out

    def test_version_flag_short(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["-V"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "worsaga" in out

    def test_setup_help_metavars(self, capsys):
        """Setup subparser should show clean metavars, not dest names."""
        import io
        with pytest.raises(SystemExit):
            main(["setup", "--help"])
        out = capsys.readouterr().out
        # Should show clean metavar names, not SETUP_URL etc.
        assert "--url URL" in out
        assert "--token TOKEN" in out
        assert "--userid ID" in out
        assert "SETUP_URL" not in out
        assert "SETUP_TOKEN" not in out
        assert "SETUP_USERID" not in out

    def test_subcommand_help_shows_json_description(self, capsys):
        """--json in subcommand --help must show its help text."""
        with pytest.raises(SystemExit):
            main(["setup", "--help"])
        out = capsys.readouterr().out
        assert "Output machine-readable JSON" in out

    def test_subcommand_help_shows_quiet_description(self, capsys):
        """--quiet in subcommand --help must show its help text."""
        with pytest.raises(SystemExit):
            main(["setup", "--help"])
        out = capsys.readouterr().out
        assert "Suppress progress output on stderr" in out

    def test_version_output_contains_correct_version(self, capsys):
        """--version must print the version from __version__."""
        from worsaga import __version__
        with pytest.raises(SystemExit):
            main(["--version"])
        out = capsys.readouterr().out
        assert __version__ in out

    def test_materials_help_references_download(self, capsys):
        """materials help text must point agents toward the download command."""
        with pytest.raises(SystemExit):
            main(["materials", "--help"])
        out = capsys.readouterr().out
        assert "download" in out.lower()

    def test_top_level_help_lists_download(self, capsys):
        """Top-level --help must list the download subcommand."""
        with pytest.raises(SystemExit):
            main(["--help"])
        out = capsys.readouterr().out
        assert "download" in out

    def test_download_help_references_materials(self, capsys):
        """download --help must point agents to 'materials' for discovery."""
        with pytest.raises(SystemExit):
            main(["download", "--help"])
        out = capsys.readouterr().out
        assert "materials" in out.lower()

    def test_materials_help_warns_file_url(self, capsys):
        """materials --help must warn that file_url is not for direct fetch."""
        with pytest.raises(SystemExit):
            main(["materials", "--help"])
        out = capsys.readouterr().out
        assert "file_url" in out
        assert "provenance" in out.lower()


class TestUpdateCommand:
    @patch("worsaga.cli._upgrade_command", return_value="python3 -m pip install --upgrade worsaga")
    @patch("worsaga.cli._get_latest_pypi_version", return_value="0.2.3")
    def test_update_human_output_when_update_available(self, mock_latest, mock_upgrade, capsys):
        from worsaga import __version__
        main(["update"])
        out = capsys.readouterr().out
        assert f"Current version: {__version__}" in out
        assert "Latest PyPI version: 0.2.3" in out
        assert "An update is available" in out
        assert "python3 -m pip install --upgrade worsaga" in out

    @patch("worsaga.cli._upgrade_command", return_value="py -m pip install --upgrade worsaga")
    def test_update_human_output_when_current(self, mock_upgrade, capsys):
        from worsaga import __version__
        with patch("worsaga.cli._get_latest_pypi_version", return_value=__version__):
            main(["update"])
        out = capsys.readouterr().out
        assert f"Latest PyPI version: {__version__}" in out
        assert "To upgrade or reinstall, run:" in out
        assert "py -m pip install --upgrade worsaga" in out

    @patch("worsaga.cli._get_latest_pypi_version", return_value="0.0.9")
    def test_update_human_output_when_ahead_of_pypi(self, mock_latest, capsys):
        main(["update"])
        out = capsys.readouterr().out
        assert "Latest PyPI version: 0.0.9" in out
        assert "newer than the latest PyPI release" in out
        assert "An update is available" not in out

    @patch("worsaga.cli._upgrade_command", return_value="python3 -m pip install --upgrade worsaga")
    @patch("worsaga.cli._get_latest_pypi_version", return_value=None)
    def test_update_human_output_without_pypi(self, mock_latest, mock_upgrade, capsys):
        main(["update"])
        out = capsys.readouterr().out
        assert "Latest PyPI version: unavailable" in out
        assert "python3 -m pip install --upgrade worsaga" in out

    @patch("worsaga.cli._upgrade_command", return_value="python3 -m pip install --upgrade worsaga")
    @patch("worsaga.cli._get_latest_pypi_version", return_value="0.2.3")
    def test_update_json_output(self, mock_latest, mock_upgrade, capsys):
        from worsaga import __version__
        main(["--json", "update"])
        out = json.loads(capsys.readouterr().out)
        assert out["current_version"] == __version__
        assert out["latest_version"] == "0.2.3"
        assert out["update_available"] is True
        assert out["ahead_of_pypi"] is False
        assert out["upgrade_command"] == "python3 -m pip install --upgrade worsaga"


class TestResolveCourseId:
    def _mock_client(self, courses):
        """Return a mock client whose get_courses() returns the given list."""
        from unittest.mock import MagicMock
        client = MagicMock()
        client.get_courses.return_value = courses
        return client

    def test_integer_id_returned_directly(self):
        client = self._mock_client([])
        assert _resolve_course_id(client, "42") == 42
        client.get_courses.assert_not_called()

    def test_shortcode_lookup(self):
        client = self._mock_client([
            {"id": 10, "shortname": "EC100"},
            {"id": 20, "shortname": "MA100"},
        ])
        assert _resolve_course_id(client, "ec100") == 10  # case-insensitive

    def test_prefix_match_underscore(self):
        """MG488 should resolve to MG488_2526 when it's the only prefix match."""
        client = self._mock_client([
            {"id": 10, "shortname": "EC100"},
            {"id": 30, "shortname": "MG488_2526"},
        ])
        assert _resolve_course_id(client, "MG488") == 30

    def test_prefix_match_hyphen(self):
        """Prefix matching should also work with hyphen separators."""
        client = self._mock_client([
            {"id": 40, "shortname": "ST200-2526"},
        ])
        assert _resolve_course_id(client, "st200") == 40  # case-insensitive

    def test_exact_match_preferred_over_prefix(self):
        """If both an exact and prefix match exist, exact wins."""
        client = self._mock_client([
            {"id": 10, "shortname": "EC100"},
            {"id": 20, "shortname": "EC100_2526"},
        ])
        assert _resolve_course_id(client, "EC100") == 10

    def test_ambiguous_prefix_raises(self):
        """Multiple courses sharing the same prefix should raise CourseResolutionError."""
        client = self._mock_client([
            {"id": 30, "shortname": "MG488_2526"},
            {"id": 31, "shortname": "MG488_2425"},
        ])
        with pytest.raises(CourseResolutionError):
            _resolve_course_id(client, "MG488")

    def test_ambiguous_prefix_error_message(self):
        """Ambiguous prefix error should list the conflicting courses."""
        client = self._mock_client([
            {"id": 30, "shortname": "MG488_2526"},
            {"id": 31, "shortname": "MG488_2425"},
        ])
        with pytest.raises(CourseResolutionError, match="ambiguous"):
            _resolve_course_id(client, "MG488")
        # Verify both shortnames appear in the exception message
        try:
            _resolve_course_id(client, "MG488")
        except CourseResolutionError as e:
            msg = str(e)
            assert "MG488_2526" in msg
            assert "MG488_2425" in msg

    def test_unknown_code_raises(self):
        client = self._mock_client([
            {"id": 10, "shortname": "EC100"},
        ])
        with pytest.raises(CourseResolutionError, match="no enrolled course"):
            _resolve_course_id(client, "NONEXISTENT")

    def test_unknown_code_caught_by_main(self, capsys):
        """CourseResolutionError from _resolve_course_id should exit 1 via main()."""
        with patch("worsaga.cli._client") as mock_client_fn:
            mock = mock_client_fn.return_value
            mock.get_courses.return_value = [{"id": 10, "shortname": "EC100"}]
            with pytest.raises(SystemExit) as exc:
                main(["contents", "NONEXISTENT"])
            assert exc.value.code == 1
            err = capsys.readouterr().err
            assert "no enrolled course" in err


class TestCommandOutput:
    FAKE_COURSES = [
        {"id": 1, "shortname": "EC100", "fullname": "Economics 100"},
        {"id": 2, "shortname": "MA100", "fullname": "Mathematics 100"},
    ]

    @patch("worsaga.cli._client")
    def test_courses_json(self, mock_client_fn, capsys):
        mock_client_fn.return_value.get_courses.return_value = self.FAKE_COURSES
        main(["--json", "courses"])
        output = json.loads(capsys.readouterr().out)
        assert len(output) == 2
        assert output[0]["shortname"] == "EC100"

    @patch("worsaga.cli._client")
    def test_courses_table(self, mock_client_fn, capsys):
        mock_client_fn.return_value.get_courses.return_value = self.FAKE_COURSES
        main(["courses"])
        out = capsys.readouterr().out
        assert "EC100" in out
        assert "MA100" in out

    @patch("worsaga.cli._client")
    def test_deadlines_empty(self, mock_client_fn, capsys):
        mock_client_fn.return_value.get_courses.return_value = []
        main(["deadlines"])
        out = capsys.readouterr().out
        assert "No deadlines" in out

    @patch("worsaga.cli._client")
    def test_contents_json(self, mock_client_fn, capsys):
        sections = [
            {"name": "Week 1", "modules": [{"name": "Lecture Notes", "modname": "resource"}]},
        ]
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "EC100"}]
        mock.get_course_contents.return_value = sections
        main(["--json", "contents", "1"])
        output = json.loads(capsys.readouterr().out)
        assert output[0]["name"] == "Week 1"

    @patch("worsaga.cli._client")
    def test_contents_by_code(self, mock_client_fn, capsys):
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 5, "shortname": "ST100"}]
        mock.get_course_contents.return_value = [
            {"name": "Overview", "modules": [{"name": "Syllabus", "modname": "page"}]},
        ]
        main(["contents", "ST100"])
        out = capsys.readouterr().out
        assert "Overview" in out
        assert "Syllabus" in out

    @patch("worsaga.cli._client")
    def test_materials_json(self, mock_client_fn, capsys):
        sections = [
            {
                "id": 1, "name": "Week 1", "section": 1,
                "modules": [{
                    "id": 10, "name": "Slides", "modname": "resource",
                    "contents": [{
                        "type": "file", "filename": "slides.pdf",
                        "fileurl": "https://example.com/slides.pdf",
                        "filesize": 1024, "mimetype": "application/pdf",
                        "timemodified": 1700000000,
                    }],
                }],
            },
        ]
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "EC100"}]
        mock.get_course_contents.return_value = sections
        main(["--json", "materials", "1"])
        output = json.loads(capsys.readouterr().out)
        assert len(output) == 1
        assert output[0]["file_name"] == "slides.pdf"

    @patch("worsaga.cli._client")
    def test_materials_table(self, mock_client_fn, capsys):
        sections = [
            {
                "id": 1, "name": "Week 1", "section": 1,
                "modules": [{
                    "id": 10, "name": "Slides", "modname": "resource",
                    "contents": [{
                        "type": "file", "filename": "slides.pdf",
                        "fileurl": "https://example.com/slides.pdf",
                        "filesize": 2097152, "mimetype": "application/pdf",
                        "timemodified": 1700000000,
                    }],
                }],
            },
        ]
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "EC100"}]
        mock.get_course_contents.return_value = sections
        main(["materials", "1"])
        out = capsys.readouterr().out
        assert "slides.pdf" in out
        assert "2.0 MB" in out

    @patch("worsaga.cli._client")
    def test_materials_with_week_filter(self, mock_client_fn, capsys):
        sections = [
            {"id": 1, "name": "Week 1", "section": 1, "modules": [{
                "id": 10, "name": "W1 Slides", "modname": "resource",
                "contents": [{"type": "file", "filename": "w1.pdf",
                              "fileurl": "", "filesize": 100,
                              "mimetype": "application/pdf", "timemodified": 0}],
            }]},
            {"id": 2, "name": "Week 2", "section": 2, "modules": [{
                "id": 20, "name": "W2 Slides", "modname": "resource",
                "contents": [{"type": "file", "filename": "w2.pdf",
                              "fileurl": "", "filesize": 200,
                              "mimetype": "application/pdf", "timemodified": 0}],
            }]},
        ]
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "EC100"}]
        mock.get_course_contents.return_value = sections
        main(["--json", "materials", "1", "--week", "1"])
        output = json.loads(capsys.readouterr().out)
        assert len(output) == 1
        assert output[0]["file_name"] == "w1.pdf"

    @patch("worsaga.cli._client")
    def test_materials_empty(self, mock_client_fn, capsys):
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "EC100"}]
        mock.get_course_contents.return_value = []
        main(["materials", "1"])
        out = capsys.readouterr().out
        assert "No materials found" in out

    @patch("worsaga.cli._client")
    def test_contents_week_filter_numeric(self, mock_client_fn, capsys):
        """contents --week 1 should only show matching sections."""
        sections = [
            {"name": "Week 1 — Intro", "modules": [
                {"name": "Lecture Notes", "modname": "resource"},
            ]},
            {"name": "Week 2 — Data", "modules": [
                {"name": "Lab Sheet", "modname": "resource"},
            ]},
        ]
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "EC100"}]
        mock.get_course_contents.return_value = sections
        main(["contents", "1", "--week", "1"])
        out = capsys.readouterr().out
        assert "Week 1" in out
        assert "Week 2" not in out

    @patch("worsaga.cli._client")
    def test_contents_week_filter_string(self, mock_client_fn, capsys):
        """contents --week revision should match by substring."""
        sections = [
            {"name": "Week 10 — Revision Session", "modules": [
                {"name": "Review Slides", "modname": "resource"},
            ]},
            {"name": "Week 3 — Methods", "modules": [
                {"name": "Lecture Notes", "modname": "resource"},
            ]},
        ]
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "EC100"}]
        mock.get_course_contents.return_value = sections
        main(["contents", "1", "--week", "revision"])
        out = capsys.readouterr().out
        assert "Revision" in out
        assert "Methods" not in out

    @patch("worsaga.cli._client")
    def test_contents_week_filter_json(self, mock_client_fn, capsys):
        """contents --week should also filter in JSON mode."""
        sections = [
            {"name": "Week 1", "modules": []},
            {"name": "Week 2", "modules": []},
        ]
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "EC100"}]
        mock.get_course_contents.return_value = sections
        main(["--json", "contents", "1", "--week", "2"])
        output = json.loads(capsys.readouterr().out)
        assert len(output) == 1
        assert output[0]["name"] == "Week 2"

    @patch("worsaga.cli._client")
    def test_contents_no_week_shows_all(self, mock_client_fn, capsys):
        """contents without --week should show all sections (unchanged)."""
        sections = [
            {"name": "Week 1", "modules": [{"name": "A", "modname": "resource"}]},
            {"name": "Week 2", "modules": [{"name": "B", "modname": "resource"}]},
        ]
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "EC100"}]
        mock.get_course_contents.return_value = sections
        main(["contents", "1"])
        out = capsys.readouterr().out
        assert "Week 1" in out
        assert "Week 2" in out


class TestNonInteractiveSetup:
    @patch("worsaga.cli.test_connection")
    @patch("worsaga.cli.MoodleConfig.write_config")
    def test_setup_noninteractive(self, mock_write, mock_test_conn, capsys, tmp_path):
        mock_test_conn.return_value = {"userid": 42}
        mock_write.return_value = tmp_path / "config.json"
        main(["setup", "--url", "https://m.example.com", "--token", "tok123"])
        out = capsys.readouterr().out
        assert "non-interactive" in out
        mock_write.assert_called_once()
        call_kwargs = mock_write.call_args
        assert call_kwargs[1]["url"] == "https://m.example.com" or call_kwargs[0][0] == "https://m.example.com"

    @patch("worsaga.cli.test_connection")
    @patch("worsaga.cli.MoodleConfig.write_config")
    def test_setup_noninteractive_with_userid(self, mock_write, mock_test_conn, capsys, tmp_path):
        mock_test_conn.return_value = {"userid": 99}
        mock_write.return_value = tmp_path / "config.json"
        main(["setup", "--url", "https://m.example.com", "--token", "tok", "--userid", "7"])
        mock_write.assert_called_once()
        # Should use the explicitly provided userid, not the auto-detected one
        _, kwargs = mock_write.call_args
        assert kwargs.get("userid", mock_write.call_args[0][2] if len(mock_write.call_args[0]) > 2 else None) == 7

    @patch("getpass.getpass", return_value="tok123")
    @patch("builtins.input", side_effect=["https://m.example.com", ""])
    @patch("worsaga.cli.test_connection")
    @patch("worsaga.cli.MoodleConfig.write_config")
    def test_setup_interactive_fallback(self, mock_write, mock_test_conn, mock_input, mock_getpass, capsys, tmp_path):
        """setup without --url/--token should still prompt interactively."""
        mock_test_conn.return_value = {"userid": 42}
        mock_write.return_value = tmp_path / "config.json"
        main(["setup"])
        out = capsys.readouterr().out
        assert "worsaga setup" in out
        assert mock_input.call_count == 2
        mock_getpass.assert_called_once()


class TestErrorHandling:
    def test_valueerror_clean_exit(self, capsys, monkeypatch):
        """ValueError (e.g. missing config) should produce clean stderr, exit 1."""
        for var in ("WORSAGA_URL", "WORSAGA_TOKEN", "WORSAGA_USERID", "WORSAGA_CREDS_PATH"):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(SystemExit) as exc:
            main([
                "--url", "", "--token", "",
                "--creds-path", "/nonexistent/path.json",
                "courses",
            ])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "Error" in err

    @patch("worsaga.cli._client")
    def test_runtime_error_clean_exit(self, mock_client_fn, capsys):
        mock_client_fn.side_effect = RuntimeError("Moodle API error: bad token")
        with pytest.raises(SystemExit) as exc:
            main(["courses"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "Moodle API error" in err

    @patch("worsaga.cli._client")
    def test_urlerror_clean_exit(self, mock_client_fn, capsys):
        import urllib.error
        mock_client_fn.side_effect = urllib.error.URLError("Name or service not known")
        with pytest.raises(SystemExit) as exc:
            main(["courses"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "network request failed" in err


class TestTopLevelCredentials:
    @patch("worsaga.cli.MoodleConfig.load")
    @patch("worsaga.cli.MoodleClient")
    def test_url_token_passed_to_config_load(self, mock_client_cls, mock_load, capsys):
        """Top-level --url/--token should be forwarded to MoodleConfig.load()."""
        from worsaga.config import MoodleConfig as RealConfig
        mock_load.return_value = RealConfig(url="https://m.example.com", token="tok")
        mock_client_cls.return_value.get_courses.return_value = []
        main(["--url", "https://m.example.com", "--token", "tok", "courses"])
        mock_load.assert_called_once_with(
            url="https://m.example.com",
            token="tok",
            userid=None,
            creds_path=None,
        )


class TestSetupMessaging:
    @patch("worsaga.cli.test_connection")
    @patch("worsaga.cli.MoodleConfig.write_config")
    def test_success_shows_next_steps(self, mock_write, mock_test_conn, capsys, tmp_path):
        mock_test_conn.return_value = {"userid": 42}
        mock_write.return_value = tmp_path / "config.json"
        main(["setup", "--url", "https://m.example.com", "--token", "tok123"])
        out = capsys.readouterr().out
        assert "Setup complete!" in out
        assert "courses" in out
        assert "deadlines" in out

    @patch("worsaga.cli.test_connection")
    @patch("worsaga.cli.MoodleConfig.write_config")
    def test_no_600_on_windows(self, mock_write, mock_test_conn, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(os, "name", "nt")
        mock_test_conn.return_value = {"userid": 42}
        mock_write.return_value = tmp_path / "config.json"
        main(["setup", "--url", "https://m.example.com", "--token", "tok123"])
        out = capsys.readouterr().out
        assert "Permissions set to owner-only (600)." not in out
        assert "Setup complete!" in out

    @patch("worsaga.cli.test_connection")
    @patch("worsaga.cli.MoodleConfig.write_config")
    def test_600_on_unix(self, mock_write, mock_test_conn, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(os, "name", "posix")
        mock_test_conn.return_value = {"userid": 42}
        mock_write.return_value = tmp_path / "config.json"
        main(["setup", "--url", "https://m.example.com", "--token", "tok123"])
        out = capsys.readouterr().out
        assert "Permissions set to owner-only (600)." in out

    @patch("getpass.getpass", return_value="tok123")
    @patch("builtins.input", side_effect=["https://m.example.com", ""])
    @patch("worsaga.cli.test_connection")
    @patch("worsaga.cli.MoodleConfig.write_config")
    def test_interactive_uses_getpass(self, mock_write, mock_test_conn, mock_input, mock_getpass, capsys, tmp_path):
        mock_test_conn.return_value = {"userid": 42}
        mock_write.return_value = tmp_path / "config.json"
        main(["setup"])
        mock_getpass.assert_called_once()
        assert mock_input.call_count == 2

    @patch("worsaga.cli.test_connection")
    @patch("worsaga.cli.MoodleConfig.write_config")
    def test_noninteractive_does_not_use_getpass(self, mock_write, mock_test_conn, capsys, tmp_path):
        mock_test_conn.return_value = {"userid": 42}
        mock_write.return_value = tmp_path / "config.json"
        with patch("getpass.getpass") as mock_getpass:
            main(["setup", "--url", "https://m.example.com", "--token", "tok123"])
            mock_getpass.assert_not_called()

    @patch("worsaga.cli.test_connection")
    @patch("worsaga.cli.MoodleConfig.write_config")
    def test_module_invocation_hint(self, mock_write, mock_test_conn, monkeypatch, capsys, tmp_path):
        """When invoked via python -m, next-step hints should use module form."""
        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.setattr(sys, "argv", ["/path/to/worsaga.cli", "setup"])
        mock_test_conn.return_value = {"userid": 42}
        mock_write.return_value = tmp_path / "config.json"
        main(["setup", "--url", "https://m.example.com", "--token", "tok123"])
        out = capsys.readouterr().out
        assert "python -m worsaga.cli" in out


class TestSearchCommand:
    SECTIONS = [
        {
            "name": "Week 1 — Introduction", "section": 1,
            "modules": [
                {"id": 10, "name": "Intro Lecture Slides", "modname": "resource", "url": ""},
                {"id": 11, "name": "Tutorial Sheet 1", "modname": "resource", "url": ""},
            ],
        },
        {
            "name": "Week 2 — Regression", "section": 2,
            "modules": [
                {"id": 20, "name": "Regression Slides", "modname": "resource", "url": ""},
                {"id": 21, "name": "Lab: OLS in R", "modname": "assign", "url": ""},
            ],
        },
    ]

    def test_parser_accepts_search(self):
        parser = _build_parser()
        args = parser.parse_args(["search", "EC100", "regression"])
        assert args.command == "search"
        assert args.course == "EC100"
        assert args.query == "regression"

    @patch("worsaga.cli._client")
    def test_search_json(self, mock_client_fn, capsys):
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "EC100"}]
        mock.get_course_contents.return_value = self.SECTIONS
        main(["--json", "search", "1", "regression"])
        output = json.loads(capsys.readouterr().out)
        assert len(output) == 2  # section name match + module name match
        names = {r["module_name"] for r in output}
        assert "Regression Slides" in names

    @patch("worsaga.cli._client")
    def test_search_table(self, mock_client_fn, capsys):
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "EC100"}]
        mock.get_course_contents.return_value = self.SECTIONS
        main(["search", "1", "regression"])
        out = capsys.readouterr().out
        assert "Regression" in out
        assert "Section" in out  # header row

    @patch("worsaga.cli._client")
    def test_search_no_matches(self, mock_client_fn, capsys):
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "EC100"}]
        mock.get_course_contents.return_value = self.SECTIONS
        main(["search", "1", "nonexistent"])
        out = capsys.readouterr().out
        assert "No matches" in out

    @patch("worsaga.cli._client")
    def test_search_json_no_matches(self, mock_client_fn, capsys):
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "EC100"}]
        mock.get_course_contents.return_value = self.SECTIONS
        main(["--json", "search", "1", "nonexistent"])
        output = json.loads(capsys.readouterr().out)
        assert output == []

    @patch("worsaga.cli._client")
    def test_search_json_shape(self, mock_client_fn, capsys):
        """Each result should contain section and module context."""
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "EC100"}]
        mock.get_course_contents.return_value = self.SECTIONS
        main(["--json", "search", "1", "tutorial"])
        output = json.loads(capsys.readouterr().out)
        assert len(output) == 1
        r = output[0]
        assert r["section_name"] == "Week 1 — Introduction"
        assert r["module_name"] == "Tutorial Sheet 1"
        assert "module_type" in r
        assert "section_num" in r


class TestDoctorCommand:
    def test_parser_accepts_doctor(self):
        parser = _build_parser()
        args = parser.parse_args(["doctor"])
        assert args.command == "doctor"

    @patch("worsaga.cli.test_connection")
    @patch("worsaga.cli.MoodleConfig.load")
    def test_doctor_success(self, mock_load, mock_test_conn, capsys):
        from worsaga.config import MoodleConfig as RC
        mock_load.return_value = RC(url="https://m.example.com", token="tok")
        mock_test_conn.return_value = {
            "userid": 42, "username": "ymushtaq", "sitename": "My Moodle",
        }
        main(["doctor"])
        out = capsys.readouterr().out
        assert "OK" in out
        assert "ymushtaq" in out
        assert "42" in out
        assert "My Moodle" in out

    @patch("worsaga.cli.test_connection")
    @patch("worsaga.cli.MoodleConfig.load")
    def test_doctor_success_json(self, mock_load, mock_test_conn, capsys):
        from worsaga.config import MoodleConfig as RC
        mock_load.return_value = RC(url="https://m.example.com", token="tok")
        mock_test_conn.return_value = {
            "userid": 42, "username": "ymushtaq", "sitename": "My Moodle",
        }
        main(["--json", "doctor"])
        output = json.loads(capsys.readouterr().out)
        assert output["ok"] is True
        assert output["userid"] == 42
        assert output["username"] == "ymushtaq"
        assert output["sitename"] == "My Moodle"

    def test_doctor_no_config(self, capsys, monkeypatch):
        """doctor should report missing credentials cleanly."""
        for var in ("WORSAGA_URL", "WORSAGA_TOKEN", "WORSAGA_USERID", "WORSAGA_CREDS_PATH"):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(SystemExit) as exc:
            main([
                "--url", "", "--token", "",
                "--creds-path", "/nonexistent/path.json",
                "doctor",
            ])
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "FAIL" in out

    def test_doctor_no_config_json(self, capsys, monkeypatch):
        for var in ("WORSAGA_URL", "WORSAGA_TOKEN", "WORSAGA_USERID", "WORSAGA_CREDS_PATH"):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(SystemExit) as exc:
            main([
                "--json", "--url", "", "--token", "",
                "--creds-path", "/nonexistent/path.json",
                "doctor",
            ])
        assert exc.value.code == 1
        output = json.loads(capsys.readouterr().out)
        assert output["ok"] is False
        assert "error" in output

    @patch("worsaga.cli.test_connection")
    @patch("worsaga.cli.MoodleConfig.load")
    def test_doctor_connection_failure(self, mock_load, mock_test_conn, capsys):
        from worsaga.config import MoodleConfig as RC
        mock_load.return_value = RC(url="https://m.example.com", token="badtok")
        mock_test_conn.side_effect = RuntimeError("Invalid token")
        with pytest.raises(SystemExit) as exc:
            main(["doctor"])
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "Invalid token" in out

    @patch("worsaga.cli.test_connection")
    @patch("worsaga.cli.MoodleConfig.load")
    def test_doctor_connection_failure_json(self, mock_load, mock_test_conn, capsys):
        from worsaga.config import MoodleConfig as RC
        mock_load.return_value = RC(url="https://m.example.com", token="badtok")
        mock_test_conn.side_effect = RuntimeError("Invalid token")
        with pytest.raises(SystemExit) as exc:
            main(["--json", "doctor"])
        assert exc.value.code == 1
        output = json.loads(capsys.readouterr().out)
        assert output["ok"] is False
        assert "Invalid token" in output["error"]


class TestConfigCommand:
    def test_parser_accepts_config_path(self):
        parser = _build_parser()
        args = parser.parse_args(["config", "path"])
        assert args.command == "config"
        assert args.action == "path"

    def test_parser_config_defaults_to_path(self):
        parser = _build_parser()
        args = parser.parse_args(["config"])
        assert args.command == "config"
        assert args.action == "path"

    @patch("worsaga.cli._find_config_file")
    def test_config_path_found(self, mock_find, capsys):
        mock_find.return_value = Path("/home/user/.config/worsaga/config.json")
        main(["config", "path"])
        out = capsys.readouterr().out
        assert "config.json" in out

    @patch("worsaga.cli._find_config_file")
    def test_config_path_not_found(self, mock_find, capsys):
        mock_find.return_value = None
        main(["config"])
        out = capsys.readouterr().out
        assert "No config file found" in out

    @patch("worsaga.cli._find_config_file")
    def test_config_path_json_found(self, mock_find, capsys):
        mock_find.return_value = Path("/home/user/.config/worsaga/config.json")
        main(["--json", "config", "path"])
        output = json.loads(capsys.readouterr().out)
        assert output["found"] is True
        assert "config.json" in output["config_path"]

    @patch("worsaga.cli._find_config_file")
    def test_config_path_json_not_found(self, mock_find, capsys):
        mock_find.return_value = None
        main(["--json", "config"])
        output = json.loads(capsys.readouterr().out)
        assert output["found"] is False
        # When no file is found, config_path shows the default path
        assert output["config_path"] is not None

    @patch("worsaga.cli._find_config_file")
    def test_config_path_with_creds_path(self, mock_find, capsys):
        """--creds-path should be forwarded to config file resolution."""
        mock_find.return_value = Path("/tmp/custom.json")
        main(["--creds-path", "/tmp/custom.json", "config", "path"])
        mock_find.assert_called_once_with("/tmp/custom.json")
        out = capsys.readouterr().out
        assert "custom.json" in out


# ── Phase 4: Output contract hardening tests ─────────────────────


class TestJsonPlacement:
    """--json should work both before and after the subcommand name."""

    @patch("worsaga.cli._client")
    def test_json_before_subcommand(self, mock_client_fn, capsys):
        mock_client_fn.return_value.get_courses.return_value = [
            {"id": 1, "shortname": "EC100", "fullname": "Econ 100"},
        ]
        main(["--json", "courses"])
        output = json.loads(capsys.readouterr().out)
        assert output[0]["shortname"] == "EC100"

    @patch("worsaga.cli._client")
    def test_json_after_subcommand(self, mock_client_fn, capsys):
        mock_client_fn.return_value.get_courses.return_value = [
            {"id": 1, "shortname": "EC100", "fullname": "Econ 100"},
        ]
        main(["courses", "--json"])
        output = json.loads(capsys.readouterr().out)
        assert output[0]["shortname"] == "EC100"

    @patch("worsaga.cli._client")
    def test_json_after_contents(self, mock_client_fn, capsys):
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "EC100"}]
        mock.get_course_contents.return_value = [
            {"name": "Week 1", "section": 1, "modules": []},
        ]
        main(["contents", "1", "--json"])
        output = json.loads(capsys.readouterr().out)
        assert output[0]["name"] == "Week 1"

    @patch("worsaga.cli._client")
    def test_json_after_deadlines(self, mock_client_fn, capsys):
        mock_client_fn.return_value.get_courses.return_value = []
        main(["deadlines", "--json"])
        output = json.loads(capsys.readouterr().out)
        assert output == []

    @patch("worsaga.cli._find_config_file")
    def test_json_after_config(self, mock_find, capsys):
        mock_find.return_value = None
        main(["config", "--json"])
        output = json.loads(capsys.readouterr().out)
        assert output["found"] is False

    def test_parser_json_after_subcommand(self):
        parser = _build_parser()
        args = parser.parse_args(["courses", "--json"])
        assert args.json is True

    def test_parser_json_before_subcommand(self):
        parser = _build_parser()
        args = parser.parse_args(["--json", "courses"])
        assert args.json is True

    def test_parser_no_json(self):
        parser = _build_parser()
        args = parser.parse_args(["courses"])
        assert args.json is False


class TestQuietFlag:
    """--quiet / -q should suppress stderr progress output."""

    def test_parser_quiet_before_subcommand(self):
        parser = _build_parser()
        args = parser.parse_args(["--quiet", "courses"])
        assert args.quiet is True

    def test_parser_quiet_after_subcommand(self):
        parser = _build_parser()
        args = parser.parse_args(["courses", "--quiet"])
        assert args.quiet is True

    def test_parser_quiet_short(self):
        parser = _build_parser()
        args = parser.parse_args(["-q", "courses"])
        assert args.quiet is True

    def test_parser_quiet_short_after_subcommand(self):
        parser = _build_parser()
        args = parser.parse_args(["courses", "-q"])
        assert args.quiet is True

    def test_parser_no_quiet(self):
        parser = _build_parser()
        args = parser.parse_args(["courses"])
        assert args.quiet is False

    @patch("worsaga.cli._client")
    @patch("worsaga.cli.find_best_section")
    @patch("worsaga.cli.build_weekly_summary")
    def test_quiet_suppresses_extraction_progress(
        self, mock_summary, mock_best, mock_client_fn, capsys
    ):
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "EC100"}]
        mock.get_course_contents.return_value = []
        mock_best.return_value = (
            {"modules": [{"name": "m"}]},
            "teaching",
            "Week 1",
        )

        def _fake_summary(client, course_id, week, *, sections=None, on_extract=None):
            if on_extract is not None:
                on_extract("slides.pdf")
            return {
                "bullets": ["point"], "method": "extraction",
                "section_name": "Week 1", "section_type": "teaching",
                "file_count": 1, "week": week, "course_id": course_id,
            }
        mock_summary.side_effect = _fake_summary

        main(["-q", "summary", "1", "--week", "1"])
        err = capsys.readouterr().err
        assert "Extracting" not in err

    @patch("worsaga.cli._client")
    @patch("worsaga.cli.find_best_section")
    @patch("worsaga.cli.build_weekly_summary")
    def test_no_quiet_shows_extraction_progress(
        self, mock_summary, mock_best, mock_client_fn, capsys
    ):
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "EC100"}]
        mock.get_course_contents.return_value = []
        mock_best.return_value = (
            {"modules": [{"name": "m"}]},
            "teaching",
            "Week 1",
        )

        def _fake_summary(client, course_id, week, *, sections=None, on_extract=None):
            if on_extract is not None:
                on_extract("slides.pdf")
            return {
                "bullets": ["point"], "method": "extraction",
                "section_name": "Week 1", "section_type": "teaching",
                "file_count": 1, "week": week, "course_id": course_id,
            }
        mock_summary.side_effect = _fake_summary

        main(["summary", "1", "--week", "1"])
        err = capsys.readouterr().err
        assert "Extracting slides.pdf" in err


class TestRawFlag:
    """--raw should output the unprocessed Moodle API payload with --json."""

    FAKE_COURSES = [
        {"id": 1, "shortname": "EC100", "fullname": "Econ 100",
         "enrolledusercount": 300, "idnumber": "EC100-2526", "visible": 1},
    ]
    FAKE_SECTIONS = [
        {
            "id": 99, "name": "Week 1", "section": 1, "visible": 1,
            "summary": "<p>intro</p>", "summaryformat": 1,
            "modules": [{
                "id": 10, "name": "Slides", "modname": "resource",
                "url": "https://example.com/mod",
                "instance": 42, "visible": 1,
                "contents": [{"type": "file", "filename": "s.pdf"}],
            }],
        },
    ]

    def test_parser_raw_on_courses(self):
        parser = _build_parser()
        args = parser.parse_args(["courses", "--raw"])
        assert args.raw is True

    def test_parser_raw_on_contents(self):
        parser = _build_parser()
        args = parser.parse_args(["contents", "1", "--raw"])
        assert args.raw is True

    @patch("worsaga.cli._client")
    def test_courses_json_normalized(self, mock_client_fn, capsys):
        """--json without --raw should return only id/shortname/fullname."""
        mock_client_fn.return_value.get_courses.return_value = self.FAKE_COURSES
        main(["courses", "--json"])
        output = json.loads(capsys.readouterr().out)
        assert len(output) == 1
        c = output[0]
        assert set(c.keys()) == {"id", "shortname", "fullname"}
        assert c["id"] == 1

    @patch("worsaga.cli._client")
    def test_courses_json_raw(self, mock_client_fn, capsys):
        """--json --raw should return the full Moodle payload."""
        mock_client_fn.return_value.get_courses.return_value = self.FAKE_COURSES
        main(["courses", "--json", "--raw"])
        output = json.loads(capsys.readouterr().out)
        assert len(output) == 1
        c = output[0]
        assert "enrolledusercount" in c
        assert "visible" in c

    @patch("worsaga.cli._client")
    def test_contents_json_normalized(self, mock_client_fn, capsys):
        """--json without --raw should return normalized sections."""
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "EC100"}]
        mock.get_course_contents.return_value = self.FAKE_SECTIONS
        main(["contents", "1", "--json"])
        output = json.loads(capsys.readouterr().out)
        s = output[0]
        assert set(s.keys()) == {"section", "name", "modules"}
        m = s["modules"][0]
        assert set(m.keys()) == {"id", "name", "type", "url"}
        assert m["type"] == "resource"

    @patch("worsaga.cli._client")
    def test_contents_json_raw(self, mock_client_fn, capsys):
        """--json --raw should return the full Moodle payload for contents."""
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "EC100"}]
        mock.get_course_contents.return_value = self.FAKE_SECTIONS
        main(["contents", "1", "--json", "--raw"])
        output = json.loads(capsys.readouterr().out)
        s = output[0]
        assert "summary" in s
        assert "visible" in s
        assert "contents" in s["modules"][0]

    @patch("worsaga.cli._client")
    def test_raw_without_json_is_table(self, mock_client_fn, capsys):
        """--raw without --json should just show the normal table."""
        mock_client_fn.return_value.get_courses.return_value = self.FAKE_COURSES
        main(["courses", "--raw"])
        out = capsys.readouterr().out
        assert "EC100" in out
        # Should NOT be JSON
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


class TestNormalizers:
    """Unit tests for _normalize_courses and _normalize_contents."""

    def test_normalize_courses_strips_extra_fields(self):
        raw = [
            {"id": 1, "shortname": "EC100", "fullname": "Econ",
             "enrolledusercount": 300, "visible": 1, "format": "topics"},
        ]
        result = _normalize_courses(raw)
        assert len(result) == 1
        assert set(result[0].keys()) == {"id", "shortname", "fullname"}
        assert result[0]["id"] == 1

    def test_normalize_courses_empty(self):
        assert _normalize_courses([]) == []

    def test_normalize_contents_structure(self):
        raw = [
            {
                "id": 99, "name": "Week 1", "section": 1, "visible": 1,
                "summary": "<p>text</p>",
                "modules": [
                    {"id": 10, "name": "Slides", "modname": "resource",
                     "url": "https://x.com/m", "instance": 42},
                ],
            },
        ]
        result = _normalize_contents(raw)
        assert len(result) == 1
        s = result[0]
        assert set(s.keys()) == {"section", "name", "modules"}
        assert s["section"] == 1
        assert s["name"] == "Week 1"
        m = s["modules"][0]
        assert set(m.keys()) == {"id", "name", "type", "url"}
        assert m["type"] == "resource"

    def test_normalize_contents_empty_modules(self):
        raw = [{"name": "General", "section": 0, "modules": []}]
        result = _normalize_contents(raw)
        assert result[0]["modules"] == []

    def test_normalize_contents_missing_fields(self):
        """Handles modules with missing optional fields gracefully."""
        raw = [{"name": "S1", "modules": [{"name": "M1"}]}]
        result = _normalize_contents(raw)
        m = result[0]["modules"][0]
        assert m["id"] is None
        assert m["type"] == ""
        assert m["url"] == ""
