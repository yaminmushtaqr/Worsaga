"""Tests for the worsaga CLI."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from worsaga.autosync import DEFAULT_INTERVAL_MINUTES, MIN_INTERVAL_MINUTES
from worsaga.cli import (
    CourseResolutionError,
    _build_parser,
    _normalize_contents,
    _normalize_courses,
    _resolve_course_id,
    main,
)
from worsaga.concurrency import DEFAULT_MAX_WORKERS, MAX_ALLOWED_WORKERS
from worsaga.watch import DEFAULT_WATCH_INTERVAL, MIN_WATCH_INTERVAL


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

    def test_grades_command(self):
        parser = _build_parser()
        args = parser.parse_args(["grades"])
        assert args.command == "grades"
        assert args.course is None

    def test_grades_with_course_and_missing(self):
        parser = _build_parser()
        args = parser.parse_args(["grades", "ECON101", "--missing"])
        assert args.course == "ECON101"
        assert args.missing is True

    def test_assignments_command(self):
        parser = _build_parser()
        args = parser.parse_args(["assignments"])
        assert args.command == "assignments"
        assert args.course is None

    def test_assignments_with_filters(self):
        parser = _build_parser()
        args = parser.parse_args([
            "assignments", "ECON101", "--due-soon", "--days", "7", "--status", "missing",
        ])
        assert args.course == "ECON101"
        assert args.due_soon is True
        assert args.days == 7
        assert args.status == "missing"

    def test_phase_1c_commands_parse(self):
        parser = _build_parser()
        assert parser.parse_args(["forums", "ECON101"]).command == "forums"
        assert parser.parse_args(["forum", "latest", "ECON101"]).action == "latest"
        assert parser.parse_args(["updates"]).command == "updates"
        assert parser.parse_args(["notifications", "--unread-only"]).unread_only is True
        assert parser.parse_args(["inbox", "--since", "24h"]).since == "24h"
        assert parser.parse_args(["digest", "--since", "7d"]).since == "7d"
        assert parser.parse_args(["calendar", "ECON101", "--days", "10"]).days == 10
        assert parser.parse_args(["calendar", "ECON101", "--week", "3"]).week == "3"

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
        args = parser.parse_args(["contents", "ECON101"])
        assert args.course == "ECON101"

    def test_materials_command(self):
        parser = _build_parser()
        args = parser.parse_args(["materials", "ECON101"])
        assert args.command == "materials"
        assert args.course == "ECON101"
        assert args.week is None

    def test_materials_with_week(self):
        parser = _build_parser()
        args = parser.parse_args(["materials", "ECON101", "--week", "3"])
        assert args.week == "3"

    def test_summary_command(self):
        parser = _build_parser()
        args = parser.parse_args(["summary", "ECON101", "--week", "3"])
        assert args.command == "summary"
        assert args.course == "ECON101"
        assert args.week == "3"

    def test_summary_string_week(self):
        parser = _build_parser()
        args = parser.parse_args(["summary", "ECON101", "--week", "revision"])
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

    def test_yaml_flag(self):
        parser = _build_parser()
        args = parser.parse_args(["--yaml", "courses"])
        assert args.yaml is True

    def test_yaml_after_subcommand(self):
        parser = _build_parser()
        args = parser.parse_args(["courses", "--yaml"])
        assert args.yaml is True

    def test_json_and_yaml_are_rejected(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--json", "--yaml", "courses"])
        assert exc.value.code == 2
        assert "cannot be used together" in capsys.readouterr().err

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
        args = parser.parse_args(["contents", "ECON101", "--week", "3"])
        assert args.week == "3"

    def test_contents_with_string_week(self):
        parser = _build_parser()
        args = parser.parse_args(["contents", "ECON101", "--week", "revision"])
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

    def test_subcommand_help_shows_yaml_description(self, capsys):
        """--yaml in subcommand --help must show its help text."""
        with pytest.raises(SystemExit):
            main(["setup", "--help"])
        out = capsys.readouterr().out
        assert "Output machine-readable YAML" in out

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


class TestPolitenessPolicy:
    """Literal pins on the politeness values, and the text users see.

    Every other test interpolates these constants, so lowering a floor or
    raising the concurrency ceiling would still pass the suite. Pinning the
    literals here makes changing one a deliberate policy decision rather
    than an incidental test fix.
    """

    def test_autosync_interval_policy(self):
        assert MIN_INTERVAL_MINUTES == 15
        assert DEFAULT_INTERVAL_MINUTES == 30

    def test_watch_interval_policy(self):
        assert MIN_WATCH_INTERVAL == 300
        assert DEFAULT_WATCH_INTERVAL == 900

    def test_concurrency_policy(self):
        assert DEFAULT_MAX_WORKERS == 4
        assert MAX_ALLOWED_WORKERS == 8

    def test_watch_help_states_the_floor(self, capsys):
        with pytest.raises(SystemExit):
            main(["watch", "--help"])
        # Whitespace-normalised: argparse rewraps help at the terminal width.
        out = " ".join(capsys.readouterr().out.split())
        assert "minimum 300s" in out

    def test_autosync_help_states_the_floor(self, capsys):
        with pytest.raises(SystemExit):
            main(["auto-sync", "--help"])
        out = " ".join(capsys.readouterr().out.split())
        assert "minimum 15m" in out


class TestUpdateCommand:
    def test_update_human_output_uses_pypi(self, capsys):
        from worsaga import __version__
        main(["update"])
        out = capsys.readouterr().out
        assert f"Current version: {__version__}" in out
        assert "Release source: PyPI" in out
        assert "pipx upgrade worsaga" in out
        assert "ssh" not in out
        assert "deploy" not in out.lower()
        assert "private" not in out.lower()

    def test_update_json_output(self, capsys):
        from worsaga import __version__

        main(["--json", "update"])
        out = json.loads(capsys.readouterr().out)
        assert out["current_version"] == __version__
        assert out["latest_version"] is None
        assert out["update_available"] is None
        assert out["source"] == "pypi"
        assert out["install_spec"] == "worsaga[mcp]"
        assert out["upgrade_command"].startswith("pipx upgrade worsaga")
        assert "ssh" not in out["upgrade_command"]


class TestResolveCourseId:
    def _mock_client(self, courses):
        """Return a mock client whose get_courses() returns the given list."""
        from unittest.mock import MagicMock
        client = MagicMock()
        client.get_courses.return_value = courses
        return client

    def test_integer_id_resolves_when_enrolled(self):
        client = self._mock_client([{"id": 42, "shortname": "ECON101"}])
        assert _resolve_course_id(client, "42") == 42

    def test_integer_id_outside_enrolment_is_refused(self):
        client = self._mock_client([{"id": 10, "shortname": "ECON101"}])
        with pytest.raises(CourseResolutionError, match="not enrolled"):
            _resolve_course_id(client, "42")

    def test_shortcode_lookup(self):
        client = self._mock_client([
            {"id": 10, "shortname": "ECON101"},
            {"id": 20, "shortname": "PSY110"},
        ])
        assert _resolve_course_id(client, "econ101") == 10  # case-insensitive

    def test_prefix_match_underscore(self):
        """CS210 should resolve to CS210_2526 when it's the only prefix match."""
        client = self._mock_client([
            {"id": 10, "shortname": "ECON101"},
            {"id": 30, "shortname": "CS210_2526"},
        ])
        assert _resolve_course_id(client, "CS210") == 30

    def test_prefix_match_hyphen(self):
        """Prefix matching should also work with hyphen separators."""
        client = self._mock_client([
            {"id": 40, "shortname": "PSY110-2526"},
        ])
        assert _resolve_course_id(client, "psy110") == 40  # case-insensitive

    def test_exact_match_preferred_over_prefix(self):
        """If both an exact and prefix match exist, exact wins."""
        client = self._mock_client([
            {"id": 10, "shortname": "ECON101"},
            {"id": 20, "shortname": "ECON101_2526"},
        ])
        assert _resolve_course_id(client, "ECON101") == 10

    def test_ambiguous_prefix_raises(self):
        """Multiple courses sharing the same prefix should raise CourseResolutionError."""
        client = self._mock_client([
            {"id": 30, "shortname": "CS210_2526"},
            {"id": 31, "shortname": "CS210_2425"},
        ])
        with pytest.raises(CourseResolutionError):
            _resolve_course_id(client, "CS210")

    def test_ambiguous_prefix_error_message(self):
        """Ambiguous prefix error should list the conflicting courses."""
        client = self._mock_client([
            {"id": 30, "shortname": "CS210_2526"},
            {"id": 31, "shortname": "CS210_2425"},
        ])
        with pytest.raises(CourseResolutionError, match="ambiguous"):
            _resolve_course_id(client, "CS210")
        # Verify both shortnames appear in the exception message
        try:
            _resolve_course_id(client, "CS210")
        except CourseResolutionError as e:
            msg = str(e)
            assert "CS210_2526" in msg
            assert "CS210_2425" in msg

    def test_unknown_code_raises(self):
        client = self._mock_client([
            {"id": 10, "shortname": "ECON101"},
        ])
        with pytest.raises(CourseResolutionError, match="no enrolled course"):
            _resolve_course_id(client, "NONEXISTENT")

    def test_unknown_code_caught_by_main(self, capsys):
        """CourseResolutionError from _resolve_course_id should exit 1 via main()."""
        with patch("worsaga.cli._client") as mock_client_fn:
            mock = mock_client_fn.return_value
            mock.get_courses.return_value = [{"id": 10, "shortname": "ECON101"}]
            with pytest.raises(SystemExit) as exc:
                main(["contents", "NONEXISTENT"])
            assert exc.value.code == 1
            err = capsys.readouterr().err
            assert "no enrolled course" in err


