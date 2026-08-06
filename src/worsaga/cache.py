"""Local SQLite cache for synced Moodle metadata.

The cache lives in the platform-native user data directory
(``platformdirs.user_data_dir("worsaga")/cache.db``) and stores only
normalized metadata snapshots and detected change events. Rows are keyed
by site (the Moodle base URL), so demo-mode data never mixes with real
course data.

Token hygiene is a hard invariant at this storage boundary: payloads are
sanitized before every write —

- keys whose name contains ``token`` (``token``, ``wstoken``,
  ``access_token``, ...) and the keys ``file_url``/``fileurl``/``sesskey``
  are dropped recursively;
- string values go through :func:`worsaga.redact.redact_text`, the same
  rule the CLI and MCP output boundaries apply — the configured token in
  any of its encoded spellings, and any ``token``-ish query parameter
  whatever its value, with ``=`` written literally, as ``%3D``, or as
  ``%253D``;
- tuples and sets are converted to sanitized lists.

On POSIX the cache file is created owner-only (0600) *before* SQLite
opens it, so the database and its rollback journal are private from the
first byte, matching the credential file treatment. Set
``WORSAGA_CACHE_PATH`` to relocate the cache (used by tests).

Rows for a site are also bound to the Moodle account that collected
them: :meth:`CacheStore.bind_principal` stamps the verified user id and
refuses a write from a different account for the same site. Reads that
make no network request (``get_recent_changes``, ``read_last_sync_at``)
are deliberately unguarded — see :mod:`worsaga.principal` for why.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import platformdirs

from worsaga.config import _absolute_override
from worsaga.principal import (
    bind_principal as _bind_principal,
)
from worsaga.principal import (
    principal_meta_key,
)
from worsaga.redact import redact_text
from worsaga.secureio import ensure_private_file

_APP_NAME = "worsaga"
SCHEMA_VERSION = 1

#: What to tell a user whose cache belongs to another account.
_CACHE_REMEDY = (
    "Delete that file and run 'worsaga sync' again to rebuild it for this "
    "account, or set WORSAGA_CACHE_PATH to a separate path per account."
)

# Keys that must never be persisted, at any nesting depth. Any key whose
# lowercased name *contains* "token" is dropped as well.
_BANNED_KEYS = {"file_url", "fileurl", "sesskey"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    site TEXT NOT NULL,
    category TEXT NOT NULL,
    item_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL,
    first_seen_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (site, category, item_key)
);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    category TEXT NOT NULL,
    item_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    detected_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changes_site_time
    ON changes (site, detected_at);
CREATE TABLE IF NOT EXISTS category_syncs (
    site TEXT NOT NULL,
    category TEXT NOT NULL,
    last_synced_at INTEGER NOT NULL,
    scope TEXT,
    PRIMARY KEY (site, category)
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    finished_at INTEGER NOT NULL,
    summary TEXT NOT NULL
);
"""


def default_cache_path() -> Path:
    """Return the cache database path (``WORSAGA_CACHE_PATH`` overrides).

    The override must be an absolute path. The cache is guarded by
    interprocess sync locks named after this file, and a relative value
    would give a ``watch`` loop and a scheduled sync different files to
    lock — so a relative one is refused and reported; see
    :func:`worsaga.config._absolute_override`.
    """
    override = _absolute_override(
        "WORSAGA_CACHE_PATH", os.environ.get("WORSAGA_CACHE_PATH", ""),
    )
    if override is not None:
        return override
    # resolve() for the same reason the downloads and state defaults do
    # it: the sync locks are named after this path, and every process on
    # the machine must derive the same name.
    return (Path(platformdirs.user_data_dir(_APP_NAME)) / "cache.db").resolve()


def _is_banned_key(key: Any) -> bool:
    lowered = str(key).lower()
    return lowered in _BANNED_KEYS or "token" in lowered


