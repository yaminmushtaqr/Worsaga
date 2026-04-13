"""Configuration loading for worsaga.

Resolution order:
1. Explicit constructor arguments
2. Environment variables (WORSAGA_URL, WORSAGA_TOKEN, WORSAGA_USERID)
3. JSON credentials file at WORSAGA_CREDS_PATH (env var) or default path

Default config file location:
- $WORSAGA_CREDS_PATH (if set)
- platformdirs.user_config_dir("worsaga")/config.json  (platform-native)

No secrets are hardcoded.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import platformdirs

_APP_NAME = "worsaga"
_PLATFORM_CONFIG_DIR = Path(platformdirs.user_config_dir(_APP_NAME))
_PLATFORM_CONFIG_PATH = _PLATFORM_CONFIG_DIR / "config.json"
DEFAULT_CONFIG_PATH = _PLATFORM_CONFIG_PATH


def _find_config_file(explicit: str | Path | None = None) -> Path | None:
    """Return the first config file that exists, or None."""
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None

    env_creds = os.environ.get("WORSAGA_CREDS_PATH", "")
    if env_creds:
        p = Path(env_creds)
        if p.is_file():
            return p

    if _PLATFORM_CONFIG_PATH.is_file():
        return _PLATFORM_CONFIG_PATH

    return None


def _load_config_file(path: Path) -> dict:
    """Read a JSON config file and return its contents as a dict."""
    with open(path) as f:
        return json.load(f)


@dataclass(frozen=True)
class MoodleConfig:
    url: str
    token: str
    userid: int = 0

    @classmethod
    def load(
        cls,
        *,
        url: str | None = None,
        token: str | None = None,
        userid: int | None = None,
        creds_path: str | Path | None = None,
    ) -> MoodleConfig:
        """Build config from explicit args > env vars > creds file."""
        env_url = os.environ.get("WORSAGA_URL", "")
        env_token = os.environ.get("WORSAGA_TOKEN", "")
        env_userid = os.environ.get("WORSAGA_USERID", "")

        file_url = ""
        file_token = ""
        file_userid = 0

        path = _find_config_file(creds_path)
        if path is not None:
            creds = _load_config_file(path)
            file_url = creds.get("url", "")
            file_token = creds.get("token", "")
            file_userid = int(creds.get("userid", 0))

        resolved_url = url or env_url or file_url
        resolved_token = token or env_token or file_token
        resolved_userid = userid if userid is not None else (int(env_userid) if env_userid else file_userid)

        if not resolved_url:
            raise ValueError(
                "Moodle URL not configured. Set WORSAGA_URL env var, "
                "pass url= to MoodleConfig.load(), or provide a creds file.\n"
                "Run 'worsaga setup' for guided configuration."
            )
        if not resolved_token:
            raise ValueError(
                "Moodle token not configured. Set WORSAGA_TOKEN env var, "
                "pass token= to MoodleConfig.load(), or provide a creds file.\n"
                "Run 'worsaga setup' for guided configuration."
            )

        return cls(
            url=resolved_url.rstrip("/"),
            token=resolved_token,
            userid=resolved_userid,
        )

    @staticmethod
    def write_config(
        url: str,
        token: str,
        userid: int = 0,
        path: Path | None = None,
    ) -> Path:
        """Write credentials to a JSON config file. Returns the path written."""
        dest = path or DEFAULT_CONFIG_PATH
        dest.parent.mkdir(parents=True, exist_ok=True)
        payload = {"url": url.rstrip("/"), "token": token, "userid": userid}
        with open(dest, "w") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        # Set owner-only permissions where the OS supports it.
        # On Windows, POSIX chmod is a no-op or unavailable, so skip it.
        if os.name != "nt":
            dest.chmod(0o600)
        return dest


def test_connection(config: MoodleConfig | None = None) -> dict:
    """Verify credentials by calling core_webservice_get_site_info.

    Returns the site info dict on success, raises on failure.
    """
    from worsaga.client import MoodleClient

    client = MoodleClient(config=config or MoodleConfig.load())
    return client.call("core_webservice_get_site_info")
