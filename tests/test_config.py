"""Tests for configuration loading and config file management."""

import json
import os

import pytest

from worsaga.config import (
    DEFAULT_CONFIG_PATH,
    MoodleConfig,
    _PLATFORM_CONFIG_DIR,
    _PLATFORM_CONFIG_PATH,
    _find_config_file,
)


class TestConfigLoad:
    def test_explicit_args_override_everything(self, tmp_path, monkeypatch):
        # Write a config file with different values
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"url": "https://file.example.com", "token": "file_tok"}))
        monkeypatch.setenv("WORSAGA_URL", "https://env.example.com")
        monkeypatch.setenv("WORSAGA_TOKEN", "env_tok")

        cfg = MoodleConfig.load(url="https://explicit.example.com", token="explicit_tok", creds_path=cfg_file)
        assert cfg.url == "https://explicit.example.com"
        assert cfg.token == "explicit_tok"

    def test_env_vars_override_file(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"url": "https://file.example.com", "token": "file_tok"}))
        monkeypatch.setenv("WORSAGA_URL", "https://env.example.com")
        monkeypatch.setenv("WORSAGA_TOKEN", "env_tok")

        cfg = MoodleConfig.load(creds_path=cfg_file)
        assert cfg.url == "https://env.example.com"
        assert cfg.token == "env_tok"

    def test_file_loading(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WORSAGA_URL", raising=False)
        monkeypatch.delenv("WORSAGA_TOKEN", raising=False)
        monkeypatch.delenv("WORSAGA_USERID", raising=False)
        monkeypatch.delenv("WORSAGA_CREDS_PATH", raising=False)

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"url": "https://file.example.com", "token": "file_tok", "userid": 99}))

        cfg = MoodleConfig.load(creds_path=cfg_file)
        assert cfg.url == "https://file.example.com"
        assert cfg.token == "file_tok"
        assert cfg.userid == 99

    def test_missing_url_raises(self, monkeypatch):
        monkeypatch.delenv("WORSAGA_URL", raising=False)
        monkeypatch.delenv("WORSAGA_TOKEN", raising=False)
        monkeypatch.delenv("WORSAGA_CREDS_PATH", raising=False)
        with pytest.raises(ValueError, match="Moodle URL not configured"):
            MoodleConfig.load(creds_path="/nonexistent/path.json")

    def test_missing_token_raises(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_URL", "https://example.com")
        monkeypatch.delenv("WORSAGA_TOKEN", raising=False)
        monkeypatch.delenv("WORSAGA_CREDS_PATH", raising=False)
        with pytest.raises(ValueError, match="Moodle token not configured"):
            MoodleConfig.load(creds_path="/nonexistent/path.json")

    def test_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_URL", "https://example.com/moodle/")
        monkeypatch.setenv("WORSAGA_TOKEN", "tok")
        cfg = MoodleConfig.load()
        assert cfg.url == "https://example.com/moodle"

    def test_userid_from_env(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_URL", "https://example.com")
        monkeypatch.setenv("WORSAGA_TOKEN", "tok")
        monkeypatch.setenv("WORSAGA_USERID", "42")
        cfg = MoodleConfig.load()
        assert cfg.userid == 42

    def test_explicit_credentials_ignore_corrupt_file(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{not valid json")
        monkeypatch.delenv("WORSAGA_URL", raising=False)
        monkeypatch.delenv("WORSAGA_TOKEN", raising=False)
        monkeypatch.delenv("WORSAGA_USERID", raising=False)

        cfg = MoodleConfig.load(
            url="https://explicit.example.com",
            token="explicit_tok",
            creds_path=cfg_file,
        )

        assert cfg.url == "https://explicit.example.com"
        assert cfg.token == "explicit_tok"
        assert cfg.userid == 0

    def test_explicit_credentials_ignore_invalid_file_userid(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"userid": "not-an-int"}))
        monkeypatch.delenv("WORSAGA_URL", raising=False)
        monkeypatch.delenv("WORSAGA_TOKEN", raising=False)
        monkeypatch.delenv("WORSAGA_USERID", raising=False)

        cfg = MoodleConfig.load(
            url="https://explicit.example.com",
            token="explicit_tok",
            creds_path=cfg_file,
        )

        assert cfg.url == "https://explicit.example.com"
        assert cfg.token == "explicit_tok"
        assert cfg.userid == 0


class TestFindConfigFile:
    def test_explicit_path_found(self, tmp_path):
        f = tmp_path / "my.json"
        f.write_text("{}")
        assert _find_config_file(f) == f

    def test_explicit_path_missing_returns_none(self):
        assert _find_config_file("/nonexistent/abc.json") is None

    def test_env_creds_path(self, tmp_path, monkeypatch):
        f = tmp_path / "env.json"
        f.write_text("{}")
        monkeypatch.setenv("WORSAGA_CREDS_PATH", str(f))
        assert _find_config_file() == f

    def test_no_files_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.delenv("WORSAGA_CREDS_PATH", raising=False)
        # Patch the module-level paths to nonexistent locations
        import worsaga.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "_PLATFORM_CONFIG_PATH", tmp_path / "nope1.json")
        assert _find_config_file() is None


class TestWriteConfig:
    def test_writes_valid_json(self, tmp_path):
        dest = tmp_path / "sub" / "config.json"
        result = MoodleConfig.write_config(
            url="https://example.com/moodle/",
            token="abc123",
            userid=7,
            path=dest,
        )
        assert result == dest
        assert dest.exists()
        data = json.loads(dest.read_text())
        assert data["url"] == "https://example.com/moodle"
        assert data["token"] == "abc123"
        assert data["userid"] == 7

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions not on Windows")
    def test_file_permissions_are_600(self, tmp_path):
        dest = tmp_path / "config.json"
        MoodleConfig.write_config(url="https://x.com", token="t", path=dest)
        mode = oct(dest.stat().st_mode & 0o777)
        assert mode == "0o600"

    def test_write_config_does_not_crash_on_windows(self, tmp_path, monkeypatch):
        """Simulates Windows (os.name == 'nt') — chmod should be skipped."""
        monkeypatch.setattr(os, "name", "nt")
        dest = tmp_path / "config.json"
        result = MoodleConfig.write_config(url="https://x.com", token="t", path=dest)
        assert result == dest
        assert dest.exists()
        data = json.loads(dest.read_text())
        assert data["url"] == "https://x.com"
        assert data["token"] == "t"

    def test_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WORSAGA_URL", raising=False)
        monkeypatch.delenv("WORSAGA_TOKEN", raising=False)
        monkeypatch.delenv("WORSAGA_USERID", raising=False)
        monkeypatch.delenv("WORSAGA_CREDS_PATH", raising=False)

        dest = tmp_path / "config.json"
        MoodleConfig.write_config(url="https://m.example.com", token="tok", userid=5, path=dest)
        cfg = MoodleConfig.load(creds_path=dest)
        assert cfg.url == "https://m.example.com"
        assert cfg.token == "tok"
        assert cfg.userid == 5


class TestPlatformDirsIntegration:
    """Phase 5: platformdirs-based config path resolution."""

    def test_platform_config_path_used_when_no_env(self, monkeypatch, tmp_path):
        """When no WORSAGA_CREDS_PATH is set, _find_config_file checks the
        platformdirs path."""
        monkeypatch.delenv("WORSAGA_CREDS_PATH", raising=False)

        import worsaga.config as cfg_mod

        platform_cfg = tmp_path / "platform" / "config.json"
        platform_cfg.parent.mkdir(parents=True)
        platform_cfg.write_text(json.dumps({"url": "https://p.example.com", "token": "ptok"}))

        monkeypatch.setattr(cfg_mod, "_PLATFORM_CONFIG_PATH", platform_cfg)

        assert _find_config_file() == platform_cfg

    def test_env_creds_path_overrides_platformdirs(self, monkeypatch, tmp_path):
        """$WORSAGA_CREDS_PATH still takes highest priority over platformdirs."""
        env_cfg = tmp_path / "env.json"
        env_cfg.write_text(json.dumps({"url": "https://e.example.com", "token": "etok"}))
        monkeypatch.setenv("WORSAGA_CREDS_PATH", str(env_cfg))

        import worsaga.config as cfg_mod

        platform_cfg = tmp_path / "platform" / "config.json"
        platform_cfg.parent.mkdir(parents=True)
        platform_cfg.write_text(json.dumps({"url": "https://p.example.com", "token": "ptok"}))
        monkeypatch.setattr(cfg_mod, "_PLATFORM_CONFIG_PATH", platform_cfg)

        assert _find_config_file() == env_cfg

    def test_default_config_path_uses_platformdirs(self):
        """DEFAULT_CONFIG_PATH should be under the platformdirs directory."""
        assert DEFAULT_CONFIG_PATH == _PLATFORM_CONFIG_PATH
        assert DEFAULT_CONFIG_PATH.parent == _PLATFORM_CONFIG_DIR

    def test_platform_config_dir_is_from_platformdirs(self):
        """_PLATFORM_CONFIG_DIR should match what platformdirs returns."""
        import platformdirs

        expected = platformdirs.user_config_dir("worsaga")
        assert str(_PLATFORM_CONFIG_DIR) == expected


class TestConfigCLIJson:
    """Phase 5: `worsaga config --json` output contract."""

    def test_config_json_includes_required_keys(self, monkeypatch):
        from worsaga.cli import main
        import io

        monkeypatch.delenv("WORSAGA_CREDS_PATH", raising=False)

        # Capture stdout
        buf = io.StringIO()
        monkeypatch.setattr("sys.stdout", buf)

        try:
            main(["config", "--json"])
        except SystemExit:
            pass

        output = json.loads(buf.getvalue())
        assert "config_path" in output
        assert "config_dir" in output
        assert "found" in output
        assert "os" in output
        assert isinstance(output["found"], bool)
        assert output["os"] in ("linux", "windows", "darwin")

    def test_config_json_found_true_when_file_exists(self, monkeypatch, tmp_path):
        from worsaga.cli import main
        import io
        import worsaga.config as cfg_mod

        monkeypatch.delenv("WORSAGA_CREDS_PATH", raising=False)

        platform_cfg = tmp_path / "config.json"
        platform_cfg.write_text("{}")
        monkeypatch.setattr(cfg_mod, "_PLATFORM_CONFIG_PATH", platform_cfg)

        buf = io.StringIO()
        monkeypatch.setattr("sys.stdout", buf)

        try:
            main(["config", "--json"])
        except SystemExit:
            pass

        output = json.loads(buf.getvalue())
        assert output["found"] is True
        assert output["config_path"] == str(platform_cfg)
