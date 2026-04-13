"""Tests for the startup banner."""

import io
import sys
from unittest.mock import patch, MagicMock

import pytest

from worsaga.banner import (
    print_banner,
    should_show_banner,
    _ansi_banner,
    _get_version,
    _use_large_banner,
    _LARGE_BANNER_MIN_WIDTH,
    _rich_banner_large,
    _rich_banner_compact,
)


class TestShouldShowBanner:
    def test_suppressed_in_json_mode(self):
        assert should_show_banner(json_mode=True) is False

    def test_suppressed_in_quiet_mode(self):
        assert should_show_banner(quiet=True) is False

    def test_suppressed_when_both_json_and_quiet(self):
        assert should_show_banner(json_mode=True, quiet=True) is False

    def test_shown_when_tty(self):
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = True
            assert should_show_banner() is True

    def test_suppressed_when_not_tty(self):
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = False
            assert should_show_banner() is False

    def test_force_overrides_non_tty(self):
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = False
            assert should_show_banner(force=True) is True

    def test_force_does_not_override_json(self):
        assert should_show_banner(json_mode=True, force=True) is False

    def test_force_does_not_override_quiet(self):
        assert should_show_banner(quiet=True, force=True) is False


class TestBannerWidthSelection:
    """Tests for large vs compact banner selection based on terminal width."""

    def test_large_banner_on_wide_terminal(self):
        assert _use_large_banner(120) is True

    def test_large_banner_at_threshold(self):
        assert _use_large_banner(_LARGE_BANNER_MIN_WIDTH) is True

    def test_compact_banner_below_threshold(self):
        assert _use_large_banner(_LARGE_BANNER_MIN_WIDTH - 1) is False

    def test_compact_banner_on_narrow_terminal(self):
        assert _use_large_banner(40) is False

    def test_large_banner_on_standard_80_cols(self):
        assert _use_large_banner(80) is True

    def test_compact_banner_on_very_narrow(self):
        assert _use_large_banner(30) is False


class TestRichBannerLarge:
    """Tests for the large ASCII-art Rich banner."""

    def test_large_banner_contains_moodle(self, capsys):
        _rich_banner_large(version="0.2.2")
        out = capsys.readouterr().out
        # The ASCII art spells out MOODLE with block chars
        assert "███" in out

    def test_large_banner_contains_subtitle(self, capsys):
        _rich_banner_large(version="0.2.2")
        out = capsys.readouterr().out
        assert "l m s" in out

    def test_large_banner_contains_version(self, capsys):
        _rich_banner_large(version="1.2.3")
        out = capsys.readouterr().out
        assert "1.2.3" in out

    def test_large_banner_contains_tagline(self, capsys):
        _rich_banner_large(version="0.2.2")
        out = capsys.readouterr().out
        assert "Study kit for university LMSs" in out


class TestRichBannerCompact:
    """Tests for the compact Rich banner (narrow terminals)."""

    def test_compact_banner_contains_name(self, capsys):
        _rich_banner_compact(version="0.2.2")
        out = capsys.readouterr().out
        assert "worsaga" in out.lower()

    def test_compact_banner_contains_version(self, capsys):
        _rich_banner_compact(version="1.2.3")
        out = capsys.readouterr().out
        assert "1.2.3" in out

    def test_compact_banner_contains_tagline(self, capsys):
        _rich_banner_compact(version="0.2.2")
        out = capsys.readouterr().out
        assert "Study kit for university LMSs" in out


