"""Canary tests for the suite's own isolation (see tests/conftest.py).

If any of these fail, the test suite can see the developer's real Moodle
credentials, and a bug in the code under test can transmit them. That has
happened once already, which is why these exist.
"""

import urllib.request

import pytest

from worsaga import cli as cli_module
from worsaga import config as config_module
from worsaga.cache import default_cache_path
from worsaga.config import MoodleConfig
from worsaga.textindex import default_index_path

from conftest import FAKE_TOKEN, FAKE_URL, FAKE_USERID


class TestCredentialIsolation:
    def test_load_sees_only_the_fake_environment(self):
        config = MoodleConfig.load()
        assert config.url == FAKE_URL
        assert config.token == FAKE_TOKEN
        assert config.userid == int(FAKE_USERID)

    def test_the_fake_site_is_unroutable(self):
        # RFC 6761 reserves .invalid, so even a leaked request goes nowhere.
        assert MoodleConfig.load().url.endswith(".invalid")

    def test_platform_config_path_is_inside_tmp(self, tmp_path):
        assert config_module._PLATFORM_CONFIG_PATH.is_relative_to(tmp_path)
        assert config_module._PLATFORM_CONFIG_DIR.is_relative_to(tmp_path)
        assert config_module.DEFAULT_CONFIG_PATH.is_relative_to(tmp_path)

    def test_modules_holding_a_copy_are_redirected_too(self, tmp_path):
        # cli imported DEFAULT_CONFIG_PATH by value; patching only the
        # definition would have left this one pointing at the real file.
        assert cli_module.DEFAULT_CONFIG_PATH.is_relative_to(tmp_path)

    def test_no_real_config_file_is_visible(self):
        assert not config_module._PLATFORM_CONFIG_PATH.exists()
        assert config_module._find_config_file() is None


class TestStoreIsolation:
    def test_cache_and_index_default_into_tmp(self, tmp_path):
        assert default_cache_path().is_relative_to(tmp_path)
        assert default_index_path().is_relative_to(tmp_path)


class TestNetworkIsolation:
    def test_urlopen_is_blocked(self):
        with pytest.raises(AssertionError, match="unmocked network call"):
            urllib.request.urlopen("https://moodle.test.invalid")

    def test_a_real_client_call_cannot_reach_the_network(self):
        from worsaga.client import MoodleClient

        client = MoodleClient(MoodleConfig.load())
        with pytest.raises(AssertionError, match="unmocked network call"):
            client.site_info()
