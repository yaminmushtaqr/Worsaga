"""Tests for demo mode: fake data, no credentials, no network.

Milestone 1 acceptance: the demo CLI commands work without any
configuration, never touch the real Moodle client or the network, the
demo dataset contains no real identifiers, and demo PDFs are generated
deterministically and clearly marked as fake.
"""

import json
import urllib.request

import pytest

from worsaga import demo as demo_mod
from worsaga.cli import main
from worsaga.demo import (
    DEMO_BASE_URL,
    DemoMoodleClient,
    build_demo_dataset,
    demo_mode_enabled,
    demo_pdf_bytes,
)
from worsaga.extraction import extract_file_text


class _BombClient:
    """Stand-in that fails the test if the real client is ever built."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("real MoodleClient must never be constructed in demo mode")


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """No credentials, no config file, no network, no real client."""
    for var in ("WORSAGA_URL", "WORSAGA_TOKEN", "WORSAGA_USERID", "WORSAGA_DEMO"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("WORSAGA_CREDS_PATH", str(tmp_path / "no-such-config.json"))
    monkeypatch.setattr("worsaga.cli.MoodleClient", _BombClient)

    def _no_network(*args, **kwargs):
        raise AssertionError("demo mode must never open a network connection")

    monkeypatch.setattr(urllib.request, "urlopen", _no_network)


# ── Acceptance commands ────────────────────────────────────────────


class TestAcceptanceCommands:
    def test_demo_courses(self, capsys):
        main(["--demo", "courses"])
        out = capsys.readouterr().out
        for code in ("ECON101", "CS210", "PSY110", "STAT120"):
            assert code in out

    def test_demo_deadlines(self, capsys):
        main(["--demo", "deadlines"])
        out = capsys.readouterr().out
        assert "Problem Set 3" in out
        assert "ECON101" in out

    def test_demo_materials_week3(self, capsys):
        main(["--demo", "materials", "ECON101", "--week", "3"])
        out = capsys.readouterr().out
        # Long file names are truncated with an ASCII '...' indicator so a
        # cut name is never mistaken for the whole name (Issue 4).
        assert "ECON101-week3-lecture-slide..." in out

    def test_demo_notifications_sender_truncated_with_ellipsis(self, capsys):
        # The notifications "Sender" column is 20 wide; the demo sender
        # "Worsaga Demo University" (23 chars) must show an ASCII '...'
        # indicator rather than a silent hard slice ("Worsaga Demo Univers").
        main(["--demo", "notifications"])
        out = capsys.readouterr().out
        assert "Worsaga Demo Univ..." in out
        assert "Worsaga Demo Univers " not in out

    def test_demo_summary_week3(self, capsys):
        main(["--demo", "summary", "ECON101", "--week", "3", "-q"])
        out = capsys.readouterr().out
        assert "Study notes" in out
        assert "Elasticity" in out

    def test_demo_flag_after_subcommand(self, capsys):
        main(["courses", "--demo"])
        assert "ECON101" in capsys.readouterr().out

    def test_env_var_enables_demo(self, capsys, monkeypatch):
        monkeypatch.setenv("WORSAGA_DEMO", "1")
        assert demo_mode_enabled()
        main(["courses"])
        assert "ECON101" in capsys.readouterr().out

    def test_env_var_falsy_values_ignored(self, monkeypatch):
        for value in ("0", "false", "", "no"):
            monkeypatch.setenv("WORSAGA_DEMO", value)
            assert not demo_mode_enabled()


# ── Wider command surface ──────────────────────────────────────────


class TestOtherCommands:
    def test_demo_doctor(self, capsys):
        main(["--demo", "doctor"])
        out = capsys.readouterr().out
        assert "demo" in out.lower()
        assert "demo.student" in out

    def test_demo_digest(self, capsys):
        main(["--demo", "digest"])
        out = capsys.readouterr().out
        assert "assignments" in out

    def test_demo_grades(self, capsys):
        main(["--demo", "grades"])
        out = capsys.readouterr().out
        assert "Problem Set 2" in out

    def test_demo_calendar_week(self, capsys):
        main(["--demo", "calendar", "ECON101", "--week", "3"])
        out = capsys.readouterr().out
        assert "Week 3 workshop" in out

    def test_demo_download(self, capsys, tmp_path):
        main([
            "--demo", "download", "ECON101", "--week", "3",
            "--match", "lecture-slides", "--output", str(tmp_path), "-q",
        ])
        saved = list(tmp_path.glob("*.pdf"))
        assert len(saved) == 1
        assert saved[0].read_bytes().startswith(b"%PDF")


# ── JSON output ────────────────────────────────────────────────────


class TestJsonOutput:
    def test_courses_json(self, capsys):
        main(["--demo", "--json", "courses"])
        payload = json.loads(capsys.readouterr().out)
        assert {c["shortname"] for c in payload} == {
            "ECON101", "CS210", "PSY110", "STAT120",
        }

    def test_materials_json_stable_fields(self, capsys):
        main(["--demo", "--json", "materials", "ECON101", "--week", "3"])
        payload = json.loads(capsys.readouterr().out)
        assert payload
        for record in payload:
            assert record["dedupe_key"]
            assert record["module_id"]
            assert "time_modified" in record

    def test_materials_json_omits_file_urls_by_default(self, capsys):
        main(["--demo", "--json", "materials", "ECON101", "--week", "3"])
        payload = json.loads(capsys.readouterr().out)
        assert payload
        for record in payload:
            assert "file_url" not in record

    def test_materials_json_include_file_urls_flag(self, capsys):
        main([
            "--demo", "--json", "materials", "ECON101", "--week", "3",
            "--include-file-urls",
        ])
        payload = json.loads(capsys.readouterr().out)
        assert payload
        assert any(record.get("file_url") for record in payload)

    def test_summary_json(self, capsys):
        main(["--demo", "--json", "summary", "ECON101", "--week", "3"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["bullets"]
        assert payload["course_id"] == 101

    def test_deadlines_json(self, capsys):
        main(["--demo", "--json", "deadlines"])
        payload = json.loads(capsys.readouterr().out)
        assert payload
        assert all(d["days_left"] >= 0 for d in payload)


# ── Isolation guarantees ───────────────────────────────────────────


class TestIsolation:
    def test_call_always_raises(self):
        client = DemoMoodleClient()
        with pytest.raises(RuntimeError, match="offline"):
            client.call("core_webservice_get_site_info")

    def test_download_refuses_non_demo_urls(self):
        client = DemoMoodleClient()
        assert client.download_file("https://moodle.example.com/pluginfile.php/x.pdf") is None

    def test_summary_pipeline_makes_no_network_calls(self, capsys):
        # _isolated_env patches urlopen to raise; summary downloads and
        # extracts four PDFs, so this proves the whole pipeline is offline.
        main(["--demo", "summary", "ECON101", "--week", "3", "-q"])
        assert "Study notes" in capsys.readouterr().out


class TestDemoUnknownCourse:
    """The demo client mirrors Moodle's not-found behaviour for course ids
    that are not enrolled, so error paths are exercised offline."""

    def test_get_course_contents_unknown_course_raises(self):
        from worsaga.client import CourseNotFoundError

        client = DemoMoodleClient()
        with pytest.raises(CourseNotFoundError) as exc_info:
            client.get_course_contents(999999)
        assert exc_info.value.course_id == 999999

    def test_get_user_grade_items_unknown_course_raises(self):
        from worsaga.client import CourseNotFoundError

        client = DemoMoodleClient()
        with pytest.raises(CourseNotFoundError):
            client.get_user_grade_items(999999)

    def test_known_course_returns_grade_dict_never_raises(self):
        # A known course id resolves to a grade payload (possibly empty),
        # never a not-found error — the not-found guard keys off enrolment,
        # not gradebook contents.
        client = DemoMoodleClient()
        for course in client.get_courses():
            payload = client.get_user_grade_items(course["id"])
            assert isinstance(payload, dict)
            assert "usergrades" in payload

    def test_cli_contents_unknown_course_friendly_error(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--demo", "contents", "999999"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "Course 999999 not found" in err
        assert "data record" not in err  # raw Moodle wording never surfaces


# ── Dataset hygiene ────────────────────────────────────────────────


ALLOWED_HOSTS = {"moodle.demo.invalid", "example.com", "www.example.com"}


def _walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk_strings(v)


class TestDatasetHygiene:
    def test_all_urls_use_reserved_hosts(self):
        import urllib.parse

        dataset = build_demo_dataset(now=1_800_000_000)
        for text in _walk_strings(dataset):
            for token in text.split():
                if token.startswith(("http://", "https://")):
                    host = urllib.parse.urlparse(token).netloc.lower()
                    assert host in ALLOWED_HOSTS, f"unexpected host in demo data: {token}"

    def test_course_codes_are_fictional_set(self):
        dataset = build_demo_dataset(now=1_800_000_000)
        assert [c["shortname"] for c in dataset["courses"]] == [
            "ECON101", "CS210", "PSY110", "STAT120",
        ]

    def test_no_email_addresses(self):
        dataset = build_demo_dataset(now=1_800_000_000)
        for text in _walk_strings(dataset):
            assert "@" not in text

    def test_dataset_deterministic_for_fixed_now(self):
        assert build_demo_dataset(now=1_800_000_000) == build_demo_dataset(
            now=1_800_000_000
        )

    def test_deadlines_land_inside_default_window(self):
        client = DemoMoodleClient()
        from worsaga.deadlines import get_upcoming_deadlines

        deadlines = get_upcoming_deadlines(client, lookahead_days=14)
        names = {d["name"] for d in deadlines}
        assert "Problem Set 3" in names
        assert all(0 <= d["days_left"] <= 14 for d in deadlines)


# ── Fake PDFs ──────────────────────────────────────────────────────


class TestDemoPdfs:
    def test_pdf_bytes_deterministic(self):
        demo_pdf_bytes.cache_clear()
        first = demo_pdf_bytes("ECON101-week3-lecture-slides.pdf")
        demo_pdf_bytes.cache_clear()
        second = demo_pdf_bytes("ECON101-week3-lecture-slides.pdf")
        assert first == second
        assert first.startswith(b"%PDF")

    def test_every_registered_pdf_extracts_with_fake_marker(self):
        for filename in demo_mod._DEMO_PDFS:
            data = demo_pdf_bytes(filename)
            text = extract_file_text(data, filename)
            assert "FAKE DEMO DATA" in text, filename

    def test_marker_never_reaches_summary_bullets(self, capsys):
        main(["--demo", "--json", "summary", "ECON101", "--week", "3"])
        payload = json.loads(capsys.readouterr().out)
        for bullet in payload["bullets"]:
            assert "FAKE DEMO DATA" not in bullet
            assert "placeholder" not in bullet.lower()

    def test_material_filesize_matches_generated_pdf(self):
        client = DemoMoodleClient()
        sections = client.get_course_contents(101)
        for section in sections:
            for module in section.get("modules", []):
                for content in module.get("contents", []):
                    if content.get("type") == "file":
                        assert content["filesize"] == len(
                            demo_pdf_bytes(content["filename"])
                        )


# ── MCP demo mode ──────────────────────────────────────────────────


class TestMcpDemo:
    @pytest.fixture(autouse=True)
    def _demo_env(self, monkeypatch):
        pytest.importorskip("mcp")
        from worsaga import mcp_server

        monkeypatch.setenv("WORSAGA_DEMO", "1")
        mcp_server._client = None
        yield
        mcp_server._client = None

    def test_uses_demo_client(self):
        from worsaga import mcp_server

        assert isinstance(mcp_server._get_client(), DemoMoodleClient)

    def test_list_courses_structured(self):
        from worsaga import mcp_server

        result = mcp_server.list_courses()
        assert isinstance(result, list)
        assert {c["shortname"] for c in result} == {
            "ECON101", "CS210", "PSY110", "STAT120",
        }

    def test_get_deadlines_structured(self):
        from worsaga import mcp_server

        result = mcp_server.get_deadlines()
        assert isinstance(result, list)
        assert any(d["name"] == "Problem Set 3" for d in result)

    def test_get_week_materials_structured(self):
        from worsaga import mcp_server

        result = mcp_server.get_week_materials(101, "3")
        assert isinstance(result, list)
        assert any(
            r["file_name"] == "ECON101-week3-lecture-slides.pdf" for r in result
        )

    def test_get_weekly_summary_structured(self):
        from worsaga import mcp_server

        result = mcp_server.get_weekly_summary(101, "3")
        assert isinstance(result, dict)
        assert result["bullets"]
        assert result["formatted"]

    def test_get_digest_structured(self):
        from worsaga import mcp_server

        result = mcp_server.get_digest()
        assert isinstance(result, dict)
        assert result["assignments"]
        assert result["warnings"] == []

    def test_download_material_structured(self, tmp_path, monkeypatch):
        from worsaga import mcp_server

        # MCP downloads are confined to the Worsaga downloads directory.
        monkeypatch.setattr(
            mcp_server, "default_downloads_dir", lambda: tmp_path,
        )
        result = mcp_server.download_material(101, "3", match="lecture-slides")
        assert "error" not in result
        assert result["bytes_written"] > 0
        assert (tmp_path / result["file_name"]).exists()


# ── Base URL sanity ────────────────────────────────────────────────


def test_demo_base_url_is_reserved_tld():
    assert DEMO_BASE_URL.endswith(".invalid")