class TestCommandOutput:
    FAKE_COURSES = [
        {"id": 1, "shortname": "ECON101", "fullname": "Economics 100"},
        {"id": 2, "shortname": "PSY110", "fullname": "Mathematics 100"},
    ]

    @patch("worsaga.cli._client")
    def test_courses_json(self, mock_client_fn, capsys):
        mock_client_fn.return_value.get_courses.return_value = self.FAKE_COURSES
        main(["--json", "courses"])
        output = json.loads(capsys.readouterr().out)
        assert len(output) == 2
        assert output[0]["shortname"] == "ECON101"

    @patch("worsaga.cli._client")
    def test_courses_table(self, mock_client_fn, capsys):
        mock_client_fn.return_value.get_courses.return_value = self.FAKE_COURSES
        main(["courses"])
        out = capsys.readouterr().out
        assert "ECON101" in out
        assert "PSY110" in out

    @patch("worsaga.cli._client")
    def test_deadlines_empty(self, mock_client_fn, capsys):
        mock_client_fn.return_value.get_courses.return_value = []
        main(["deadlines"])
        out = capsys.readouterr().out
        assert "No deadlines" in out

    @patch("worsaga.cli.collect_grades_data")
    @patch("worsaga.cli._client")
    def test_grades_json(self, mock_client_fn, mock_collect_grades, capsys):
        mock_collect_grades.return_value = {
            "grades": [{
                "course_shortname": "ECON101",
                "item_name": "Essay",
                "grade_display": "70",
                "percentage": 70.0,
                "weight": None,
                "status": "graded",
                "feedback": "",
            }],
            "warnings": [],
        }
        main(["--json", "grades"])
        output = json.loads(capsys.readouterr().out)
        assert output[0]["item_name"] == "Essay"
        mock_collect_grades.assert_called_once_with(
            mock_client_fn.return_value, course_id=None, on_progress=None,
        )

    @patch("worsaga.cli.collect_grades_data")
    @patch("worsaga.cli._client")
    def test_grades_missing_filters(self, mock_client_fn, mock_collect_grades, capsys):
        mock_collect_grades.return_value = {
            "grades": [
            {
                "course_shortname": "ECON101",
                "item_name": "Essay",
                "grade_display": "",
                "percentage": None,
                "weight": None,
                "status": "missing",
                "feedback": "",
            },
            {
                "course_shortname": "ECON101",
                "item_name": "Midterm",
                "grade_display": "70",
                "percentage": 70.0,
                "weight": None,
                "status": "graded",
                "feedback": "",
            },
            ],
            "warnings": [],
        }
        main(["--json", "grades", "--missing"])
        output = json.loads(capsys.readouterr().out)
        assert [row["item_name"] for row in output] == ["Essay"]

    @patch("worsaga.cli.collect_grades_data")
    @patch("worsaga.cli._client")
    def test_grades_table(self, mock_client_fn, mock_collect_grades, capsys):
        mock_collect_grades.return_value = {
            "grades": [{
                "course_shortname": "ECON101",
                "item_name": "Essay",
                "grade_display": "70",
                "percentage": 70.0,
                "weight": 40.0,
                "status": "graded",
                "feedback": "Good",
            }],
            "warnings": [],
        }
        main(["grades"])
        out = capsys.readouterr().out
        assert "Essay" in out
        assert "graded" in out

    @patch("worsaga.cli.collect_grades_data")
    @patch("worsaga.cli._client")
    def test_grades_default_hides_unknown(self, mock_client_fn, mock_collect_grades, capsys):
        mock_collect_grades.return_value = {
            "grades": [
                {
                    "course_shortname": "ECON101",
                    "item_name": "Placeholder",
                    "grade_display": "",
                    "percentage": None,
                    "weight": None,
                    "status": "unknown",
                    "feedback": "",
                },
            ],
            "warnings": [],
        }
        main(["--json", "grades"])
        assert json.loads(capsys.readouterr().out) == []

    @patch("worsaga.cli.collect_grades_data")
    @patch("worsaga.cli._client")
    def test_grades_all_shows_unknown(self, mock_client_fn, mock_collect_grades, capsys):
        mock_collect_grades.return_value = {
            "grades": [
                {
                    "course_shortname": "ECON101",
                    "item_name": "Placeholder",
                    "grade_display": "",
                    "percentage": None,
                    "weight": None,
                    "status": "unknown",
                    "feedback": "",
                },
            ],
            "warnings": [],
        }
        main(["--json", "grades", "--all"])
        output = json.loads(capsys.readouterr().out)
        assert output[0]["status"] == "unknown"

    @patch("worsaga.cli.build_grade_summary")
    @patch("worsaga.cli._client")
    def test_grades_summary(self, mock_client_fn, mock_summary, capsys):
        mock_summary.return_value = {
            "course_id": None,
            "total_items": 2,
            "status_counts": {"graded": 1, "missing": 1},
            "course_totals": [],
        }
        main(["grades", "--summary"])
        out = capsys.readouterr().out
        assert "Grade items: 2" in out
        assert "missing: 1" in out

    @patch("worsaga.cli.get_assignments_data")
    @patch("worsaga.cli._client")
    def test_assignments_json(self, mock_client_fn, mock_get_assignments, capsys):
        mock_get_assignments.return_value = [
            {
                "course_shortname": "ECON101",
                "name": "Essay",
                "due_str": "Nov 15 22:13 UTC",
                "days_left": 1,
                "status": "missing",
                "submitted": False,
            },
        ]
        main(["--json", "assignments"])
        output = json.loads(capsys.readouterr().out)
        assert output[0]["name"] == "Essay"
        mock_get_assignments.assert_called_once_with(
            mock_client_fn.return_value,
            course_id=None,
            include_feedback=False,
            on_progress=None,
        )

    @patch("worsaga.cli.get_assignments_data")
    @patch("worsaga.cli._client")
    def test_assignments_due_soon_and_status_filters(self, mock_client_fn, mock_get_assignments, capsys):
        mock_get_assignments.return_value = [
            {
                "course_shortname": "ECON101",
                "name": "Essay",
                "due_str": "soon",
                "days_left": 1,
                "status": "missing",
                "submission_status": "new",
                "submitted": False,
            },
            {
                "course_shortname": "ECON101",
                "name": "Later",
                "due_str": "later",
                "days_left": 20,
                "status": "missing",
                "submission_status": "new",
                "submitted": False,
            },
            {
                "course_shortname": "ECON101",
                "name": "Done",
                "due_str": "soon",
                "days_left": 1,
                "status": "submitted",
                "submission_status": "submitted",
                "submitted": True,
            },
        ]
        main(["--json", "assignments", "--due-soon", "--days", "7", "--status", "missing"])
        output = json.loads(capsys.readouterr().out)
        assert [row["name"] for row in output] == ["Essay"]

    @patch("worsaga.cli.get_assignments_data")
    @patch("worsaga.cli._client")
    def test_assignments_table(self, mock_client_fn, mock_get_assignments, capsys):
        mock_get_assignments.return_value = [
            {
                "course_shortname": "ECON101",
                "name": "Essay",
                "due_str": "Nov 15 22:13 UTC",
                "days_left": 1,
                "status": "missing",
                "submitted": False,
            },
        ]
        main(["assignments"])
        out = capsys.readouterr().out
        assert "Essay" in out
        assert "missing" in out

    @patch("worsaga.cli.get_course_forums_data")
    @patch("worsaga.cli._client")
    def test_forums_json(self, mock_client_fn, mock_forums, capsys):
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "ECON101"}]
        mock_forums.return_value = [{"forum_id": 5, "name": "Announcements"}]
        main(["--json", "forums", "ECON101"])
        output = json.loads(capsys.readouterr().out)
        assert output[0]["name"] == "Announcements"
        mock_forums.assert_called_once_with(mock, 1)

    @patch("worsaga.cli.get_forum_discussions_data")
    @patch("worsaga.cli._client")
    def test_forum_latest_table(self, mock_client_fn, mock_discussions, capsys):
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "ECON101"}]
        mock_discussions.return_value = [
            {
                "modified_at": 1700000000,
                "forum_name": "Announcements",
                "unread_count": None,
                "name": "Update",
            },
        ]
        main(["forum", "latest", "ECON101"])
        out = capsys.readouterr().out
        assert "Update" in out
        assert "UTC" in out
        assert "1700000000" not in out

    @patch("worsaga.cli.get_latest_updates_data")
    @patch("worsaga.cli._client")
    def test_updates_json(self, mock_client_fn, mock_updates, capsys):
        mock_updates.return_value = [{"name": "Update"}]
        main(["--json", "updates", "--since", "7d"])
        output = json.loads(capsys.readouterr().out)
        assert output == [{"name": "Update"}]
        assert mock_updates.call_args.kwargs["since_days"] == 7

    @patch("worsaga.cli.get_latest_updates_data")
    @patch("worsaga.cli._client")
    def test_updates_table_formats_timestamps(self, mock_client_fn, mock_updates, capsys):
        mock_updates.return_value = [{
            "course_shortname": "ECON101",
            "forum_name": "Announcements",
            "modified_at": 1700000000,
            "name": "Update",
        }]
        main(["updates", "--since", "7d"])
        out = capsys.readouterr().out
        assert "UTC" in out
        assert "1700000000" not in out

    @patch("worsaga.cli.get_notifications_data")
    @patch("worsaga.cli._client")
    def test_notifications_json(self, mock_client_fn, mock_notifications, capsys):
        mock_notifications.return_value = [{"subject": "Notice"}]
        main(["--json", "notifications", "--unread-only"])
        output = json.loads(capsys.readouterr().out)
        assert output[0]["subject"] == "Notice"
        assert mock_notifications.call_args.kwargs["unread_only"] is True

    @patch("worsaga.cli.get_notifications_data")
    @patch("worsaga.cli._client")
    def test_notifications_table_formats_timestamps(
        self, mock_client_fn, mock_notifications, capsys,
    ):
        mock_notifications.return_value = [{
            "created_at": 1700000000,
            "read": False,
            "sender": "Tutor",
            "subject": "Notice",
        }]
        main(["notifications"])
        out = capsys.readouterr().out
        assert "UTC" in out
        assert "1700000000" not in out

    @patch("worsaga.cli.get_messages_data")
    @patch("worsaga.cli._client")
    def test_inbox_json(self, mock_client_fn, mock_messages, capsys):
        mock_messages.return_value = [{"subject": "Message"}]
        main(["--json", "inbox"])
        output = json.loads(capsys.readouterr().out)
        assert output[0]["subject"] == "Message"

    @patch("worsaga.cli.get_digest_data")
    @patch("worsaga.cli._client")
    def test_digest_json(self, mock_client_fn, mock_digest, capsys):
        mock_digest.return_value = {"since_days": 1, "warnings": []}
        main(["--json", "digest", "--since", "24h"])
        output = json.loads(capsys.readouterr().out)
        assert output["since_days"] == 1

    @patch("worsaga.cli.get_calendar_events_data")
    @patch("worsaga.cli._client")
    def test_calendar_json(self, mock_client_fn, mock_calendar, capsys):
        mock_calendar.return_value = [{"name": "Deadline"}]
        main(["--json", "calendar", "--days", "10"])
        output = json.loads(capsys.readouterr().out)
        assert output[0]["name"] == "Deadline"
        assert mock_calendar.call_args.kwargs["days"] == 10
        assert mock_calendar.call_args.kwargs["week"] is None

    @patch("worsaga.cli.get_calendar_events_data")
    @patch("worsaga.cli._client")
    def test_calendar_week_json(self, mock_client_fn, mock_calendar, capsys):
        mock_calendar.return_value = [{"name": "Week 3 quiz"}]
        mock_client_fn.return_value.get_courses.return_value = [
            {"id": 1, "shortname": "ECON101"},
        ]
        main(["--json", "calendar", "1", "--week", "3"])
        output = json.loads(capsys.readouterr().out)
        assert output[0]["name"] == "Week 3 quiz"
        assert mock_calendar.call_args.kwargs["course_id"] == 1
        assert mock_calendar.call_args.kwargs["week"] == "3"

    @patch("worsaga.cli._client")
    def test_contents_json(self, mock_client_fn, capsys):
        sections = [
            {"name": "Week 1", "modules": [{"name": "Lecture Notes", "modname": "resource"}]},
        ]
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "ECON101"}]
        mock.get_course_contents.return_value = sections
        main(["--json", "contents", "1"])
        output = json.loads(capsys.readouterr().out)
        assert output[0]["name"] == "Week 1"

    @patch("worsaga.cli._client")
    def test_contents_by_code(self, mock_client_fn, capsys):
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 5, "shortname": "STAT120"}]
        mock.get_course_contents.return_value = [
            {"name": "Overview", "modules": [{"name": "Syllabus", "modname": "page"}]},
        ]
        main(["contents", "STAT120"])
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
        mock.get_courses.return_value = [{"id": 1, "shortname": "ECON101"}]
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
        mock.get_courses.return_value = [{"id": 1, "shortname": "ECON101"}]
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
        mock.get_courses.return_value = [{"id": 1, "shortname": "ECON101"}]
        mock.get_course_contents.return_value = sections
        main(["--json", "materials", "1", "--week", "1"])
        output = json.loads(capsys.readouterr().out)
        assert len(output) == 1
        assert output[0]["file_name"] == "w1.pdf"

    @patch("worsaga.cli._client")
    def test_materials_empty(self, mock_client_fn, capsys):
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "ECON101"}]
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
        mock.get_courses.return_value = [{"id": 1, "shortname": "ECON101"}]
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
        mock.get_courses.return_value = [{"id": 1, "shortname": "ECON101"}]
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
        mock.get_courses.return_value = [{"id": 1, "shortname": "ECON101"}]
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
        mock.get_courses.return_value = [{"id": 1, "shortname": "ECON101"}]
        mock.get_course_contents.return_value = sections
        main(["contents", "1"])
        out = capsys.readouterr().out
        assert "Week 1" in out
        assert "Week 2" in out


