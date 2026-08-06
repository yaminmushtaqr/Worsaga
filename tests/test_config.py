"""Tests for configuration loading and config file management."""

import json
import os
from pathlib import Path

import pytest

from worsaga import secureio
from worsaga.cache import default_cache_path
from worsaga.config import (
    DEFAULT_CONFIG_PATH,
    MoodleConfig,
    _PLATFORM_CONFIG_DIR,
    _PLATFORM_CONFIG_PATH,
    _find_config_file,
    canonical_moodle_url,
    default_downloads_dir,
    default_state_dir,
)
from worsaga.secureio import SecureWriteError
from worsaga.textindex import default_index_path

#: Every ``WORSAGA_*`` variable that relocates a store, the function that
#: resolves it, and the last path component of the platform default it
#: falls back to. One policy, applied to all of them, so a new store
#: cannot quietly acquire a different one.
_STORE_OVERRIDES = [
    ("WORSAGA_DOWNLOADS_DIR", default_downloads_dir, "downloads"),
    ("WORSAGA_STATE_DIR", default_state_dir, "worsaga"),
    ("WORSAGA_CACHE_PATH", default_cache_path, "cache.db"),
    ("WORSAGA_INDEX_PATH", default_index_path, "search.db"),
]

_STORE_IDS = [name for name, _resolve, _default in _STORE_OVERRIDES]


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

    @pytest.mark.skipif(os.name != "nt", reason="runs on real Windows only")
    def test_write_config_does_not_crash_on_windows(self, tmp_path):
        """POSIX modes do not apply on Windows; the write must still land.

        Runs only on real Windows: patching ``os.name`` on POSIX makes
        ``pathlib`` pick ``WindowsPath``, which cannot be instantiated
        there — the simulation crashes, not the code under test.
        """
        dest = tmp_path / "config.json"
        result = MoodleConfig.write_config(url="https://x.com", token="t", path=dest)
        assert result == dest
        assert dest.exists()
        data = json.loads(dest.read_text())
        assert data["url"] == "https://x.com"
        assert data["token"] == "t"

    def test_write_requests_owner_only_mode(self, tmp_path, monkeypatch):
        """Platform-independent: 0600 is what the create asks for."""
        modes = []
        real_open = os.open

        def spy(path, flags, mode=0o777, **kwargs):
            if flags & os.O_CREAT:  # ignore the read-only directory fsync
                modes.append(mode)
            return real_open(path, flags, mode, **kwargs)

        monkeypatch.setattr(secureio.os, "open", spy)
        MoodleConfig.write_config(
            url="https://x.com", token="t", path=tmp_path / "config.json",
        )
        assert modes == [0o600]

    def test_write_is_atomic_and_leaves_no_temp_file(self, tmp_path):
        dest = tmp_path / "config.json"
        MoodleConfig.write_config(url="https://x.com", token="first", path=dest)
        MoodleConfig.write_config(url="https://x.com", token="second", path=dest)
        assert json.loads(dest.read_text())["token"] == "second"
        assert [p.name for p in tmp_path.iterdir()] == ["config.json"]

    def test_failed_write_keeps_the_previous_credentials(
        self, tmp_path, monkeypatch,
    ):
        dest = tmp_path / "config.json"
        MoodleConfig.write_config(url="https://x.com", token="good", path=dest)
        monkeypatch.setattr(
            secureio.os, "replace",
            lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")),
        )
        with pytest.raises(PermissionError):
            MoodleConfig.write_config(
                url="https://x.com", token="bad", path=dest,
            )
        assert json.loads(dest.read_text())["token"] == "good"
        assert [p.name for p in tmp_path.iterdir()] == ["config.json"]

    def test_refuses_to_write_through_a_symlink(self, tmp_path):
        real = tmp_path / "elsewhere.json"
        real.write_text("original")
        link = tmp_path / "config.json"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            pytest.skip("this environment cannot create symbolic links")
        with pytest.raises(SecureWriteError):
            MoodleConfig.write_config(
                url="https://x.com", token="t", path=link,
            )
        assert real.read_text() == "original"

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