def sanitize_payload(value: Any) -> Any:
    """Return *value* safe to persist: no token keys, no token values.

    Token-bearing keys are removed at every depth, and tuples and sets
    become sanitized lists so nothing bypasses the walk. String values go
    through :func:`worsaga.redact.redact_text`, the same definition of
    "what a secret looks like" that the CLI and MCP output boundaries use.

    Sharing it matters: this used to carry its own pattern, which matched
    only a literal ``token=value``. A percent-encoded ``token%3D...`` (a
    Moodle link nested inside another link's query) and the configured
    token appearing on its own both slipped past it and were written to
    disk — while the output boundary caught both. Storage and output now
    cannot disagree about what a secret is.

    Keys are dropped rather than rewritten, so ``redact_keys`` stays off:
    rewriting a key named after a token would put it back in the payload.
    """
    if isinstance(value, dict):
        return {
            key: sanitize_payload(item)
            for key, item in value.items()
            if not _is_banned_key(key)
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def read_last_sync_at(site: str, path: str | Path | None = None) -> int | None:
    """Read the most recent sync finish time for *site*, read-only.

    Unlike opening a :class:`CacheStore` (which creates the directory,
    the database file, and the schema as a side effect), this opens the
    existing database with SQLite's ``mode=ro`` and returns ``None``
    when the cache does not exist yet — status-style callers must never
    create state.
    """
    cache_path = Path(path) if path else default_cache_path()
    if not cache_path.is_file():
        return None
    # as_uri() percent-encodes '#', '%', spaces, etc. and renders UNC
    # paths correctly — exactly the encoding SQLite's URI parser
    # decodes. Hand-built URIs truncated at '#' and silently created a
    # new database, breaking the read-only contract.
    try:
        uri = cache_path.resolve().as_uri() + "?mode=ro"
    except ValueError:
        return None
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT MAX(finished_at) FROM sync_runs WHERE site = ?", (site,)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return int(row[0]) if row and row[0] is not None else None


class CacheStore:
    """SQLite-backed store for metadata snapshots and change events.

    Connections run in autocommit mode; callers group writes with
    :meth:`begin_immediate` + :meth:`commit`. ``BEGIN IMMEDIATE``
    serializes concurrent sync transactions so two processes can never
    diff against the same stale state and record duplicate events.

    **Account binding is enforced one layer up.** :meth:`bind_principal`
    is the check, but it is the orchestration layer (``run_sync``, and
    the CLI and MCP surfaces above it) that calls it before writing.
    Reaching for :meth:`upsert_item` or :meth:`record_change` directly
    from library code bypasses the guard entirely. That is a deliberate
    interim shape — threading a principal through every mutation would
    be churn against the per-account store namespacing planned for
    0.9.0, which removes the need for the check at this level.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_cache_path()
        # Cached metadata is course data — owner-only, like credentials.
        # Created before SQLite opens it so the database *and* the
        # rollback journal SQLite derives from its mode are private from
        # the start; a chmod afterwards would have missed the journal and
        # left a readable window. (POSIX modes only; on Windows the file
        # inherits the profile directory's ACLs.)
        ensure_private_file(self.path)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.execute("PRAGMA busy_timeout = 10000")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )

    def __enter__(self) -> CacheStore:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self._conn.in_transaction:
            self._conn.execute("ROLLBACK")
        self._conn.close()

    # ── Transactions ──────────────────────────────────────────────

    def begin_immediate(self) -> None:
        """Open a write transaction now, blocking other writers."""
        self._conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        if self._conn.in_transaction:
            self._conn.execute("COMMIT")

    # ── Account binding ───────────────────────────────────────────

    def get_principal(self, site: str) -> int | None:
        """Return the Moodle user id this cache's *site* rows belong to."""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (principal_meta_key(site),),
        ).fetchone()
        if row is None:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return None

    def holds_site_data(self, site: str) -> bool:
        """Return True if any earlier sync recorded something for *site*."""
        row = self._conn.execute(
            "SELECT 1 FROM category_syncs WHERE site = ?"
            " UNION ALL SELECT 1 FROM items WHERE site = ? LIMIT 1",
            (site, site),
        ).fetchone()
        return row is not None

    def bind_principal(self, site: str, principal: int | None) -> bool:
        """Bind this cache's *site* rows to the authenticated account.

        Called by the sync write path before it writes anything, so a
        cross-account run is refused with the cache untouched. See
        :mod:`worsaga.principal` for the adoption and refusal rules.

        Returns whether the caller may go on to write. ``False`` means
        this run verified no identity while the cache already belongs to
        one, so its rows cannot be attributed to anybody; a mismatch
        raises instead.
        """
        stored = self.get_principal(site)
        stamp = _bind_principal(
            stored=stored,
            principal=principal,
            site=site,
            store_label="sync cache",
            store_path=str(self.path),
            remedy=_CACHE_REMEDY,
            holds_data=self.holds_site_data(site),
        )
        if stamp is not None:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (principal_meta_key(site), str(stamp)),
            )
        return principal is not None or stored is None

    # ── Items ─────────────────────────────────────────────────────

    def get_items(self, site: str, category: str) -> dict[str, dict[str, Any]]:
        """Return cached items as ``{item_key: {fingerprint, payload}}``."""
        rows = self._conn.execute(
            "SELECT item_key, fingerprint, payload FROM items "
            "WHERE site = ? AND category = ?",
            (site, category),
        ).fetchall()
        return {
            key: {"fingerprint": fingerprint, "payload": json.loads(payload)}
            for key, fingerprint, payload in rows
        }

    def upsert_item(
        self,
        site: str,
        category: str,
        item_key: str,
        fingerprint: str,
        payload: dict[str, Any],
        *,
        now: int | None = None,
    ) -> None:
        """Insert or refresh one cached item (payload is sanitized here)."""
        now = int(time.time()) if now is None else int(now)
        text = json.dumps(sanitize_payload(payload), sort_keys=True, default=str)
        self._conn.execute(
            "INSERT INTO items (site, category, item_key, fingerprint, payload,"
            " first_seen_at, last_seen_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (site, category, item_key) DO UPDATE SET"
            " last_seen_at = excluded.last_seen_at,"
            " updated_at = CASE WHEN items.fingerprint = excluded.fingerprint"
            "   THEN items.updated_at ELSE excluded.updated_at END,"
            " fingerprint = excluded.fingerprint,"
            " payload = excluded.payload",
            (site, category, item_key, fingerprint, text, now, now, now),
        )

    # ── Category sync state (baselines) ───────────────────────────

    def get_category_state(
        self, site: str, category: str,
    ) -> dict[str, Any] | None:
        """Return sync state for a category, or None if never synced.

        The row's existence — not the presence of items — is what marks
        a category as baselined, so a legitimately empty first sync
        still finishes baselining.
        """
        row = self._conn.execute(
            "SELECT last_synced_at, scope FROM category_syncs"
            " WHERE site = ? AND category = ?",
            (site, category),
        ).fetchone()
        if row is None:
            return None
        last_synced_at, scope_text = row
        scope = json.loads(scope_text) if scope_text else None
        return {"last_synced_at": int(last_synced_at), "scope": scope}

    def set_category_state(
        self,
        site: str,
        category: str,
        *,
        now: int | None = None,
        scope: list[Any] | None = None,
    ) -> None:
        """Record a successful category sync (and its coverage scope)."""
        now = int(time.time()) if now is None else int(now)
        scope_text = None if scope is None else json.dumps(sorted(scope))
        self._conn.execute(
            "INSERT INTO category_syncs (site, category, last_synced_at, scope)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (site, category) DO UPDATE SET"
            " last_synced_at = excluded.last_synced_at,"
            " scope = excluded.scope",
            (site, category, now, scope_text),
        )

    # ── Changes ───────────────────────────────────────────────────

    def record_change(
        self,
        site: str,
        category: str,
        item_key: str,
        change: dict[str, Any],
        *,
        now: int | None = None,
    ) -> None:
        """Persist one change event (detail is sanitized here)."""
        now = int(time.time()) if now is None else int(now)
        detail = json.dumps(sanitize_payload(change), sort_keys=True, default=str)
        self._conn.execute(
            "INSERT INTO changes (site, category, item_key, kind, detail,"
            " detected_at) VALUES (?, ?, ?, ?, ?, ?)",
            (site, category, item_key, str(change.get("kind", "")), detail, now),
        )

    def get_changes(
        self,
        site: str,
        *,
        since_ts: int | None = None,
        category: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return recorded change events, newest first."""
        query = "SELECT category, item_key, detail FROM changes WHERE site = ?"
        params: list[Any] = [site]
        if since_ts is not None:
            query += " AND detected_at >= ?"
            params.append(int(since_ts))
        if category:
            query += " AND category = ?"
            params.append(category)
        query += " ORDER BY detected_at DESC, id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = self._conn.execute(query, params).fetchall()
        results = []
        for row_category, item_key, detail in rows:
            change = json.loads(detail)
            change["category"] = row_category
            change["item_key"] = item_key
            results.append(change)
        return results

    # ── Sync runs ─────────────────────────────────────────────────

    def record_sync_run(
        self,
        site: str,
        started_at: int,
        finished_at: int,
        summary: dict[str, Any],
    ) -> None:
        self._conn.execute(
            "INSERT INTO sync_runs (site, started_at, finished_at, summary)"
            " VALUES (?, ?, ?, ?)",
            (
                site,
                int(started_at),
                int(finished_at),
                json.dumps(sanitize_payload(summary), sort_keys=True, default=str),
            ),
        )

    def last_sync_at(self, site: str) -> int | None:
        row = self._conn.execute(
            "SELECT MAX(finished_at) FROM sync_runs WHERE site = ?", (site,)
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None