class TestTokenSource:
    """--token is deprecated but still works; --token-stdin replaces it."""

    TOKEN = "piped-token-value"

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_URL", "https://moodle.example.edu")
        monkeypatch.setenv("WORSAGA_USERID", "5")
        monkeypatch.delenv("WORSAGA_TOKEN", raising=False)
        monkeypatch.delenv("WORSAGA_DEMO", raising=False)

    def _stdin(self, monkeypatch, text):
        import io

        monkeypatch.setattr(sys, "stdin", io.StringIO(text))

    @patch("worsaga.cli.test_connection")
    def test_token_in_argv_still_works_but_warns(self, mock_conn, capsys):
        mock_conn.return_value = {"userid": 5, "username": "u", "sitename": "s"}
        main(["--token", "argv-token", "doctor"])
        captured = capsys.readouterr()
        assert "Warning:" in captured.err
        assert "shell history" in captured.err
        assert "--token-stdin" in captured.err
        # Deprecated, not broken: the command still ran with that token.
        assert mock_conn.call_args[0][0].token == "argv-token"

    @patch("worsaga.cli.test_connection")
    @patch("worsaga.cli.MoodleConfig.write_config")
    def test_setup_token_in_argv_warns(
        self, mock_write, mock_conn, capsys, tmp_path,
    ):
        mock_conn.return_value = {"userid": 42}
        mock_write.return_value = tmp_path / "config.json"
        main(["setup", "--url", "https://m.example.com", "--token", "tok"])
        assert "shell history" in capsys.readouterr().err
        mock_write.assert_called_once()

    @patch("worsaga.cli.test_connection")
    def test_global_token_stdin_feeds_the_client(
        self, mock_conn, monkeypatch, capsys,
    ):
        mock_conn.return_value = {"userid": 5, "username": "u", "sitename": "s"}
        self._stdin(monkeypatch, f"{self.TOKEN}\n")
        main(["--token-stdin", "doctor"])
        assert mock_conn.call_args[0][0].token == self.TOKEN
        captured = capsys.readouterr()
        # No deprecation warning, and the secret is never echoed.
        assert "Warning:" not in captured.err
        assert self.TOKEN not in captured.out
        assert self.TOKEN not in captured.err

    @patch("worsaga.cli.test_connection")
    @patch("worsaga.cli.MoodleConfig.write_config")
    def test_setup_token_stdin_round_trip(
        self, mock_write, mock_conn, monkeypatch, capsys, tmp_path,
    ):
        mock_conn.return_value = {"userid": 42}
        mock_write.return_value = tmp_path / "config.json"
        self._stdin(monkeypatch, f"  {self.TOKEN}  \n")
        main([
            "setup", "--url", "https://m.example.com", "--token-stdin",
        ])
        # Surrounding whitespace and the newline are stripped.
        assert mock_write.call_args[1]["token"] == self.TOKEN
        captured = capsys.readouterr()
        assert self.TOKEN not in captured.out
        assert self.TOKEN not in captured.err

    def test_token_and_token_stdin_conflict(self, monkeypatch, capsys):
        self._stdin(monkeypatch, f"{self.TOKEN}\n")
        with pytest.raises(SystemExit) as exc:
            main(["--token", "argv", "--token-stdin", "doctor"])
        assert exc.value.code == 2
        assert "cannot be used together" in capsys.readouterr().err

    def test_empty_token_value_still_counts_as_a_conflict(
        self, monkeypatch, capsys,
    ):
        """--token "" is still the user putting a token option in argv."""
        self._stdin(monkeypatch, f"{self.TOKEN}\n")
        with pytest.raises(SystemExit) as exc:
            main(["--token", "", "--token-stdin", "doctor"])
        assert exc.value.code == 2
        assert "cannot be used together" in capsys.readouterr().err

    def test_empty_setup_token_value_still_counts_as_a_conflict(
        self, monkeypatch, capsys,
    ):
        self._stdin(monkeypatch, f"{self.TOKEN}\n")
        with pytest.raises(SystemExit) as exc:
            main([
                "setup", "--url", "https://m.example.com",
                "--token", "", "--token-stdin",
            ])
        assert exc.value.code == 2
        assert "cannot be used together" in capsys.readouterr().err

    @patch("worsaga.cli.test_connection")
    def test_utf8_bom_is_stripped_from_the_piped_token(
        self, mock_conn, monkeypatch,
    ):
        """A UTF-8 signature is not part of the token, and leaving it in
        makes Moodle answer with an opaque 'invalid token'."""
        mock_conn.return_value = {"userid": 5, "username": "u", "sitename": "s"}
        self._stdin(monkeypatch, "\ufeff" + self.TOKEN + "\n")
        main(["--token-stdin", "doctor"])
        assert mock_conn.call_args[0][0].token == self.TOKEN

    def test_empty_stdin_is_a_clean_error(self, monkeypatch, capsys):
        self._stdin(monkeypatch, "\n")
        with pytest.raises(SystemExit) as exc:
            main(["--token-stdin", "doctor"])
        assert exc.value.code == 1
        assert "no token" in capsys.readouterr().err

    @patch("worsaga.cli.MoodleConfig.write_config")
    def test_setup_token_stdin_needs_a_url(
        self, mock_write, monkeypatch, capsys,
    ):
        self._stdin(monkeypatch, f"{self.TOKEN}\n")
        with pytest.raises(SystemExit) as exc:
            main(["setup", "--token-stdin"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "--token-stdin" in err and "--url" in err
        mock_write.assert_not_called()

    def test_help_marks_token_deprecated(self, capsys):
        with pytest.raises(SystemExit):
            main(["--help"])
        out = capsys.readouterr().out
        assert "DEPRECATED" in out
        assert "--token-stdin" in out

    def test_setup_help_marks_token_deprecated(self, capsys):
        with pytest.raises(SystemExit):
            main(["setup", "--help"])
        out = capsys.readouterr().out
        assert "DEPRECATED" in out
        assert "--token-stdin" in out


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

    @patch("worsaga.cli._stdin_is_interactive", return_value=True)
    @patch("getpass.getpass", return_value="tok123")
    @patch("builtins.input", side_effect=["https://m.example.com", ""])
    @patch("worsaga.cli.test_connection")
    @patch("worsaga.cli.MoodleConfig.write_config")
    def test_setup_interactive_fallback(self, mock_write, mock_test_conn, mock_input, mock_getpass, mock_tty, capsys, tmp_path):
        """setup without --url/--token should still prompt interactively."""
        mock_test_conn.return_value = {"userid": 42}
        mock_write.return_value = tmp_path / "config.json"
        main(["setup"])
        out = capsys.readouterr().out
        assert "worsaga setup" in out
        assert mock_input.call_count == 2
        mock_getpass.assert_called_once()


class TestSetupNonInteractiveGuard:
    """Setup must never crash with a raw traceback on a non-TTY stdin."""

    @patch("worsaga.cli.MoodleConfig.write_config")
    @patch("worsaga.cli._stdin_is_interactive", return_value=False)
    def test_non_tty_exits_cleanly_with_guidance(
        self, mock_tty, mock_write, capsys,
    ):
        with pytest.raises(SystemExit) as exc:
            main(["setup"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "not a TTY" in err
        # Points at every non-interactive alternative the command supports.
        assert "--url" in err and "--token" in err
        assert "WORSAGA_URL" in err and "WORSAGA_TOKEN" in err
        assert "WORSAGA_CREDS_PATH" in err
        # The existing config file must never be written on this abort path.
        mock_write.assert_not_called()

    @patch("worsaga.cli.MoodleConfig.write_config")
    @patch("worsaga.cli.test_connection")
    @patch("builtins.input", side_effect=EOFError())
    @patch("worsaga.cli._stdin_is_interactive", return_value=True)
    def test_eof_mid_prompt_is_clean(
        self, mock_tty, mock_input, mock_test_conn, mock_write, capsys,
    ):
        with pytest.raises(SystemExit) as exc:
            main(["setup"])
        assert exc.value.code == 1
        assert "aborted" in capsys.readouterr().err.lower()
        mock_test_conn.assert_not_called()
        mock_write.assert_not_called()

    @patch("worsaga.cli.MoodleConfig.write_config")
    @patch("worsaga.cli.test_connection")
    @patch("builtins.input", side_effect=KeyboardInterrupt())
    @patch("worsaga.cli._stdin_is_interactive", return_value=True)
    def test_keyboard_interrupt_mid_prompt_is_clean(
        self, mock_tty, mock_input, mock_test_conn, mock_write, capsys,
    ):
        with pytest.raises(SystemExit) as exc:
            main(["setup"])
        # Ctrl-C uses the codebase's 130 convention, with a one-line message.
        assert exc.value.code == 130
        assert "aborted" in capsys.readouterr().err.lower()
        mock_write.assert_not_called()

    def test_subprocess_stdin_devnull_no_traceback(self, tmp_path):
        """End-to-end: `worsaga setup` with closed stdin never traces back."""
        repo_root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(repo_root / "src")
        # Ensure no ambient credentials divert the interactive fallback.
        # The child escapes the suite's in-process isolation fixture, so an
        # existing throwaway creds file shadows the developer's real
        # platform config outright (an unset or missing WORSAGA_CREDS_PATH
        # would fall through to the real config.json).
        for var in (
            "WORSAGA_URL", "WORSAGA_TOKEN", "WORSAGA_USERID", "WORSAGA_DEMO",
        ):
            env.pop(var, None)
        sandbox_creds = tmp_path / "empty-config.json"
        sandbox_creds.write_text("{}")
        env["WORSAGA_CREDS_PATH"] = str(sandbox_creds)
        proc = subprocess.run(
            [sys.executable, "-m", "worsaga.cli", "setup"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            env=env,
            timeout=60,
        )
        assert proc.returncode == 1
        # The essential guarantee: no raw traceback escapes, on any platform.
        assert "Traceback" not in proc.stderr
        assert "EOFError" not in proc.stderr
        # Either the up-front TTY guard fires (POSIX /dev/null) or the
        # EOF handler catches it (Windows NUL reports isatty() True); both
        # are clean, and both name the non-interactive alternative.
        assert "not a TTY" in proc.stderr or "aborted" in proc.stderr.lower()
        assert "--url" in proc.stderr and "--token" in proc.stderr


class TestWeekNotFoundCli:
    """A week matching no section must fail (exit 1); an empty-but-valid
    week stays a valid answer. Exercised end-to-end through demo mode."""

    NONSENSE = "zzz_nonsense"

    def test_study_pack_unmatched_week_human(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--demo", "study-pack", "ECON101", "--week", self.NONSENSE,
                  "--stdout"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert f"no section matching week '{self.NONSENSE}'" in err
        assert "Available sections:" in err
        assert "Week 3" in err

    def test_study_pack_unmatched_week_json(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--demo", "--json", "study-pack", "ECON101",
                  "--week", self.NONSENSE, "--stdout"])
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["error_code"] == "week_not_found"
        assert self.NONSENSE in payload["error"]
        assert any("Week 3" in n for n in payload["available_sections"])

    def test_summary_unmatched_week_human(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--demo", "summary", "ECON101", "--week", self.NONSENSE])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert f"no section matching week '{self.NONSENSE}'" in err

    def test_summary_unmatched_week_json(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--demo", "--json", "summary", "ECON101",
                  "--week", self.NONSENSE])
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["error_code"] == "week_not_found"

    def test_summary_empty_but_valid_week_succeeds(self, capsys):
        # "revision" matches the empty Revision/Exam section: valid, exit 0.
        main(["--demo", "summary", "ECON101", "--week", "revision"])
        out = capsys.readouterr().out
        assert "Study notes" in out

    def test_materials_unmatched_week_exits_1(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--demo", "materials", "ECON101", "--week", self.NONSENSE])
        assert exc.value.code == 1
        assert f"no section matching week '{self.NONSENSE}'" in capsys.readouterr().err

    def test_materials_empty_but_valid_week_exits_0(self, capsys):
        # A matched section with no downloadable files is a valid empty
        # listing (exit 0), distinguished from week-not-found.
        main(["--demo", "materials", "ECON101", "--week", "revision"])
        out = capsys.readouterr().out
        assert "No materials found" in out
        assert "section found" in out

    def test_materials_valid_week_lists_files(self, capsys):
        main(["--demo", "materials", "ECON101", "--week", "3"])
        assert ".pdf" in capsys.readouterr().out

    def test_download_unmatched_week_exits_1(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--demo", "download", "ECON101", "--week", self.NONSENSE])
        assert exc.value.code == 1
        assert f"no section matching week '{self.NONSENSE}'" in capsys.readouterr().err


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

    @patch("worsaga.cli._stdin_is_interactive", return_value=True)
    @patch("getpass.getpass", return_value="tok123")
    @patch("builtins.input", side_effect=["https://m.example.com", ""])
    @patch("worsaga.cli.test_connection")
    @patch("worsaga.cli.MoodleConfig.write_config")
    def test_interactive_uses_getpass(self, mock_write, mock_test_conn, mock_input, mock_getpass, mock_tty, capsys, tmp_path):
        mock_test_conn.return_value = {"userid": 42}
        mock_write.return_value = tmp_path / "config.json"
        main(["setup"])
        mock_getpass.assert_called_once()
        assert mock_input.call_count == 2

    @patch("worsaga.cli._stdin_is_interactive", return_value=True)
    @patch("getpass.getpass", return_value="tok123")
    @patch("builtins.input", side_effect=["https://m.example.com", "abc"])
    @patch("worsaga.cli.test_connection")
    def test_interactive_rejects_non_numeric_userid(
        self, mock_test_conn, mock_input, mock_getpass, mock_tty, capsys,
    ):
        with pytest.raises(SystemExit) as exc:
            main(["setup"])
        assert exc.value.code == 1
        assert "User ID must be a number" in capsys.readouterr().err
        mock_test_conn.assert_not_called()

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
        args = parser.parse_args(["search", "ECON101", "regression"])
        assert args.command == "search"
        assert args.course == "ECON101"
        assert args.query == "regression"

    @patch("worsaga.cli._client")
    def test_search_json(self, mock_client_fn, capsys):
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "ECON101"}]
        mock.get_course_contents.return_value = self.SECTIONS
        main(["--json", "search", "1", "regression"])
        output = json.loads(capsys.readouterr().out)
        assert len(output) == 2  # section name match + module name match
        names = {r["module_name"] for r in output}
        assert "Regression Slides" in names

    @patch("worsaga.cli._client")
    def test_search_table(self, mock_client_fn, capsys):
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "ECON101"}]
        mock.get_course_contents.return_value = self.SECTIONS
        main(["search", "1", "regression"])
        out = capsys.readouterr().out
        assert "Regression" in out
        assert "Section" in out  # header row

    @patch("worsaga.cli._client")
    def test_search_no_matches(self, mock_client_fn, capsys):
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "ECON101"}]
        mock.get_course_contents.return_value = self.SECTIONS
        main(["search", "1", "nonexistent"])
        out = capsys.readouterr().out
        assert "No matches" in out

    @patch("worsaga.cli._client")
    def test_search_json_no_matches(self, mock_client_fn, capsys):
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "ECON101"}]
        mock.get_course_contents.return_value = self.SECTIONS
        main(["--json", "search", "1", "nonexistent"])
        output = json.loads(capsys.readouterr().out)
        assert output == []

    @patch("worsaga.cli._client")
    def test_search_json_shape(self, mock_client_fn, capsys):
        """Each result should contain section and module context."""
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "ECON101"}]
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
            {"id": 1, "shortname": "ECON101", "fullname": "Econ 100"},
        ]
        main(["--json", "courses"])
        output = json.loads(capsys.readouterr().out)
        assert output[0]["shortname"] == "ECON101"

    @patch("worsaga.cli._client")
    def test_json_after_subcommand(self, mock_client_fn, capsys):
        mock_client_fn.return_value.get_courses.return_value = [
            {"id": 1, "shortname": "ECON101", "fullname": "Econ 100"},
        ]
        main(["courses", "--json"])
        output = json.loads(capsys.readouterr().out)
        assert output[0]["shortname"] == "ECON101"

    @patch("worsaga.cli._client")
    def test_json_after_contents(self, mock_client_fn, capsys):
        mock = mock_client_fn.return_value
        mock.get_courses.return_value = [{"id": 1, "shortname": "ECON101"}]
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
        mock.get_courses.return_value = [{"id": 1, "shortname": "ECON101"}]
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
        mock.get_courses.return_value = [{"id": 1, "shortname": "ECON101"}]
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


