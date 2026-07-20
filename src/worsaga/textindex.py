"""Local full-text search index over extracted course material text.

The index lives in the platform-native user data directory
(``platformdirs.user_data_dir("worsaga")/search.db``), separate from the
metadata cache. It stores per-page extracted *text* from supported
course materials (PDF, PPTX, DOCX, TXT) in a SQLite FTS5 table so
``worsaga search-text`` and the MCP ``search_text`` tool can answer
content queries locally, with no network access.

Building the index (``build_text_index``) fetches files through the
authenticated client in memory — nothing is written to disk except the
index database itself. Documents are fingerprinted by material identity
(name, size, modification time), so re-running the build only fetches
files that changed since the last run.

Token hygiene is a hard invariant at this storage boundary, exactly as
for the metadata cache: raw ``file_url`` values are never stored, the
only URL kept is the token-free ``view_url``, and every stored string
is passed through :func:`worsaga.cache.sanitize_payload` so embedded
``token=``-style values are redacted. On POSIX the database file is
created owner-only (0600). Set ``WORSAGA_INDEX_PATH`` to relocate the
index (used by tests).
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable

import platformdirs

from worsaga.cache import sanitize_payload
from worsaga.client import DownloadError
from worsaga.extraction import (
    MAX_TEXT_PER_FILE,
    SUPPORTED_EXTENSIONS,
    extract_file_structured,
)
from worsaga.materials import extract_materials, get_section_materials

if TYPE_CHECKING:
    from worsaga.client import MoodleClient

_APP_NAME = "worsaga"
SCHEMA_VERSION = 1

#: Default cap on files fetched per build run. Unchanged files are
#: skipped without a fetch, so repeated runs make incremental progress
#: even when a single run cannot cover everything.
INDEX_MAX_FILES_PER_RUN = 100

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    doc_key TEXT NOT NULL,
    course_id INTEGER NOT NULL,
    course_shortname TEXT NOT NULL DEFAULT '',
    section_name TEXT NOT NULL DEFAULT '',
    module_name TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL DEFAULT '',
    file_type TEXT NOT NULL DEFAULT '',
    view_url TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0,
    indexed_at INTEGER NOT NULL,
    UNIQUE (site, doc_key)
);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
    text,
    doc_id UNINDEXED,
    page UNINDEXED,
    tokenize = 'porter unicode61'
);
"""


class TextIndexError(RuntimeError):
    """Raised when the full-text index cannot be created or queried."""


def default_index_path() -> Path:
    """Return the index database path (``WORSAGA_INDEX_PATH`` overrides)."""
    env_path = os.environ.get("WORSAGA_INDEX_PATH", "").strip()
    if env_path:
        return Path(env_path)
    return Path(platformdirs.user_data_dir(_APP_NAME)) / "search.db"


def fts_match_expression(query: str) -> str:
    """Convert a free-form user query into a safe FTS5 MATCH expression.

    Every whitespace-separated term is wrapped in double quotes (with
    embedded quotes doubled), so FTS5 operator characters in user input
    (``"``, ``*``, ``:``, ``-``, parentheses) can never produce a syntax
    error or query-syntax injection. Terms are implicitly ANDed.
    """
    terms = []
    for term in query.split():
        escaped = term.replace('"', '""')
        terms.append(f'"{escaped}"')
    return " ".join(terms)


