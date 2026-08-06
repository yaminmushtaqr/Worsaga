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

import ipaddress
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import platformdirs

from worsaga.redact import remember_secret
from worsaga.secureio import write_private_file

_APP_NAME = "worsaga"
_PLATFORM_CONFIG_DIR = Path(platformdirs.user_config_dir(_APP_NAME))
_PLATFORM_CONFIG_PATH = _PLATFORM_CONFIG_DIR / "config.json"
DEFAULT_CONFIG_PATH = _PLATFORM_CONFIG_PATH


def default_downloads_dir() -> Path:
    """Platform-native directory where Worsaga saves downloaded materials.

    Used as the MCP download destination so files never land in the
    server process working directory. Not created until first use.
    """
    return Path(platformdirs.user_data_dir(_APP_NAME)) / "downloads"


def default_state_dir() -> Path:
    """Directory for Worsaga's small operational state files.

    Home to the records processes leave for each other rather than for the
    user: the per-origin backpressure cooldown
    (:mod:`worsaga.ratelimit`) and the per-site sync outcome history
    (:mod:`worsaga.syncstate`). Distinct from the cache — losing any of it
    costs at most one over-eager request or one forgotten failure count.

    ``WORSAGA_STATE_DIR`` relocates it (used by tests, and by anyone
    keeping several accounts apart). Not created until first use.
    """
    env_dir = os.environ.get("WORSAGA_STATE_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    return Path(platformdirs.user_data_dir(_APP_NAME))


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


def _is_loopback_host(host: str) -> bool:
    """Return True only for a host that is genuinely this machine.

    ``localhost``, plus anything the stdlib parses as a loopback IP literal
    (127.0.0.0/8 and ``::1``). Name-shape tests are deliberately avoided: a
    ``startswith("127.")`` check accepts ``127.example.com`` and
    ``127.0.0.1.nip.io``, which are ordinary DNS names anyone can register
    and point at a server of their choosing.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def canonical_moodle_url(url: str) -> str:
    """Validate a Moodle base URL and return its canonical origin form.

    Accepts only a plain ``http(s)://host[:port][/path]`` site address:
    user-info, a query string, and a fragment are all rejected, because the
    base URL is the origin every request and download is checked against,
    not a link.

    HTTPS is required for every host that is not this machine. The token is
    sent with every API call and download, so plain HTTP would expose it;
    ``http://`` is permitted only for a genuinely local development
    instance (see :func:`_is_loopback_host`).

    Normalisation is deliberately minimal — the scheme and host are
    lowercased, an explicit default port (``:443`` for https, ``:80`` for
    http) is dropped, and a trailing slash is stripped. The path is
    otherwise preserved byte-for-byte: cache rows and sync history are keyed
    by this string, so an ordinary configured value such as
    ``https://moodle.university.example/moodle`` has to normalise to itself.
    """
    raw = str(url or "").strip()
    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()

    if scheme not in {"http", "https"}:
        raise ValueError(
            f"Moodle URL must start with https:// (got '{raw}'). Use the "
            "address you open Moodle at in a browser, for example "
            "https://moodle.example.edu."
        )
    # ``is not None`` and a raw ``?``/``#`` scan, not truthiness: an empty
    # user-info, query, or fragment ('https://@example.com',
    # 'https://example.com?') is still not a plain site address, and a base
    # URL never legitimately carries either delimiter.
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(
            f"Moodle URL must not carry a user name or password (got "
            f"'{raw}'). Worsaga authenticates with the web-service token "
            "from your configuration, never with URL credentials."
        )
    if "?" in raw or "#" in raw:
        raise ValueError(
            f"Moodle URL must be a plain site address with no query string "
            f"or fragment (got '{raw}'). Use just the site root, for "
            "example https://moodle.example.edu."
        )
    try:
        port = parsed.port
    except ValueError:
        raise ValueError(
            f"Moodle URL has an invalid port (got '{raw}')."
        ) from None

    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError(
            f"Moodle URL must include a host name (got '{raw}'). Use the "
            "address you open Moodle at in a browser, for example "
            "https://moodle.example.edu."
        )
    if scheme == "http" and not _is_loopback_host(host):
        raise ValueError(
            f"Moodle URL must use https:// (got '{raw}'). The API token is "
            "sent with every request, so plain HTTP would expose it. "
            "http:// is allowed only for a Moodle running on this machine "
            "(localhost or a loopback IP address)."
        )

    # urlsplit strips the brackets from an IPv6 host; the netloc needs them
    # back or the rebuilt URL is unparseable.
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if scheme == "https" else 80):
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, parsed.path.rstrip("/"), "", ""))


def _validate_moodle_url(url: str) -> None:
    """Raise ``ValueError`` unless *url* is an acceptable Moodle base URL."""
    canonical_moodle_url(url)


@dataclass(frozen=True)
class MoodleConfig:
    url: str
    # repr=False plus the explicit __repr__ below: this object is held by
    # every client, so it appears in tracebacks, logging calls, and
    # debugger dumps. None of those may print the token.
    token: str = field(repr=False)
    userid: int = 0

    def __repr__(self) -> str:
        """Render the config with the token redacted.

        Deliberately says nothing about the token beyond its presence —
        not its length, not a prefix — so a captured traceback or an
        agent transcript never narrows the search space for the secret.
        """
        marker = "'***'" if self.token else "''"
        return (
            f"MoodleConfig(url={self.url!r}, token={marker}, "
            f"userid={self.userid!r})"
        )

    def __post_init__(self):
        if self.url:
            # Frozen dataclass: normalise in place so every consumer keyed
            # on this string (cache rows, sync history, the download-origin
            # check) sees one canonical spelling of the site.
            object.__setattr__(self, "url", canonical_moodle_url(self.url))
        # Arm the output-boundary redactor from the one place every token
        # arrives, whatever its source (argument, environment, or file).
        # Doing it here rather than at each boundary means a token can
        # never be in use without also being redactable.
        remember_secret(self.token)

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

        resolved_url = url or env_url
        resolved_token = token or env_token
        resolved_userid = userid
        if resolved_userid is None and env_userid:
            resolved_userid = int(env_userid)

        need_file_url = not resolved_url
        need_file_token = not resolved_token
        need_file_userid = resolved_userid is None
        if need_file_url or need_file_token or need_file_userid:
            path = _find_config_file(creds_path)
            if path is not None:
                try:
                    creds = _load_config_file(path)
                    if not isinstance(creds, dict):
                        raise ValueError("credentials file must contain a JSON object")
                except (OSError, json.JSONDecodeError, ValueError):
                    if need_file_url or need_file_token:
                        raise
                    creds = {}

                if need_file_url:
                    resolved_url = creds.get("url", "")
                if need_file_token:
                    resolved_token = creds.get("token", "")
                if need_file_userid:
                    try:
                        resolved_userid = int(creds.get("userid", 0) or 0)
                    except (TypeError, ValueError):
                        if need_file_url or need_file_token:
                            raise
                        resolved_userid = 0

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
            userid=resolved_userid or 0,
        )

    @staticmethod
    def write_config(
        url: str,
        token: str,
        userid: int = 0,
        path: Path | None = None,
    ) -> Path:
        """Write credentials to a JSON config file. Returns the path written.

        The file is created owner-only (0600) *at open*, written whole,
        and renamed into place, so there is never a moment where a
        readable, half-written, or previously world-readable credentials
        file exists on disk. A destination that is a symbolic link is
        refused rather than followed. See :mod:`worsaga.secureio` for the
        primitive and for what those guarantees mean on Windows, where
        POSIX modes do not apply and the file inherits the profile
        directory's ACLs.
        """
        canonical_url = canonical_moodle_url(url)
        dest = path or DEFAULT_CONFIG_PATH
        payload = {"url": canonical_url, "token": token, "userid": userid}
        return write_private_file(
            dest, json.dumps(payload, indent=2) + "\n",
        )


def test_connection(config: MoodleConfig | None = None) -> dict:
    """Verify credentials by calling core_webservice_get_site_info.

    Returns the site info dict on success, raises on failure.
    """
    from worsaga.client import MoodleClient

    client = MoodleClient(config=config or MoodleConfig.load())
    return client.site_info()