class TestProgressFeedback:
    """All-course fan-outs report progress on stderr, never on stdout.

    The shared orchestrators are also used by the MCP server over stdio,
    where stdout is the protocol channel, so progress must be stderr-only
    and suppressed in machine (--json/--yaml) and --quiet modes (Issue 2).
    """

    def test_digest_progress_on_stderr_not_stdout(self, capsys):
        main(["--demo", "digest"])
        captured = capsys.readouterr()
        assert "[1/5]" in captured.err
        assert "[5/5]" in captured.err
        assert "[1/5]" not in captured.out
        assert "[5/5]" not in captured.out

    def test_digest_quiet_suppresses_progress(self, capsys):
        main(["--demo", "-q", "digest"])
        assert "[1/5]" not in capsys.readouterr().err

    def test_digest_json_has_clean_stdout_and_no_progress(self, capsys):
        main(["--demo", "--json", "digest"])
        captured = capsys.readouterr()
        assert "[1/5]" not in captured.err
        # stdout is exactly the JSON payload — nothing else leaked in.
        payload = json.loads(captured.out)
        assert "deadlines" in payload

    def test_sync_per_course_progress_on_stderr_not_stdout(
        self, capsys, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("WORSAGA_CACHE_PATH", str(tmp_path / "cache.db"))
        main(["--demo", "sync"])
        captured = capsys.readouterr()
        # Phase-prefixed per-course/per-forum labels on stderr.
        assert "grades:" in captured.err
        assert "files:" in captured.err
        assert "forums:" in captured.err
        # The human summary is on stdout; progress never leaks there.
        assert "Synced" in captured.out
        assert "files:" not in captured.out

    def test_sync_quiet_suppresses_progress(
        self, capsys, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("WORSAGA_CACHE_PATH", str(tmp_path / "cache.db"))
        main(["--demo", "-q", "sync"])
        assert "files:" not in capsys.readouterr().err


class TestRawFlag:
    """--raw should output the unprocessed Moodle API payload with --json."""

    FAKE_COURSES = [
        {"id": 1, "shortname": "ECON101", "fullname": "Econ 100",
         "enrolledusercount": 300, "idnumber": "ECON101-2526", "visible": 1},
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
        mock.get_courses.return_value = [{"id": 1, "shortname": "ECON101"}]
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
        mock.get_courses.return_value = [{"id": 1, "shortname": "ECON101"}]
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
        assert "ECON101" in out
        # Should NOT be JSON
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


class TestNormalizers:
    """Unit tests for _normalize_courses and _normalize_contents."""

    def test_normalize_courses_strips_extra_fields(self):
        raw = [
            {"id": 1, "shortname": "ECON101", "fullname": "Econ",
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


# ── Legacy-encoding console safety ───────────────────────────────


class TestCp1252ConsoleSafety:
    """Human output must never crash on legacy-encoded stdout (cp1252).

    Windows consoles and pipes frequently use cp1252, which cannot
    encode box-drawing or many content characters. main() reconfigures
    the streams with errors="replace", and table separators are ASCII.
    """

    def _cp1252_stdout(self, monkeypatch):
        import io

        buf = io.BytesIO()
        wrapper = io.TextIOWrapper(buf, encoding="cp1252", errors="strict")
        monkeypatch.setattr(sys, "stdout", wrapper)
        return buf, wrapper

    def test_demo_materials_table_survives_cp1252(self, monkeypatch, capsys):
        buf, wrapper = self._cp1252_stdout(monkeypatch)

        main(["--demo", "materials", "ECON101", "--week", "3"])

        wrapper.flush()
        out = buf.getvalue().decode("cp1252")
        # Full table rendered: header, ASCII separator row, data rows.
        assert "Section" in out
        assert "-" * 10 in out
        assert ".pdf" in out
        assert "?" not in out  # nothing needed replacement
        assert "codec" not in capsys.readouterr().err

    def test_demo_courses_table_survives_cp1252(self, monkeypatch):
        buf, wrapper = self._cp1252_stdout(monkeypatch)

        main(["--demo", "courses"])

        wrapper.flush()
        out = buf.getvalue().decode("cp1252")
        assert "ECON101" in out
        assert "-" * 8 in out

    def test_unencodable_content_is_replaced_not_fatal(self, monkeypatch):
        """Characters outside cp1252 degrade to '?' instead of crashing."""
        buf, wrapper = self._cp1252_stdout(monkeypatch)
        client = MagicMock()
        client.get_courses.return_value = [
            {"id": 1, "shortname": "MATH101", "fullname": "Analysis – ε–δ proofs"},
        ]
        with patch("worsaga.cli._client", return_value=client):
            main(["courses"])

        wrapper.flush()
        out = buf.getvalue().decode("cp1252")
        assert "MATH101" in out
        assert "Analysis" in out  # row still printed
        assert "?" in out  # Greek letters replaced, not fatal


class TestStructuredCourseErrors:
    """--json/--yaml emit the MCP-style error dict for course-resolution
    failures instead of leaving stdout empty (exit stays 1)."""

    def test_json_unknown_course_id_emits_course_not_found(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--demo", "--json", "contents", "999999"])
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["error_code"] == "course_not_found"
        assert "999999" in payload["error"]

    def test_json_unknown_shortname_emits_course_not_found(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--demo", "--json", "grades", "NOTACOURSE"])
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["error_code"] == "course_not_found"

    def test_json_ambiguous_prefix_emits_candidates(self, capsys):
        client = MagicMock()
        client.get_courses.return_value = [
            {"id": 1, "shortname": "PSY110_2526", "fullname": "Intro Psychology"},
            {"id": 2, "shortname": "PSY110_2425", "fullname": "Intro Psychology (2024/25)"},
        ]
        with patch("worsaga.cli._client", return_value=client):
            with pytest.raises(SystemExit) as exc:
                main(["--json", "contents", "PSY1"])
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["error_code"] == "course_ambiguous"
        shortnames = {c["shortname"] for c in payload["candidates"]}
        assert shortnames == {"PSY110_2526", "PSY110_2425"}
        assert all({"id", "shortname", "fullname"} <= set(c) for c in payload["candidates"])

    def test_human_mode_unchanged_stderr_only(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--demo", "contents", "999999"])
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Course 999999 not found" in captured.err


class TestCoursesTableTruncation:
    """The courses table pads with spaces and marks real cuts with '...'
    (the old dotted leader made uncut short codes look truncated)."""

    def test_short_code_padded_with_spaces_not_dots(self, capsys):
        client = MagicMock()
        client.get_courses.return_value = [
            {"id": 1, "shortname": "ECON101", "fullname": "Intro"},
        ]
        with patch("worsaga.cli._client", return_value=client):
            main(["courses"])
        out = capsys.readouterr().out
        assert "ECON101      " in out
        assert "ECON101....." not in out

    def test_overlong_short_code_gets_ellipsis(self, capsys):
        client = MagicMock()
        client.get_courses.return_value = [
            {"id": 1, "shortname": "VERYLONGSHORTCODE_EXCEEDING", "fullname": "X"},
        ]
        with patch("worsaga.cli._client", return_value=client):
            main(["courses"])
        assert "VERYLONGSHORTCODE..." in capsys.readouterr().out


class TestProgressLabelHygiene:
    """Progress labels are decoded/contextualized before display."""

    def test_assignment_labels_are_html_unescaped(self):
        from worsaga.assignments import get_assignments

        client = MagicMock()
        client.get_courses.return_value = [
            {"id": 1, "shortname": "STAT120", "fullname": "Statistics"},
        ]
        client.get_assignments_by_courses.return_value = {
            "courses": [{"id": 1, "assignments": [
                {"id": 11, "course": 1, "name": "Group 1 &amp; 2 Blog", "duedate": 0},
            ]}],
        }
        client.get_assignment_submission_status.return_value = {}
        labels = []
        get_assignments(
            client,
            on_progress=lambda done, total, label: labels.append(label),
        )
        assert labels == ["Group 1 & 2 Blog"]

    def test_updates_labels_carry_course_shortname(self):
        from worsaga.forums import get_latest_updates

        client = MagicMock()
        client.base_url = "https://moodle.example.com"
        client.get_courses.return_value = [
            {"id": 1, "shortname": "STAT120", "fullname": "Statistics"},
        ]
        client.get_forums_by_courses.return_value = [
            {"id": 7, "course": 1, "name": "Announcements"},
        ]
        client.get_forum_discussions.return_value = {"discussions": []}
        labels = []
        get_latest_updates(
            client,
            on_progress=lambda done, total, label: labels.append(label),
        )
        assert labels == ["STAT120: Announcements"]