def material_fingerprint(material: dict[str, Any]) -> str:
    """Return a stable content fingerprint for a material record.

    Based on identity and metadata only (never file bytes), so change
    detection works without downloading: a re-uploaded file changes
    ``time_modified``/``file_size`` and gets re-indexed.
    """
    parts = "|".join(
        str(material.get(field, ""))
        for field in ("dedupe_key", "file_name", "file_size", "time_modified")
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


class TextIndexStore:
    """SQLite FTS5-backed store for per-page material text.

    Writes for one document (delete stale pages, upsert the document
    row, insert fresh pages) are grouped in a single ``BEGIN IMMEDIATE``
    transaction so a crash mid-write can never leave a document row
    pointing at half-written pages.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_index_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.execute("PRAGMA busy_timeout = 10000")
        self._conn.executescript(_SCHEMA)
        try:
            self._conn.executescript(_FTS_SCHEMA)
        except sqlite3.OperationalError as exc:
            self._conn.close()
            raise TextIndexError(
                "This Python's SQLite lacks the FTS5 extension, which "
                "full-text search requires. Rebuild Python against a "
                "SQLite with FTS5 enabled (the python.org builds have it)."
            ) from exc
        self._conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        # Indexed text is course data — owner-only, like the cache.
        # (POSIX only; Windows ACLs are inherited from the profile dir.)
        if os.name != "nt":
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def __enter__(self) -> TextIndexStore:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self._conn.in_transaction:
            self._conn.execute("ROLLBACK")
        self._conn.close()

    # ── Documents ─────────────────────────────────────────────────

    def get_fingerprint(self, site: str, doc_key: str) -> str | None:
        """Return the stored fingerprint for a document, or None."""
        row = self._conn.execute(
            "SELECT fingerprint FROM documents WHERE site = ? AND doc_key = ?",
            (site, doc_key),
        ).fetchone()
        return row[0] if row else None

    def upsert_document(
        self,
        site: str,
        doc_key: str,
        fingerprint: str,
        meta: dict[str, Any],
        pages: list[tuple[int, str]],
        *,
        now: int | None = None,
    ) -> None:
        """Replace one document and its page texts atomically.

        *pages* is ``[(page_number, text), ...]``; empty texts are
        skipped. All stored strings are sanitized here so no caller can
        accidentally persist token-bearing values.
        """
        now = int(time.time()) if now is None else int(now)
        clean = {
            key: sanitize_payload(str(meta.get(key, "")))
            for key in (
                "course_shortname", "section_name", "module_name",
                "file_name", "file_type", "view_url",
            )
        }
        stored_pages = [
            (page, sanitize_payload(text))
            for page, text in pages
            if text and text.strip()
        ]
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT INTO documents (site, doc_key, course_id,"
                " course_shortname, section_name, module_name, file_name,"
                " file_type, view_url, fingerprint, page_count, indexed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (site, doc_key) DO UPDATE SET"
                " course_id = excluded.course_id,"
                " course_shortname = excluded.course_shortname,"
                " section_name = excluded.section_name,"
                " module_name = excluded.module_name,"
                " file_name = excluded.file_name,"
                " file_type = excluded.file_type,"
                " view_url = excluded.view_url,"
                " fingerprint = excluded.fingerprint,"
                " page_count = excluded.page_count,"
                " indexed_at = excluded.indexed_at",
                (
                    site, doc_key, int(meta.get("course_id", 0)),
                    clean["course_shortname"], clean["section_name"],
                    clean["module_name"], clean["file_name"],
                    clean["file_type"], clean["view_url"],
                    fingerprint, len(stored_pages), now,
                ),
            )
            doc_id = self._conn.execute(
                "SELECT id FROM documents WHERE site = ? AND doc_key = ?",
                (site, doc_key),
            ).fetchone()[0]
            self._conn.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))
            self._conn.executemany(
                "INSERT INTO pages (text, doc_id, page) VALUES (?, ?, ?)",
                [(text, doc_id, page) for page, text in stored_pages],
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    # ── Search ────────────────────────────────────────────────────

    def search(
        self,
        site: str,
        query: str,
        *,
        course_id: int | None = None,
        course_shortname: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return best-matching pages for *query*, most relevant first.

        Each hit carries course/section/file context, the 1-based
        ``page``, a bracket-highlighted ``snippet``, and a relevance
        ``score`` (higher is better).
        """
        match = fts_match_expression(query)
        if not match:
            return []
        sql = (
            "SELECT d.course_id, d.course_shortname, d.section_name,"
            " d.module_name, d.file_name, d.file_type, d.view_url,"
            " pages.page, snippet(pages, 0, '[', ']', ' ... ', 12),"
            " bm25(pages)"
            " FROM pages JOIN documents d ON d.id = pages.doc_id"
            " WHERE pages MATCH ? AND d.site = ?"
        )
        params: list[Any] = [match, site]
        if course_id is not None:
            sql += " AND d.course_id = ?"
            params.append(int(course_id))
        if course_shortname:
            sql += " AND lower(d.course_shortname) = lower(?)"
            params.append(course_shortname)
        sql += " ORDER BY bm25(pages) LIMIT ?"
        params.append(max(1, int(limit)))
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            raise TextIndexError(f"search failed: {exc}") from exc
        return [
            {
                "course_id": row[0],
                "course_shortname": row[1],
                "section_name": row[2],
                "module_name": row[3],
                "file_name": row[4],
                "file_type": row[5],
                "view_url": row[6],
                "page": row[7],
                "snippet": row[8],
                # bm25() returns lower-is-better negatives; flip so a
                # bigger score always means a better match.
                "score": round(-row[9], 4),
            }
            for row in rows
        ]

    # ── Stats ─────────────────────────────────────────────────────

    def stats(self, site: str) -> dict[str, Any]:
        """Return index coverage for *site* (documents, pages, courses)."""
        docs, pages, courses, last = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(page_count), 0),"
            " COUNT(DISTINCT course_id), MAX(indexed_at)"
            " FROM documents WHERE site = ?",
            (site,),
        ).fetchone()
        return {
            "documents": int(docs),
            "pages": int(pages),
            "courses": int(courses),
            "last_indexed_at": int(last) if last is not None else None,
        }


# ── Build orchestration ──────────────────────────────────────────


def _supported(material: dict[str, Any]) -> bool:
    name = str(material.get("file_name", ""))
    return PurePosixPath(name).suffix.lower() in SUPPORTED_EXTENSIONS