class TestTokenRedaction:
    """The config object is held by every client, so it shows up in
    tracebacks, log records, and debugger dumps. None of them may
    print the token."""

    TOKEN = "s3cr3t-token-value"

    def _config(self):
        return MoodleConfig(
            url="https://moodle.example.edu", token=self.TOKEN, userid=7,
        )

    def test_repr_hides_the_token(self):
        text = repr(self._config())
        assert self.TOKEN not in text
        assert "token='***'" in text

    def test_repr_still_identifies_the_site_and_user(self):
        text = repr(self._config())
        assert "https://moodle.example.edu" in text
        assert "userid=7" in text

    def test_str_hides_the_token(self):
        assert self.TOKEN not in str(self._config())

    def test_format_hides_the_token(self):
        assert self.TOKEN not in f"{self._config()}"

    def test_empty_token_is_shown_as_empty(self):
        text = repr(MoodleConfig(url="https://moodle.example.edu", token=""))
        assert "token=''" in text

    def test_traceback_never_carries_the_token(self):
        import traceback

        config = self._config()
        try:
            raise RuntimeError(f"boom while using {config!r}")
        except RuntimeError:
            text = "".join(traceback.format_exc())
        assert self.TOKEN not in text
        assert "token='***'" in text

    def test_logging_a_config_never_carries_the_token(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            logging.getLogger("worsaga.test").warning(
                "config was %r", self._config(),
            )
        assert self.TOKEN not in caplog.text

    def test_the_token_is_still_readable_on_the_attribute(self):
        assert self._config().token == self.TOKEN


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


class TestHttpsEnforcement:
    def test_https_url_accepted(self):
        cfg = MoodleConfig(url="https://moodle.example.ac.uk", token="t")
        assert cfg.url.startswith("https://")

    def test_http_localhost_accepted_for_development(self):
        MoodleConfig(url="http://localhost:8080/moodle", token="t")
        MoodleConfig(url="http://127.0.0.1/moodle", token="t")

    def test_http_remote_rejected(self):
        with pytest.raises(ValueError, match="https"):
            MoodleConfig(url="http://moodle.example.ac.uk", token="t")

    def test_load_rejects_http_env_url(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_URL", "http://moodle.example.ac.uk")
        monkeypatch.setenv("WORSAGA_TOKEN", "tok")
        monkeypatch.setenv("WORSAGA_USERID", "1")
        with pytest.raises(ValueError, match="https"):
            MoodleConfig.load()

    def test_write_config_rejects_http_url(self, tmp_path):
        with pytest.raises(ValueError, match="https"):
            MoodleConfig.write_config(
                url="http://moodle.example.ac.uk",
                token="tok",
                path=tmp_path / "config.json",
            )


class TestCanonicalMoodleUrl:
    """The base URL must be a plain, canonical origin.

    It is the origin every download is checked against and the key cache
    rows and sync history are stored under, so it has to be exactly one
    unambiguous string per site.
    """

    ACCEPTED = [
        ("http://127.0.0.1", "http://127.0.0.1"),
        ("http://localhost:8080", "http://localhost:8080"),
        ("http://[::1]:8080", "http://[::1]:8080"),
        ("https://moodle.example.ac.uk/site", "https://moodle.example.ac.uk/site"),
    ]

    REJECTED = [
        # 127.example.com and 127.0.0.1.nip.io are ordinary DNS names that
        # a "starts with 127." test would have waved through as local.
        "http://127.example.com",
        "http://127.0.0.1.nip.io",
        "https:///missing-host",
        "https://user:pass@example.com",
        "https://example.com/path?x=1#frag",
        # Empty delimiters are still not a plain site address: truthiness
        # checks on username/query/fragment waved all three through.
        "https://@example.com",
        "https://example.com?",
        "https://example.com#",
    ]

    FIXED_POINTS = [
        "https://moodle.university.example/moodle",
        "https://moodle.example.ac.uk",
        "https://moodle.example.edu/vle/site",
        "http://localhost:8080/moodle",
    ]

    @pytest.mark.parametrize("raw,expected", ACCEPTED)
    def test_accepted_urls_normalise_as_expected(self, raw, expected):
        assert canonical_moodle_url(raw) == expected

    @pytest.mark.parametrize("raw", REJECTED)
    def test_rejected_urls(self, raw):
        with pytest.raises(ValueError):
            canonical_moodle_url(raw)
        with pytest.raises(ValueError):
            MoodleConfig(url=raw, token="t")

    @pytest.mark.parametrize("url", FIXED_POINTS)
    def test_typical_configured_urls_are_fixed_points(self, url):
        # Byte-for-byte stability matters: these strings key cache rows and
        # sync history, and churn would orphan an existing user's cache.
        assert canonical_moodle_url(url) == url
        assert MoodleConfig(url=url, token="t").url == url

    def test_scheme_and_host_are_lowercased(self):
        assert canonical_moodle_url("HTTPS://Moodle.Example.EDU/Moodle") == (
            "https://moodle.example.edu/Moodle"  # path case is preserved
        )

    def test_default_ports_are_dropped(self):
        assert canonical_moodle_url("https://m.example.edu:443/x") == (
            "https://m.example.edu/x"
        )
        assert canonical_moodle_url("http://localhost:80") == "http://localhost"

    def test_non_default_port_is_kept(self):
        assert canonical_moodle_url("https://m.example.edu:8443") == (
            "https://m.example.edu:8443"
        )

    def test_non_http_scheme_rejected(self):
        for url in ("ftp://m.example.edu", "file:///etc/passwd", "m.example.edu"):
            with pytest.raises(ValueError):
                canonical_moodle_url(url)

    def test_write_config_stores_the_canonical_form(self, tmp_path):
        dest = tmp_path / "config.json"
        MoodleConfig.write_config(
            url="HTTPS://Moodle.Example.EDU:443/moodle/", token="t", path=dest,
        )
        assert json.loads(dest.read_text())["url"] == (
            "https://moodle.example.edu/moodle"
        )


class TestDefaultDownloadsDir:
    """The one destination both the CLI and the MCP server resolve.

    It has to be absolute. A relative override would mean two different
    directories — the CLI resolves against the shell's working directory,
    the MCP server against whatever directory its host launched it from —
    which is precisely the drift this shared helper exists to prevent.
    """

    def test_platform_default_is_absolute(self, monkeypatch):
        monkeypatch.delenv("WORSAGA_DOWNLOADS_DIR", raising=False)
        assert default_downloads_dir().is_absolute()

    def test_relative_override_is_refused_and_reported(
        self, tmp_path, monkeypatch, capsys,
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("WORSAGA_DOWNLOADS_DIR", "course-files")
        result = default_downloads_dir()
        # Not resolved against the working directory: that would only
        # make the wrong answer absolute.
        assert result != (tmp_path / "course-files").resolve()
        assert result.is_absolute()
        assert result.name == "downloads"
        err = capsys.readouterr().err
        assert "WORSAGA_DOWNLOADS_DIR must be an absolute path" in err
        assert "course-files" in err

    @pytest.mark.parametrize("value", ["course-files", "./dl", "../dl"])
    def test_relative_override_is_the_same_dir_from_any_cwd(
        self, tmp_path, monkeypatch, value,
    ):
        """The bug this guards.

        The CLI and the MCP server run with unrelated working
        directories, so a relative override named two different places
        and dropped course files wherever an agent host was launched
        from.
        """
        one = tmp_path / "one"
        one.mkdir()
        two = tmp_path / "two"
        two.mkdir()
        monkeypatch.setenv("WORSAGA_DOWNLOADS_DIR", value)

        monkeypatch.chdir(one)
        from_one = default_downloads_dir()
        monkeypatch.chdir(two)
        from_two = default_downloads_dir()

        assert from_one == from_two
        assert from_one.is_absolute()

    def test_absolute_override_is_honoured_from_any_cwd(
        self, tmp_path, monkeypatch,
    ):
        target = tmp_path / "dl"
        monkeypatch.setenv("WORSAGA_DOWNLOADS_DIR", str(target))
        one = tmp_path / "one"
        one.mkdir()
        monkeypatch.chdir(one)
        assert default_downloads_dir() == target.resolve()
        monkeypatch.chdir(tmp_path)
        assert default_downloads_dir() == target.resolve()

    def test_tilde_is_expanded(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_DOWNLOADS_DIR", "~/worsaga-course-files")
        result = default_downloads_dir()
        assert "~" not in str(result)
        assert result == (
            Path.home() / "worsaga-course-files"
        ).resolve()

    def test_whitespace_only_override_falls_back_to_the_default(
        self, monkeypatch,
    ):
        monkeypatch.setenv("WORSAGA_DOWNLOADS_DIR", "   ")
        assert default_downloads_dir().name == "downloads"


class TestStoreOverridesMustBeAbsolute:
    """One policy for every variable that moves a store.

    A relative value is not merely untidy. These paths are read by
    processes with unrelated working directories -- the CLI's is the
    shell's, the MCP server's is whatever its host launched it from, a
    scheduled sync's is the scheduler's -- and the persisted cooldown, the
    credential circuit state, and the sync locks are only machine-wide
    while all of them agree on one file.
    """

    @pytest.mark.parametrize(
        "env_name,resolve,default_name", _STORE_OVERRIDES, ids=_STORE_IDS,
    )
    def test_relative_is_refused_reported_once_and_cwd_independent(
        self, env_name, resolve, default_name, tmp_path, monkeypatch, capsys,
    ):
        one = tmp_path / "one"
        one.mkdir()
        two = tmp_path / "two"
        two.mkdir()
        monkeypatch.setenv(env_name, "worsaga-relative")

        monkeypatch.chdir(one)
        first = resolve()
        monkeypatch.chdir(two)
        second = resolve()

        # The whole point: two callers in two directories, one answer.
        assert first == second
        assert first.is_absolute()
        assert first.name == default_name
        # Not resolved against the working directory -- that would only
        # make the wrong answer absolute.
        assert first != (one / "worsaga-relative")
        assert first != (two / "worsaga-relative")

        err = capsys.readouterr().err
        assert f"{env_name} must be an absolute path" in err
        assert "worsaga-relative" in err
        # Reported once per process per value: a command that resolves
        # four stores must not repeat one mistake four times.
        assert err.count("must be an absolute path") == 1

    @pytest.mark.parametrize(
        "env_name,resolve,default_name", _STORE_OVERRIDES, ids=_STORE_IDS,
    )
    def test_tilde_is_expanded(
        self, env_name, resolve, default_name, monkeypatch, capsys,
    ):
        monkeypatch.setenv(env_name, "~/worsaga-store-under-test")
        result = resolve()
        assert "~" not in str(result)
        assert result == (Path.home() / "worsaga-store-under-test").resolve()
        assert "must be an absolute path" not in capsys.readouterr().err

    @pytest.mark.parametrize(
        "env_name,resolve,default_name", _STORE_OVERRIDES, ids=_STORE_IDS,
    )
    def test_absolute_is_honoured_from_any_cwd(
        self, env_name, resolve, default_name, tmp_path, monkeypatch, capsys,
    ):
        target = tmp_path / "chosen"
        monkeypatch.setenv(env_name, str(target))
        one = tmp_path / "one"
        one.mkdir()
        monkeypatch.chdir(one)
        assert resolve() == target.resolve()
        monkeypatch.chdir(tmp_path)
        assert resolve() == target.resolve()
        assert "must be an absolute path" not in capsys.readouterr().err

    @pytest.mark.parametrize(
        "env_name,resolve,default_name", _STORE_OVERRIDES, ids=_STORE_IDS,
    )
    def test_whitespace_only_override_falls_back_silently(
        self, env_name, resolve, default_name, monkeypatch, capsys,
    ):
        # An empty value is "unset", not "wrong": nothing to report.
        monkeypatch.setenv(env_name, "   ")
        assert resolve().name == default_name
        assert capsys.readouterr().err == ""

    def test_one_bad_variable_does_not_silence_another(
        self, tmp_path, monkeypatch, capsys,
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("WORSAGA_CACHE_PATH", "shared-name")
        monkeypatch.setenv("WORSAGA_INDEX_PATH", "shared-name")
        default_cache_path()
        default_index_path()
        err = capsys.readouterr().err
        assert "WORSAGA_CACHE_PATH must be an absolute path" in err
        assert "WORSAGA_INDEX_PATH must be an absolute path" in err


class TestConfigCommandReportsEveryStore:
    """'worsaga config' is the documented way to find what to delete."""

    def _payload(self, capsys):
        from worsaga.cli import main

        main(["--json", "config"])
        return json.loads(capsys.readouterr().out)

    def test_json_reports_every_resolved_store(self, capsys):
        payload = self._payload(capsys)
        for key in (
            "config_path", "config_dir", "downloads_dir",
            "cache_path", "index_path", "state_dir",
        ):
            assert key in payload, key
            assert Path(payload[key]).is_absolute(), key

    def test_json_reflects_the_active_overrides(self, tmp_path, capsys):
        # conftest points cache/index/state at tmp_path already; the
        # command must report those, not the platform defaults.
        payload = self._payload(capsys)
        assert Path(payload["cache_path"]) == (tmp_path / "cache.db")
        assert Path(payload["index_path"]) == (tmp_path / "search.db")
        assert Path(payload["state_dir"]) == (tmp_path / "state")

    def test_human_output_names_every_store(self, capsys):
        from worsaga.cli import main

        main(["config"])
        out = capsys.readouterr().out
        for label in ("Config dir:", "Downloads:", "Cache:", "Index:",
                      "State dir:"):
            assert label in out, label

    def test_a_refused_relative_override_reports_the_default(
        self, tmp_path, monkeypatch, capsys,
    ):
        """The command that tells you where things are cannot say 'here'.

        A relative override is refused, so what this prints is the
        default location -- and it prints it absolute, because a path
        relative to the shell that happened to run the command is not an
        answer anybody can act on.
        """
        from worsaga.cli import main

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("WORSAGA_INDEX_PATH", "search.db")
        main(["--json", "config"])
        captured = capsys.readouterr()
        index_path = Path(json.loads(captured.out)["index_path"])
        assert index_path.is_absolute()
        assert index_path != (tmp_path / "search.db")
        assert index_path.name == "search.db"
        assert "WORSAGA_INDEX_PATH must be an absolute path" in captured.err

    def test_a_relative_creds_path_is_displayed_resolved(
        self, tmp_path, monkeypatch, capsys,
    ):
        """--creds-path still works relative; it is *shown* absolute.

        One process reads that file, so a relative value is usable in a
        way a shared store's is not. Printing it back unresolved would
        still name a different file to anyone reading the output from
        anywhere else.
        """
        from worsaga.cli import main

        creds = tmp_path / "elsewhere.json"
        creds.write_text(json.dumps({
            "url": "https://moodle.example.edu", "token": "t" * 20,
            "userid": 7,
        }))
        monkeypatch.chdir(tmp_path)
        main(["--json", "--creds-path", "elsewhere.json", "config"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["found"] is True
        assert Path(payload["config_path"]).is_absolute()
        assert Path(payload["config_path"]) == creds.resolve()
