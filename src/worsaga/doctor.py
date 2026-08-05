"""Shared connection-check orchestration for the CLI and MCP surfaces.

The CLI ``doctor`` command and the MCP ``get_connection_info`` tool both
need to answer the same cheap question — *am I authenticated, to which
Moodle site, as which user?* — without pulling any course data. This module
holds the single implementation they share: fetch
``core_webservice_get_site_info`` (or the offline demo stand-in), classify
auth/network failures, and normalise the result into a compact, token-free
record.

Nothing here ever exposes the web-service token, an authenticated URL, or
the contents of a credentials file — only the site's base URL and, for a
file-backed config, the file *path*.
"""

from __future__ import annotations

import os
import urllib.error
from typing import Any

from worsaga.client import MoodleRequestError, MoodleWriteAttemptError
from worsaga.config import _find_config_file
from worsaga.models import as_int

# Moodle ``errorcode`` values that mean "the credentials were rejected"
# rather than "the network/server was unreachable". Moodle localises the
# human message but keeps the errorcode stable, so classify on it.
_AUTH_ERRORCODES = frozenset({
    "invalidtoken", "accessexception", "invalidlogin", "tokenexpired",
})


class ConnectionCheckError(RuntimeError):
    """A connection check failed for an expected auth/network reason.

    ``code`` is ``"auth"`` (credentials rejected) or ``"network"`` (site
    unreachable) — the same vocabulary as ``DownloadError.code`` — so the
    MCP tool can surface a structured ``{"error", "error_code"}`` dict. The
    message never contains a token or an authenticated URL.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _is_auth_error(exc: MoodleRequestError) -> bool:
    """Return True when a Moodle exception payload signals rejected auth."""
    code = str(getattr(exc, "errorcode", "") or "").lower()
    if code in _AUTH_ERRORCODES:
        return True
    message = str(exc).lower()
    return "invalid token" in message or "invalidtoken" in message


def fetch_site_info(client: Any) -> dict[str, Any]:
    """Return the raw ``core_webservice_get_site_info`` dict for *client*.

    Offline demo clients (``is_demo``) answer from their built-in stand-in;
    a live client makes the single cheap web-service call. That call is
    memoised on the client and shared with its identity verification, so a
    process makes it once however many surfaces ask. Expected auth and
    network failures are re-raised as :class:`ConnectionCheckError` with a
    token-free message; a blocked-function safety error still propagates.
    """
    if getattr(client, "is_demo", False):
        return client.site_info()
    try:
        return client.site_info()
    except MoodleWriteAttemptError:
        # A read-only safety violation is a bug, not a connection state.
        raise
    except urllib.error.HTTPError as exc:
        code = "auth" if exc.code in (401, 403) else "network"
        raise ConnectionCheckError(
            code, f"Moodle returned HTTP {exc.code} for the site-info check."
        ) from exc
    except urllib.error.URLError as exc:
        raise ConnectionCheckError(
            "network", f"Could not reach Moodle: {exc.reason}"
        ) from exc
    except MoodleRequestError as exc:
        code = "auth" if _is_auth_error(exc) else "network"
        raise ConnectionCheckError(code, str(exc)) from exc
    except (TimeoutError, OSError) as exc:
        raise ConnectionCheckError(
            "network", f"Could not reach Moodle: {exc}"
        ) from exc


def describe_config_source(*, demo_mode: bool) -> tuple[str, str | None]:
    """Return ``(source, path)`` describing where credentials came from.

    ``source`` is ``"demo"`` (offline demo mode), ``"env"`` (``WORSAGA_URL``
    /``WORSAGA_TOKEN`` environment variables), ``"file"`` (a JSON credentials
    file, whose *path* — never its contents — is returned), or ``"unset"``.
    Environment variables take precedence, mirroring
    :meth:`worsaga.config.MoodleConfig.load`.
    """
    if demo_mode:
        return "demo", None
    if os.environ.get("WORSAGA_TOKEN") or os.environ.get("WORSAGA_URL"):
        return "env", None
    path = _find_config_file(None)
    if path is not None:
        return "file", str(path)
    return "unset", None


def build_connection_info(client: Any, *, demo_mode: bool) -> dict[str, Any]:
    """Return a compact, token-free connection-info record for *client*.

    Reuses :func:`fetch_site_info` (raising :class:`ConnectionCheckError` on
    auth/network failure) and :func:`describe_config_source`. The record
    carries only the site *base* URL (no token, no ``/webservice`` paths),
    the site name, the authenticated user's id and display name, the Worsaga
    version, the demo-mode flag, and the config-source hint.
    """
    from worsaga import __version__

    info = fetch_site_info(client)
    source, config_path = describe_config_source(demo_mode=demo_mode)
    display_name = str(info.get("fullname") or info.get("username") or "")
    return {
        "authenticated": True,
        "demo_mode": demo_mode,
        "site_url": str(getattr(client, "base_url", "") or ""),
        "site_name": str(info.get("sitename") or ""),
        "user_id": as_int(info.get("userid"), 0),
        "user_display_name": display_name,
        "worsaga_version": __version__,
        "config_source": source,
        "config_path": config_path,
    }