def build_text_index(
    client: "MoodleClient",
    *,
    course_id: int | None = None,
    week: int | str | None = None,
    index_path: str | Path | None = None,
    max_files: int = INDEX_MAX_FILES_PER_RUN,
    now: int | None = None,
    on_file: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Fetch, extract, and index material text for one or all courses.

    Files are fetched in memory through the authenticated client and
    their per-page text stored in the local FTS5 index. Materials whose
    fingerprint (name, size, modification time) is unchanged since the
    last build are skipped without a fetch. At most *max_files* files
    are fetched per run; when the budget runs out the run reports
    ``budget_exhausted`` and the next run resumes where this one
    stopped.

    Shared by the CLI (``worsaga index``) and the MCP server
    (``build_search_index``). Returns a summary dict — courses covered,
    per-outcome file counts, pages indexed, warnings, and the index
    path. **No tokens or authenticated URLs are stored or returned.**
    """
    started_at = int(time.time()) if now is None else int(now)
    site = client.base_url
    warnings: list[str] = []

    courses = [
        course for course in client.get_courses()
        if course.get("id") and (course_id is None or course["id"] == course_id)
    ]
    if course_id is not None and not courses:
        raise ValueError(f"no enrolled course with id {course_id}")

    indexed = unchanged = failed = unsupported = 0
    pages_indexed = 0
    budget_exhausted = False
    covered: list[dict[str, Any]] = []

    with TextIndexStore(index_path) as store:
        for course in courses:
            cid = course["id"]
            shortname = str(course.get("shortname", ""))
            try:
                sections = client.get_course_contents(cid)
            except Exception as exc:
                warnings.append(f"{shortname or cid}: contents fetch failed: {exc}")
                continue
            if week is None:
                materials = extract_materials(
                    sections, cid, base_url=client.base_url,
                )
            else:
                materials = get_section_materials(
                    sections, cid, week, base_url=client.base_url,
                )
            covered.append({"course_id": cid, "course_shortname": shortname})

            for material in materials:
                if not material.get("file_url") or not _supported(material):
                    unsupported += 1
                    continue
                doc_key = f"{cid}:{material.get('dedupe_key', '')}"
                fingerprint = material_fingerprint(material)
                if store.get_fingerprint(site, doc_key) == fingerprint:
                    unchanged += 1
                    continue
                if indexed + failed >= max_files:
                    budget_exhausted = True
                    break

                file_name = str(material.get("file_name", ""))
                if on_file is not None:
                    on_file(file_name)
                try:
                    data = client.download_file(material["file_url"])
                except DownloadError as exc:
                    failed += 1
                    warnings.append(f"{shortname or cid}: {file_name}: {exc}")
                    continue
                if not data:
                    failed += 1
                    warnings.append(
                        f"{shortname or cid}: {file_name}: empty download"
                    )
                    continue

                result = extract_file_structured(
                    data, file_name, max_chars=MAX_TEXT_PER_FILE, clean=True,
                )
                pages = [
                    (page.get("page", 0), page.get("text", ""))
                    for page in result.get("pages", [])
                ]
                for warning in result.get("warnings", []):
                    warnings.append(f"{shortname or cid}: {file_name}: {warning}")
                store.upsert_document(
                    site, doc_key, fingerprint,
                    {
                        "course_id": cid,
                        "course_shortname": shortname,
                        "section_name": material.get("section_name", ""),
                        "module_name": material.get("module_name", ""),
                        "file_name": file_name,
                        "file_type": result.get("file_type", ""),
                        "view_url": material.get("view_url", ""),
                    },
                    pages,
                    now=started_at,
                )
                indexed += 1
                pages_indexed += sum(1 for _, text in pages if text.strip())
            if budget_exhausted:
                break

        stats = store.stats(site)
        resolved_path = str(store.path)

    if budget_exhausted:
        warnings.append(
            f"file budget reached ({max_files} fetches); run the index "
            "again to continue where this run stopped."
        )

    return {
        "site": site,
        "indexed_at": started_at,
        "courses": covered,
        "files_indexed": indexed,
        "files_unchanged": unchanged,
        "files_failed": failed,
        "files_skipped_unsupported": unsupported,
        "pages_indexed": pages_indexed,
        "budget_exhausted": budget_exhausted,
        "index": stats,
        "warnings": warnings,
        "index_path": resolved_path,
    }


def search_text_index(
    site: str,
    query: str,
    *,
    course_id: int | None = None,
    course_shortname: str | None = None,
    limit: int = 20,
    index_path: str | Path | None = None,
) -> dict[str, Any]:
    """Search the local index (no network) and return hits plus coverage.

    The ``index`` stats let callers distinguish "no match" from "nothing
    indexed yet" and prompt for a ``worsaga index`` run.
    """
    with TextIndexStore(index_path) as store:
        hits = store.search(
            site, query,
            course_id=course_id,
            course_shortname=course_shortname,
            limit=limit,
        )
        stats = store.stats(site)
        resolved_path = str(store.path)
    return {
        "site": site,
        "query": query,
        "hits": hits,
        "index": stats,
        "index_path": resolved_path,
    }


__all__ = [
    "INDEX_MAX_FILES_PER_RUN",
    "TextIndexError",
    "TextIndexStore",
    "build_text_index",
    "default_index_path",
    "fts_match_expression",
    "material_fingerprint",
    "search_text_index",
]
