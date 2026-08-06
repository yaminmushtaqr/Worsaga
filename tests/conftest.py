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
- cache, index, and operational-state paths redirected into ``tmp_path``;
- a `urlopen` that raises instead of reaching the network;
- a fresh set of per-origin rate coordinators whose pacing sleeps do
  nothing, so wire-level tests neither wait 250 ms per request nor inherit
  a cooldown or a spent retry budget from the test before them;
- an empty output-redaction registry, so a token registered by one test
  cannot silently rewrite another test's expected output;
- an empty record of already-reported relative-override refusals, which
  are warned about once per process.

Tests that legitimately fake transport patch `urlopen` themselves and so
override the guard for their own scope; demo-mode tests never reach it.
"""

import urllib.request

import pytest

import worsaga as worsaga_package
from worsaga import cli as cli_module
from worsaga import config as config_module
from worsaga import ratelimit as ratelimit_module
from worsaga import redact as redact_module

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
    monkeypatch.setenv("WORSAGA_STATE_DIR", str(tmp_path / "state"))
    # Downloads and study packs default here now, so without this a test
    # that exercises the default destination writes real files into the
    # developer's own downloads directory — which is exactly what happened
    # the first time the default moved off the working directory.
    monkeypatch.setenv("WORSAGA_DOWNLOADS_DIR", str(tmp_path / "downloads"))
    monkeypatch.delenv("WORSAGA_MIN_REQUEST_GAP_MS", raising=False)
    monkeypatch.delenv("WORSAGA_MAX_IN_FLIGHT", raising=False)

    # Coordinators live in a module-level registry, so without this a
    # cooldown or a spent retry budget from one test would be inherited by
    # the next. The no-op sleep keeps the real 250 ms pacing out of the
    # suite; tests that assert on pacing build their own coordinator with
    # their own clock.
    ratelimit_module.for_testing_reset(sleep_fn=lambda _seconds: None)

    # The redaction registry is module-level for the same reason the
    # coordinators are: it has to be reachable from every output
    # boundary. Cleared around each test so one test's token never
    # rewrites another's assertions.
    redact_module.forget_secrets()

    # A refused relative override is reported once per process, so without
    # this the second test to try the same bad value would see no warning
    # and fail for a reason that has nothing to do with what it tests.
    config_module._forget_override_warnings()

    def _blocked(*args, **kwargs):
        raise AssertionError("unmocked network call in test")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
    yield
    ratelimit_module.for_testing_reset()
    redact_module.forget_secrets()
    config_module._forget_override_warnings()