class TestPrintBanner:
    def test_ansi_banner_contains_name(self, capsys):
        _ansi_banner(version="0.2.0")
        out = capsys.readouterr().out
        assert "worsaga" in out

    def test_ansi_banner_contains_version(self, capsys):
        _ansi_banner(version="1.2.3")
        out = capsys.readouterr().out
        assert "1.2.3" in out

    def test_ansi_banner_contains_tagline(self, capsys):
        _ansi_banner(version="0.2.0")
        out = capsys.readouterr().out
        assert "Study kit for university LMSs" in out

    def test_print_banner_force_ansi(self, capsys):
        """force_ansi=True should use ANSI path even when Rich is available."""
        print_banner(force_ansi=True)
        out = capsys.readouterr().out
        assert "worsaga" in out

    def test_print_banner_rich_path(self, capsys):
        """Default path should use Rich (if available) and produce output."""
        print_banner()
        out = capsys.readouterr().out
        # Should contain some banner output regardless of width
        assert len(out) > 0

    def test_print_banner_wide_uses_large(self, capsys):
        """Wide terminal should produce the large ASCII-art banner."""
        print_banner(width=120)
        out = capsys.readouterr().out
        assert "███" in out
        assert "l m s" in out

    def test_print_banner_narrow_uses_compact(self, capsys):
        """Narrow terminal should produce the compact banner."""
        print_banner(width=50)
        out = capsys.readouterr().out
        assert "worsaga" in out.lower()
        # Should NOT contain ASCII art blocks
        assert "███" not in out

    def test_ansi_banner_no_json_contamination(self, capsys):
        """ANSI banner should not produce valid JSON."""
        _ansi_banner(version="0.2.0")
        out = capsys.readouterr().out
        import json
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)

    def test_rich_large_banner_no_json_contamination(self, capsys):
        """Large Rich banner should not produce valid JSON."""
        _rich_banner_large(version="0.2.2")
        out = capsys.readouterr().out
        import json
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


class TestGetVersion:
    def test_returns_string(self):
        ver = _get_version()
        assert isinstance(ver, str)
        assert len(ver) > 0

    def test_matches_package_dunder_version(self):
        """_get_version() must return the same value as __version__ in __init__.py."""
        from worsaga import __version__
        assert _get_version() == __version__

    def test_matches_pyproject_version(self):
        """__version__ must stay in sync with pyproject.toml."""
        import tomllib
        from pathlib import Path
        from worsaga import __version__
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        if not pyproject.exists():
            pytest.skip("pyproject.toml not found (installed package)")
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        assert __version__ == data["project"]["version"]

    def test_fallback_when_init_import_fails(self):
        """If __version__ import fails, fall back to importlib.metadata."""
        with patch("worsaga.__version__", new=None):
            # Even with __version__ set to None, the function reads it;
            # test the deeper fallback by patching the import itself.
            pass
        # Patch the entire worsaga import path inside _get_version
        with patch.dict("sys.modules", {"worsaga": None}):
            ver = _get_version()
            # Should fall back to importlib.metadata or "dev"
            assert isinstance(ver, str)
            assert len(ver) > 0

    def test_fallback_on_all_errors(self):
        with patch.dict("sys.modules", {"worsaga": None}), \
             patch("importlib.metadata.version", side_effect=Exception("no pkg")):
            ver = _get_version()
        assert ver == "dev"


class TestBannerInCLI:
    """Integration tests: banner appears/disappears in the right CLI contexts."""

    def test_no_subcommand_shows_banner_on_tty(self, capsys):
        """Running with no subcommand on a TTY should show the banner."""
        with patch("sys.stdout") as mock_stdout:
            # Make isatty return True but still capture output
            mock_stdout.isatty.return_value = True
            # We can't fully test this with capsys + mock, so test the predicate
            from worsaga.banner import should_show_banner
            assert should_show_banner() is True

    def test_no_subcommand_no_banner_in_json(self, capsys):
        """worsaga --json should not produce banner output."""
        from worsaga.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["--json"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        # Should have help text but no banner decorations
        assert "worsaga" not in out or "┌" not in out

    def test_no_subcommand_no_banner_in_quiet(self, capsys):
        """worsaga -q should not produce banner output."""
        from worsaga.cli import main
        with pytest.raises(SystemExit) as exc:
            main(["-q"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        # No Rich panel or ANSI box drawing from banner
        assert "┌" not in out

    @patch("getpass.getpass", return_value="tok123")
    @patch("builtins.input", side_effect=["https://m.example.com", ""])
    @patch("worsaga.cli.test_connection")
    @patch("worsaga.cli.MoodleConfig.write_config")
    def test_interactive_setup_no_banner_when_quiet(
        self, mock_write, mock_test_conn, mock_input, mock_getpass, capsys, tmp_path
    ):
        """Interactive setup with -q should not show banner."""
        from worsaga.cli import main
        mock_test_conn.return_value = {"userid": 42}
        mock_write.return_value = tmp_path / "config.json"
        main(["-q", "setup"])
        out = capsys.readouterr().out
        # The banner should not appear
        assert "┌" not in out
        # But setup text should still appear
        assert "worsaga setup" in out
