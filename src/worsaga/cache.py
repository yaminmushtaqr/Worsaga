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

Opening a store also runs any one-time migration the database has not
had yet. There is one: :meth:`CacheStore._scrub_stored_feedback` removes
instructor feedback text from grade rows and from recorded grade change
events written before the sync stopped storing it. A privacy default
that only applies to data collected *after* the upgrade would leave the
text sitting in the cache of everybody who already had one, and the
change history is replayed by ``worsaga changes`` indefinitely.

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
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import platformdirs

from worsaga.config import _absolute_override
from worsaga.grademeta import (
    grade_fingerprint,
    legacy_grade_fingerprint,
    scrub_feedback,
)
from worsaga.principal import (
    bind_principal as _bind_principal,
)
from worsaga.principal import (
    principal_meta_key,
)
from worsaga.redact import redact_text
from worsaga.secureio import ensure_private_file

logger = logging.getLogger(__name__)

_APP_NAME = "worsaga"
SCHEMA_VERSION = 1

#: Meta keys recording how far the one-time feedback scrub got, in two
#: phases, because the two halves of it can fail independently: the rows
#: are rewritten inside a transaction, and the free space they used to
#: occupy is reclaimed afterwards by ``VACUUM``, which is not
#: transactional and can fail on its own (a busy database, a full disk).
#: One marker for both would either claim a reclaim that never happened —
#: leaving the words recoverable from the file with nothing left to retry
#: — or redo the whole scrub to retry a rebuild.
FEEDBACK_SCRUB_META_KEY = "grades_feedback_scrubbed"
FEEDBACK_RECLAIM_META_KEY = "grades_feedback_reclaimed"

