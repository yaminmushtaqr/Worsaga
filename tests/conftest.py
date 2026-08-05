"""Suite-wide isolation from the developer's real Moodle account.

Worsaga resolves credentials from the environment and from a
platform-native config file, and its stores default to platform-native
data paths. Without this fixture a test run on a configured machine
reads the maintainer's real `config.json`, and any code path that
reaches `urlopen` sends their real token to their real Moodle site. That
is not hypothetical: it is exactly what an earlier revision of the
auto-sync account binding did, and the test suite is where it happened.

So every test gets, automatically:

- a fake site, token, and user id in the environment;
- `config`'s platform paths (and every module that imported
  `DEFAULT_CONFIG_PATH` by value) redirected into ``tmp_path``;
- cache and index paths redirected into ``tmp_path``;
- a `urlopen` that raises instead of reaching the network.

Tests that legitimately fake transport patch `urlopen` themselves and so
override the guard for their own scope; demo-mode tests never reach it.
"""

import urllib.request

import pytest

import worsaga as worsaga_package
from worsaga import cli as cli_module
from worsaga import config as config_module

#: Deliberately unroutable (RFC 6761 reserves ``.invalid``) and obviously
#: fake, so a value that escapes into an assertion message is recognisable.
FAKE_URL = "https://moodle.test.invalid"
FAKE_TOKEN = "fake-test-token-not-a-real-credential"
FAKE_USERID = "424242"


@pytest.fixture(autouse=True)
def isolate_worsaga_environment(tmp_path, monkeypatch):
    """Point every credential and store path at throwaway test state."""
    monkeypatch.setenv("WORSAGA_URL", FAKE_URL)
    monkeypatch.setenv("WORSAGA_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("WORSAGA_USERID", FAKE_USERID)
    monkeypatch.delenv("WORSAGA_CREDS_PATH", raising=False)
    monkeypatch.delenv("WORSAGA_DEMO", raising=False)

    config_dir = tmp_path / "worsaga-config"
    config_path = config_dir / "config.json"
    monkeypatch.setattr(config_module, "_PLATFORM_CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_module, "_PLATFORM_CONFIG_PATH", config_path)
    # DEFAULT_CONFIG_PATH is imported by value elsewhere, so patching the
    # definition alone would leave those copies pointing at the real file.
    for module in (config_module, cli_module, worsaga_package):
        if hasattr(module, "DEFAULT_CONFIG_PATH"):
            monkeypatch.setattr(module, "DEFAULT_CONFIG_PATH", config_path)

    monkeypatch.setenv("WORSAGA_CACHE_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setenv("WORSAGA_INDEX_PATH", str(tmp_path / "search.db"))

    def _blocked(*args, **kwargs):
        raise AssertionError("unmocked network call in test")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