#: Revision of the scrub. Both markers hold it as an integer: a stored
#: value below this reruns the migration (a corrected revision must reach
#: caches the old one already touched), a value above it means a newer
#: Worsaga has been here and nothing is touched, and anything that is not
#: an integer at all is treated as never-run — the scrub is idempotent,
#: so rerunning it on a corrupt marker costs one pass and no risk.
FEEDBACK_SCRUB_VERSION = 1

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
        self._scrub_stored_feedback()

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

    # ── One-time migrations ───────────────────────────────────────

    def _meta_version(self, key: str) -> int | None:
        """Return *key* as an integer, or None if absent or not one.

        "Not an integer" and "not there" are answered the same way on
        purpose: a marker nobody can read says nothing about what has
        been done to the database, and the migration it guards is safe to
        repeat.
        """
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,),
        ).fetchone()
        if row is None:
            return None
        try:
            return int(str(row[0]).strip())
        except (TypeError, ValueError):
            return None

    def _set_meta(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    def _scrub_stored_feedback(self) -> None:
        """Remove instructor feedback text left in rows written earlier.

        The sync stopped persisting feedback text, but that only governs
        what is written from then on. Two places keep the old text
        indefinitely without this:

        - a cached grade row for an item that never appears in another
          snapshot (a finished module, a course the user is no longer
          enrolled on) is never rewritten, so it keeps its text forever;
        - every recorded ``grade_updated`` event holds a before/after
          diff view that carried the feedback *field* when it was
          recorded, and ``worsaga changes`` replays that history to a
          person or an agent for as long as the cache exists.

        So both tables are scrubbed once, here, when a database that has
        not had it is opened. Grade rows are also re-fingerprinted to the
        current shape from the scrubbed payload, which is what makes the
        next sync a straight comparison: a feedback-only edit made across
        the upgrade is detected on the first sync afterwards rather than
        adopted as an unreadable older shape.

        **Two phases, two markers.** Rewriting the rows and reclaiming the
        space they used are separate promises that fail separately, so
        each records itself (see :data:`FEEDBACK_SCRUB_META_KEY`). Phase
        one is the row rewrite, and its marker is written *inside* the
        same transaction, so it can never claim a scrub that did not land.
        Phase two is ``VACUUM``, and its marker is written only after that
        succeeded — a rebuild that failed is retried on a later open,
        on its own, rather than being remembered as done. That matters
        because ``VACUUM`` here is not housekeeping: SQLite leaves the
        *old*, longer record in the free space of its page, so until the
        file is rebuilt the instructor's words are still in it, findable
        by anything that reads it as bytes.

        Nothing here may stop a cache from opening. Any failure — a busy
        database, an unreadable row, a full disk — rolls back, is logged
        at debug level, and leaves the markers saying what is genuinely
        still outstanding.

        It deliberately runs before :meth:`bind_principal`, so a cache
        that belongs to another account is scrubbed too. The migration
        only ever *deletes* somebody's text; it reads nothing out, writes
        nothing new, and doing it for a person whose account does not
        match this run is exactly what that person would want done with
        their gradebook comments.
        """
        try:
            scrubbed = self._meta_version(FEEDBACK_SCRUB_META_KEY)
            reclaimed = self._meta_version(FEEDBACK_RECLAIM_META_KEY)
        except sqlite3.Error as exc:  # pragma: no cover - unreadable meta
            logger.debug("could not read the cache migration markers: %s", exc)
            return
        if scrubbed is not None and scrubbed > FEEDBACK_SCRUB_VERSION:
            # Written by a newer Worsaga. It knows things this build does
            # not; leave its work alone.
            return
        if scrubbed == FEEDBACK_SCRUB_VERSION:
            if reclaimed is None or reclaimed < FEEDBACK_SCRUB_VERSION:
                self._reclaim_scrubbed_space()
            return
        self._run_feedback_scrub()

    def _run_feedback_scrub(self) -> None:
        """Phase one: rewrite the rows, in one transaction with its marker.

        The markers are read **again** here, inside the write lock. The
        check that sent us here was unlocked, and between the two another
        process may have scrubbed this cache and had its rebuild fail — in
        which case this transaction finds nothing left carrying feedback
        and must not read that as "a clean cache, nothing to reclaim".
        Doing so would record a reclaim nobody performed and leave the old
        records in free space with nothing left to retry them. Only a
        transaction that itself finds no phase-one marker *and* no
        feedback-bearing row may claim that.
        """
        items = events = 0
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            scrubbed = self._meta_version(FEEDBACK_SCRUB_META_KEY)
            reclaimed = self._meta_version(FEEDBACK_RECLAIM_META_KEY)
            if scrubbed is not None and scrubbed > FEEDBACK_SCRUB_VERSION:
                # A newer Worsaga got here while we were looking.
                self._conn.execute("COMMIT")
                return
            first = scrubbed is None
            if scrubbed != FEEDBACK_SCRUB_VERSION:
                items = self._scrub_feedback_from_items()
                events = self._scrub_feedback_from_changes()
                self._set_meta(FEEDBACK_SCRUB_META_KEY, FEEDBACK_SCRUB_VERSION)
                if first and not (items or events):
                    # This transaction found an unscrubbed cache with
                    # nothing to scrub, so there is no free space holding
                    # anything: phase two is finished before it starts, and
                    # recording that keeps a clean cache from rebuilding
                    # itself on every open forever.
                    self._set_meta(
                        FEEDBACK_RECLAIM_META_KEY, FEEDBACK_SCRUB_VERSION,
                    )
                    self._conn.execute("COMMIT")
                    return
            self._conn.execute("COMMIT")
        except Exception as exc:
            # Broad on purpose. A pathological row can raise things SQLite
            # never would (``RecursionError`` out of ``json``), and a
            # migration that let one of those escape would abort the
            # constructor with a transaction still open.
            logger.debug("deferred the stored-feedback scrub: %s", exc)
            self._rollback_quietly()
            return
        if items or events:
            logger.debug(
                "removed stored feedback text from %d grade rows and %d "
                "change events in %s", items, events, self.path,
            )
        if reclaimed is None or reclaimed < FEEDBACK_SCRUB_VERSION:
            self._reclaim_scrubbed_space()

    def _reclaim_scrubbed_space(self) -> None:
        """Phase two: rebuild the file, then record that it was rebuilt.

        Retried on a later open if it fails, which is why its marker is
        separate. If the rebuild succeeds and the marker write does not,
        the next open rebuilds an already-clean file: wasted work once,
        never a false claim.
        """
        try:
            self._conn.execute("VACUUM")
            self._set_meta(FEEDBACK_RECLAIM_META_KEY, FEEDBACK_SCRUB_VERSION)
        except Exception as exc:
            logger.debug(
                "could not rebuild %s after scrubbing; will retry on the "
                "next open: %s", self.path, exc,
            )

    def _rollback_quietly(self) -> None:
        try:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - nothing left to do
            pass

    def _decode_row(self, stored: Any) -> Any:
        """Return one stored JSON column, or None when it cannot be read.

        ``RecursionError`` is caught alongside the parse errors because
        deeply nested JSON raises it rather than a ``ValueError``, and one
        pathological row must cost that row, not the migration.
        """
        try:
            return json.loads(stored)
        except (TypeError, ValueError, RecursionError) as exc:
            logger.debug("skipped an unreadable row during migration: %s", exc)
            return None

    def _encode_row(self, value: Any) -> str | None:
        """Return the JSON to store, or None when it will not encode."""
        try:
            return json.dumps(value, sort_keys=True, default=str)
        except (TypeError, ValueError, RecursionError) as exc:
            logger.debug("skipped a row that would not re-encode: %s", exc)
            return None

    def _scrub_feedback_from_items(self) -> int:
        """Scrub cached grade payloads; returns how many were rewritten.

        Only the ``grades`` category, only rows that actually carry a
        ``feedback`` key, and only the payload and the fingerprint: the
        timestamps are left alone because nothing about the item changed
        in Moodle. A row that will not parse is skipped rather than
        deleted — an unreadable row is already inert, and the next sync
        overwrites it.

        The fingerprint is rewritten to the current shape only when the
        stored payload can be *proved* to be the one its fingerprint was
        computed from. An older Worsaga fingerprinted the record it
        fetched and then stored a sanitized copy, so where the sanitizer
        edited the feedback the cached words are not the words Moodle
        sent, and a hash of them can never equal the hash the next sync
        computes from the live text — re-fingerprinting such a row would
        announce a feedback change nobody made. Recomputing the legacy
        fingerprint over the stored payload
        (:func:`worsaga.grademeta.legacy_grade_fingerprint`) settles it
        exactly: equal means faithful, and anything else keeps its stored
        fingerprint and takes the sync's existing migration path instead —
        adopted quietly, still judged on the fields that did not move.
        Guessing from the text itself was the alternative, and it
        mistook honest feedback containing ``***`` for an edited row.
        """
        rows = self._conn.execute(
            "SELECT site, item_key, fingerprint, payload FROM items"
            " WHERE category = 'grades'"
        ).fetchall()
        rewritten = 0
        for site, item_key, fingerprint, stored in rows:
            payload = self._decode_row(stored)
            if not isinstance(payload, dict) or "feedback" not in payload:
                continue
            faithful = fingerprint == legacy_grade_fingerprint(payload)
            scrubbed = scrub_feedback(payload)
            text = self._encode_row(scrubbed)
            if text is None:
                continue
            self._conn.execute(
                "UPDATE items SET payload = ?, fingerprint = ?"
                " WHERE site = ? AND category = 'grades' AND item_key = ?",
                (
                    text,
                    grade_fingerprint(scrubbed) if faithful else fingerprint,
                    site,
                    item_key,
                ),
            )
            rewritten += 1
        return rewritten

    def _scrub_feedback_from_changes(self) -> int:
        """Scrub recorded grade events; returns how many were rewritten.

        The feedback field lives inside the ``before``/``after`` diff
        views, and is replaced there by the same presence flag and hash a
        current event carries — so a replayed old event has the shape an
        agent reads today, minus the words.
        """
        rows = self._conn.execute(
            "SELECT id, detail FROM changes"
            " WHERE category = 'grades' OR kind = 'grade_updated'"
        ).fetchall()
        rewritten = 0
        for row_id, stored in rows:
            detail = self._decode_row(stored)
            if not isinstance(detail, dict):
                continue
            scrubbed = dict(detail)
            touched = False
            for side in ("before", "after"):
                view = scrubbed.get(side)
                if isinstance(view, dict) and "feedback" in view:
                    scrubbed[side] = scrub_feedback(view)
                    touched = True
            if not touched:
                continue
            text = self._encode_row(scrubbed)
            if text is None:
                continue
            self._conn.execute(
                "UPDATE changes SET detail = ? WHERE id = ?", (text, row_id),
            )
            rewritten += 1
        return rewritten

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
