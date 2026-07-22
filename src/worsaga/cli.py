"""CLI entrypoint for worsaga.

Usage:
    worsaga courses              List enrolled courses
    worsaga deadlines [--days N] Show upcoming deadlines
    worsaga contents <id|code>   Show course sections & modules
    worsaga materials <id|code>  Discover downloadable materials
    worsaga download <id|code>   Download a material file
    worsaga extract <id|code>    Extract per-page text from a material
    worsaga grades [id|code]     Show grade items
    worsaga assignments [id|code] Show assignment statuses
    worsaga forums <id|code>     Show course forums
    worsaga updates [id|code]    Show recent forum updates
    worsaga notifications        Show Moodle notifications
    worsaga inbox                Show Moodle messages
    worsaga digest               Show a live study digest
    worsaga calendar [id|code]   Show calendar events
    worsaga summary <id|code>    Generate weekly study summary
    worsaga search <id|code> <q> Search course content by keyword
    worsaga index [id|code]      Build the local full-text search index
    worsaga search-text <q>      Search indexed material text (no network)
    worsaga study-pack <id|code> Export a Markdown study pack for a week
    worsaga sync                 Sync metadata to the local cache
    worsaga changes              Show changes detected by syncs
    worsaga watch                Foreground sync loop with notifications
    worsaga auto-sync <action>   Manage the scheduled background sync
    worsaga doctor               Check auth and connectivity
    worsaga config [path]        Show active config file location
    worsaga setup                Guided first-time configuration
    worsaga update               Show how to upgrade safely

Demo mode (no credentials, no network):
    worsaga --demo courses
    worsaga --demo summary ECON101 --week 3
    (or set WORSAGA_DEMO=1)
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.error

from worsaga.banner import print_banner, should_show_banner
from worsaga.assignments import get_assignments as get_assignments_data
from worsaga.calendar import get_calendar_events as get_calendar_events_data
from worsaga.client import MoodleClient
from worsaga.config import (
    DEFAULT_CONFIG_PATH,
    MoodleConfig,
    _PLATFORM_CONFIG_DIR,
    _find_config_file,
    test_connection,
)

from worsaga.deadlines import get_upcoming_deadlines
from worsaga.demo import DemoMoodleClient, demo_mode_enabled
from worsaga.digest import get_digest as get_digest_data
from worsaga.forums import get_course_forums as get_course_forums_data
from worsaga.forums import get_forum_discussions as get_forum_discussions_data
from worsaga.forums import get_latest_updates as get_latest_updates_data
from worsaga.grades import collect_grades as collect_grades_data
from worsaga.grades import get_grade_summary as build_grade_summary
from worsaga.client import DownloadError
from worsaga.extraction import MAX_TEXT_PER_FILE
from worsaga.materials import (
    MaterialSelectionError,
    download_material,
    extract_material_content,
    extract_materials,
    get_section_materials,
    match_section,
    search_course_content,
    select_material,
    strip_file_urls,
)
from worsaga.autosync import (
    DEFAULT_INTERVAL_MINUTES,
    autosync_status,
    install_autosync,
    remove_autosync,
)
from worsaga.output import render_structured, wants_structured
from worsaga.sections import find_best_section, summarize_modules
from worsaga.watch import DEFAULT_WATCH_INTERVAL, MIN_WATCH_INTERVAL, run_watch
from worsaga.studypack import build_study_pack, write_study_pack
from worsaga.summaries import build_weekly_summary, format_bullets
from worsaga.textindex import (
    INDEX_MAX_FILES_PER_RUN,
    build_text_index,
    search_text_index,
)
from worsaga.sync import (
    SYNC_CATEGORIES,
    SYNC_LOOKAHEAD_DAYS,
    get_recent_changes,
    run_sync,
)
from worsaga.messages import get_messages as get_messages_data
from worsaga.messages import get_notifications as get_notifications_data
from worsaga.time_utils import parse_interval, parse_since, timestamp_to_display


PUBLIC_INSTALL_SPEC = "worsaga[mcp]"


class CourseResolutionError(ValueError):
    """Raised when a course identifier cannot be resolved."""


def _non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or a positive integer")
    return number


def _build_parser() -> argparse.ArgumentParser:
    from worsaga.banner import _get_version

    parser = argparse.ArgumentParser(
        prog="worsaga",
        description=(
            "Open-source, local-first, read-only study toolkit for Moodle. "
            "Moodle is the only supported LMS today."
        ),
    )
    parser.add_argument(
        "-V", "--version", action="version",
        version=f"%(prog)s {_get_version()}",
    )
    parser.add_argument(
        "--json", action="store_true", help="Output machine-readable JSON"
    )
    parser.add_argument(
        "--yaml", action="store_true", help="Output machine-readable YAML"
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="Suppress progress output on stderr",
    )
    # Top-level credential overrides — usable by any subcommand.
    parser.add_argument(
        "--url", default=None, metavar="URL",
        help="Moodle site URL (overrides config/env)",
    )
    parser.add_argument(
        "--token", default=None, metavar="TOKEN",
        help="Moodle API token (overrides config/env)",
    )
    parser.add_argument(
        "--userid", default=None, type=int, metavar="ID",
        help="Moodle user ID (overrides config/env)",
    )
    parser.add_argument(
        "--creds-path", default=None, metavar="PATH",
        help="Path to a JSON credentials file",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="Use built-in fake demo data (no credentials, no network)",
    )

    # Shared flags inherited by every subcommand so that --json and --quiet
    # work both before and after the subcommand name.  Using SUPPRESS avoids
    # overwriting a value already set by the top-level parser.
    _shared = argparse.ArgumentParser(add_help=False)
    _shared.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS,
        help="Output machine-readable JSON",
    )
    _shared.add_argument(
        "--yaml", action="store_true", default=argparse.SUPPRESS,
        help="Output machine-readable YAML",
    )
    _shared.add_argument(
        "-q", "--quiet", action="store_true", default=argparse.SUPPRESS,
        help="Suppress progress output on stderr",
    )
    _shared.add_argument(
        "--demo", action="store_true", default=argparse.SUPPRESS,
        help="Use built-in fake demo data (no credentials, no network)",
    )

    sub = parser.add_subparsers(dest="command")

    cs = sub.add_parser("courses", parents=[_shared], help="List enrolled courses")
    cs.add_argument(
        "--raw", action="store_true",
        help="With --json, output the raw Moodle API payload",
    )

    dl = sub.add_parser("deadlines", parents=[_shared], help="Show upcoming deadlines")
    dl.add_argument(
        "--days",
        type=int,
        default=14,
        help="Look-ahead window in days (default: 14)",
    )

    ct = sub.add_parser(
        "contents", parents=[_shared], help="Show course sections and modules",
    )
    ct.add_argument(
        "course",
        help="Moodle course ID (integer) or course short-code (e.g. ECON101)",
    )
    ct.add_argument(
        "--week",
        default=None,
        help="Filter to a specific teaching week (number or name substring)",
    )
    ct.add_argument(
        "--raw", action="store_true",
        help="With --json, output the raw Moodle API payload",
    )

    mt = sub.add_parser(
        "materials", parents=[_shared],
        help="List downloadable materials (use 'download' to fetch files)",
        description=(
            "List downloadable materials for a course (discovery only). "
            "To fetch a file, use 'worsaga download'.\n\n"
            "Raw Moodle file_url values are omitted from JSON output by\n"
            "default because they require token authentication; pass\n"
            "--include-file-urls if you explicitly need them for\n"
            "provenance. Never fetch a file_url directly — use\n"
            "'worsaga download' for authenticated retrieval."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mt.add_argument(
        "course",
        help="Moodle course ID (integer) or course short-code (e.g. ECON101)",
    )
    mt.add_argument(
        "--week",
        default=None,
        help="Filter to a specific teaching week (number or name substring)",
    )
    mt.add_argument(
        "--include-file-urls", action="store_true",
        help="Include raw Moodle file_url values in JSON output (provenance only)",
    )

    dn = sub.add_parser(
        "download", parents=[_shared],
        help="Download a material file (authenticated)",
        description=(
            "Download a material file using authenticated Moodle credentials. "
            "Use 'worsaga materials' first to discover available files, "
            "then pass --match or --index to select one. "
            "The Moodle token is never exposed in the output."
        ),
        epilog=(
            "Example workflow:\n"
            "  worsaga materials ECON101 --week 3          # discover files\n"
            "  worsaga download ECON101 --week 3 --index 0 # fetch one\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    dn.add_argument(
        "course",
        help="Moodle course ID (integer) or course short-code (e.g. ECON101)",
    )
    dn.add_argument(
        "--week", required=True,
        help="Teaching week number or name substring",
    )
    dn.add_argument(
        "--match", default=None,
        help="Substring filter on file/module name to narrow selection",
    )
    dn.add_argument(
        "--index", type=int, default=None,
        help="Zero-based index to pick from matching materials",
    )
    dn.add_argument(
        "--output", default=None, metavar="DIR",
        help="Directory to save the file (default: current directory)",
    )

    ex = sub.add_parser(
        "extract", parents=[_shared],
        help="Extract per-page text from a material (nothing saved to disk)",
        description=(
            "Fetch a material into memory and extract structured per-page "
            "text — no file is written to disk (use 'worsaga download' to "
            "save the file itself). Light cleaning preserves educational "
            "content such as captions, learning objectives, and references "
            "by default."
        ),
        epilog=(
            "Example workflow:\n"
            "  worsaga materials ECON101 --week 3            # discover files\n"
            "  worsaga extract ECON101 --week 3 --match slides\n"
            "  worsaga --json extract ECON101 --week 3 --index 0\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ex.add_argument(
        "course",
        help="Moodle course ID (integer) or course short-code (e.g. ECON101)",
    )
    ex.add_argument(
        "--week", required=True,
        help="Teaching week number or name substring",
    )
    ex.add_argument(
        "--match", default=None,
        help="Substring filter on file/module name to narrow selection",
    )
    ex.add_argument(
        "--index", type=int, default=None,
        help="Zero-based index to pick from matching materials",
    )
    ex.add_argument(
        "--raw", action="store_true",
        help="Skip boilerplate cleaning; return extractor output unchanged",
    )
    ex.add_argument(
        "--max-chars", type=_non_negative_int, default=None, metavar="N",
        help=(
            "Cap total extracted text at N characters "
            f"(default: {MAX_TEXT_PER_FILE}; 0 = no cap)"
        ),
    )

    gr = sub.add_parser("grades", parents=[_shared], help="Show grade items")
    gr.add_argument(
        "course",
        nargs="?",
        default=None,
        help="Optional Moodle course ID or course short-code",
    )
    gr.add_argument(
        "--missing",
        action="store_true",
        help="Only show missing/unreleased grade items",
    )
    gr.add_argument(
        "--all",
        action="store_true",
        help="Include unknown/blank gradebook placeholders",
    )
    gr.add_argument(
        "--summary",
        action="store_true",
        help="Show aggregate grade status counts",
    )

    ag = sub.add_parser("assignments", parents=[_shared], help="Show assignment statuses")
    ag.add_argument(
        "course",
        nargs="?",
        default=None,
        help="Optional Moodle course ID or course short-code",
    )
    ag.add_argument(
        "--due-soon",
        action="store_true",
        help="Only show assignments due within the look-ahead window",
    )
    ag.add_argument(
        "--days",
        type=int,
        default=14,
        help="Look-ahead window for --due-soon (default: 14)",
    )
    ag.add_argument(
        "--status",
        default=None,
        help="Filter by derived status, such as missing or submitted",
    )
    ag.add_argument(
        "--include-feedback",
        action="store_true",
        help=(
            "Deprecated no-op: grade/feedback fields always derive from "
            "your own submission status"
        ),
    )

    fm = sub.add_parser("forums", parents=[_shared], help="Show course forums")
    fm.add_argument(
        "course",
        help="Moodle course ID (integer) or course short-code",
    )

    fl = sub.add_parser("forum", parents=[_shared], help="Forum actions")
    fl.add_argument("action", choices=["latest"], help="Forum action")
    fl.add_argument(
        "course",
        help="Moodle course ID (integer) or course short-code",
    )

    up = sub.add_parser("updates", parents=[_shared], help="Show recent forum updates")
    up.add_argument(
        "course",
        nargs="?",
        default=None,
        help="Optional Moodle course ID or course short-code",
    )
    up.add_argument(
        "--since",
        default="7d",
        help="Lookback window like 7d, 24h, or YYYY-MM-DD",
    )

    nf = sub.add_parser("notifications", parents=[_shared], help="Show notifications")
    nf.add_argument(
        "--unread-only",
        action="store_true",
        help="Only request unread notifications",
    )

    ib = sub.add_parser("inbox", parents=[_shared], help="Show inbox messages")
    ib.add_argument(
        "--since",
        default=None,
        help="Optional lookback window like 7d, 24h, or YYYY-MM-DD",
    )

    dg = sub.add_parser("digest", parents=[_shared], help="Show a live study digest")
    dg.add_argument(
        "--since",
        default="24h",
        help="Lookback window like 7d, 24h, or YYYY-MM-DD",
    )

    cal = sub.add_parser("calendar", parents=[_shared], help="Show calendar events")
    cal.add_argument(
        "course",
        nargs="?",
        default=None,
        help="Optional Moodle course ID or course short-code",
    )
    cal.add_argument(
        "--days",
        type=int,
        default=30,
        help="Look-ahead window in days (default: 30)",
    )
    cal.add_argument(
        "--week",
        default=None,
        help="Filter to a specific teaching week (number or name substring)",
    )

    sm = sub.add_parser(
        "summary", parents=[_shared], help="Generate weekly study summary",
    )
    sm.add_argument(
        "course",
        help="Moodle course ID (integer) or course short-code (e.g. ECON101)",
    )
    sm.add_argument(
        "--week",
        required=True,
        help="Teaching week number or name query (e.g. 3, revision, reading)",
    )

    sp = sub.add_parser("setup", parents=[_shared], help="Guided first-time configuration")
    sp.add_argument("--url", dest="setup_url", default=None, metavar="URL", help="Moodle site URL")
    sp.add_argument("--token", dest="setup_token", default=None, metavar="TOKEN", help="Moodle API token")
    sp.add_argument("--userid", dest="setup_userid", default=None, type=int, metavar="ID", help="Moodle user ID")

    sr = sub.add_parser(
        "search", parents=[_shared], help="Search course content by keyword",
    )
    sr.add_argument(
        "course",
        help="Moodle course ID (integer) or course short-code (e.g. ECON101)",
    )
    sr.add_argument("query", help="Search keyword (case-insensitive)")

    ix = sub.add_parser(
        "index", parents=[_shared],
        help="Build or update the local full-text search index",
        description=(
            "Fetch supported course materials (PDF, PPTX, DOCX, TXT) in "
            "memory, extract their text, and store it page by page in a "
            "local SQLite full-text index. Files unchanged since the "
            "last run are skipped, so re-running is cheap, and "
            "full-course runs drop entries for files deleted on Moodle. "
            "Tokens and authenticated URLs are never stored."
        ),
    )
    ix.add_argument(
        "course", nargs="?", default=None,
        help="Moodle course ID or short-code (default: all courses)",
    )
    ix.add_argument(
        "--week", default=None,
        help="Only index sections matching this week number or name",
    )
    ix.add_argument(
        "--max-files", type=_non_negative_int, default=INDEX_MAX_FILES_PER_RUN,
        help=(
            "Cap on files fetched this run "
            f"(default: {INDEX_MAX_FILES_PER_RUN})"
        ),
    )

    st = sub.add_parser(
        "search-text", parents=[_shared],
        help="Full-text search over indexed material text (no network)",
        description=(
            "Search the local full-text index built by 'worsaga index'. "
            "Runs entirely offline against previously indexed material "
            "text and reports the course, file, and page of each hit."
        ),
    )
    st.add_argument("query", help="Search terms (matched with AND)")
    st.add_argument(
        "--course", default=None,
        help="Limit hits to one course (ID or short-code)",
    )
    st.add_argument(
        "--limit", type=_non_negative_int, default=20,
        help="Maximum hits to show (default: 20)",
    )

    sk = sub.add_parser(
        "study-pack", parents=[_shared],
        help="Export a Markdown study pack for a course week",
        description=(
            "Build a single Markdown document for one teaching week: "
            "study notes, a materials overview, and the extracted "
            "per-page content of every supported file in the section."
        ),
    )
    sk.add_argument(
        "course",
        help="Moodle course ID (integer) or course short-code (e.g. ECON101)",
    )
    sk.add_argument(
        "--week",
        required=True,
        help="Teaching week number or name query (e.g. 3, revision, reading)",
    )
    sk.add_argument(
        "--output", default=None, metavar="DIR",
        help="Directory to write the pack into (default: current directory)",
    )
    sk.add_argument(
        "--stdout", action="store_true",
        help="Print the Markdown to stdout instead of writing a file",
    )

    sy = sub.add_parser(
        "sync", parents=[_shared],
        help="Sync metadata to the local cache and report changes",
        description=(
            "Fetch metadata-only snapshots (deadlines, file metadata, "
            "grades, forum discussions — never file contents) into the "
            "local SQLite cache and report what changed since the last "
            "sync. The first sync establishes a baseline and reports no "
            "changes. Tokens and authenticated URLs are never stored."
        ),
    )
    sy.add_argument(
        "--days",
        type=int,
        default=SYNC_LOOKAHEAD_DAYS,
        help=f"Deadline look-ahead window in days (default: {SYNC_LOOKAHEAD_DAYS})",
    )

    ch = sub.add_parser(
        "changes", parents=[_shared],
        help="Show changes detected by previous syncs (no network)",
    )
    ch.add_argument(
        "--since",
        default="7d",
        help="Lookback window like 7d, 24h, or YYYY-MM-DD",
    )
    ch.add_argument(
        "--category",
        default=None,
        choices=list(SYNC_CATEGORIES),
        help="Only show changes from one category",
    )

    wt = sub.add_parser(
        "watch", parents=[_shared],
        help="Run a foreground sync loop with change notifications",
        description=(
            "Repeatedly run the metadata sync on a fixed interval, print "
            "detected changes, and raise a local desktop notification "
            "when something changed. Runs in the foreground; stop with "
            "Ctrl+C. For unattended background syncs use "
            "'worsaga auto-sync install' instead."
        ),
    )
    wt.add_argument(
        "--interval", default=None, metavar="SPAN",
        help="Time between syncs like 15m, 900, or 1h (default: 15m)",
    )
    wt.add_argument(
        "--cycles", type=_non_negative_int, default=None,
        help="Stop after this many sync cycles (default: run until Ctrl+C)",
    )
    wt.add_argument(
        "--no-notify", action="store_true",
        help="Do not send desktop notifications",
    )
    wt.add_argument(
        "--days", type=int, default=SYNC_LOOKAHEAD_DAYS,
        help=f"Deadline look-ahead window in days (default: {SYNC_LOOKAHEAD_DAYS})",
    )

    au = sub.add_parser(
        "auto-sync", parents=[_shared],
        help="Manage the scheduled background sync (install/status/remove)",
        description=(
            "Register a periodic 'worsaga sync --quiet' with the "
            "platform scheduler (Task Scheduler on Windows, launchd on "
            "macOS, a systemd user timer on Linux), inspect it, or "
            "remove it. 'install --dry-run' shows exactly what would "
            "be executed or written without changing anything."
        ),
    )
    au.add_argument(
        "action", choices=["install", "status", "remove"],
        help="What to do with the scheduled sync",
    )
    au.add_argument(
        "--interval", default=None, metavar="SPAN",
        help=(
            "Time between background syncs like 30m or 2h "
            f"(install only; default: {DEFAULT_INTERVAL_MINUTES}m)"
        ),
    )
    au.add_argument(
        "--dry-run", action="store_true",
        help="Show what install/remove would do without doing it",
    )
    au.add_argument(
        "--force-local", action="store_true",
        help=(
            "remove only: delete Worsaga's local files (record, plist, "
            "unit files) without querying or changing the scheduler"
        ),
    )

    sub.add_parser("doctor", parents=[_shared], help="Check auth and connectivity")
    sub.add_parser("update", parents=[_shared], help="Show how to upgrade safely")

    cfg = sub.add_parser("config", parents=[_shared], help="Show configuration info")
    cfg.add_argument(
        "action", nargs="?", default="path", choices=["path"],
        help="Config action (default: path)",
    )

    return parser


# ── Helpers ───────────────────────────────────────────────────────


def _demo_mode(args: argparse.Namespace) -> bool:
    """Return True when demo mode is requested via flag or environment."""
    return getattr(args, "demo", False) or demo_mode_enabled()


def _client(args: argparse.Namespace) -> MoodleClient:
    """Build a MoodleClient, respecting top-level credential overrides.

    In demo mode, returns the offline fake-data client instead — no
    credentials, config, or network access required.
    """
    if _demo_mode(args):
        return DemoMoodleClient()
    return MoodleClient(
        MoodleConfig.load(
            url=args.url,
            token=args.token,
            userid=args.userid,
            creds_path=getattr(args, "creds_path", None),
        )
    )


def _emit_data(args: argparse.Namespace, payload) -> bool:
    """Print JSON/YAML payload when requested, returning whether it did."""
    if not wants_structured(args):
        return False
    print(
        render_structured(
            payload,
            json_mode=getattr(args, "json", False),
            yaml_mode=getattr(args, "yaml", False),
        )
    )
    return True


def _since_to_days(value: str | None, *, default_days: int) -> int:
    """Convert a --since expression into at least one whole day."""
    if value is None:
        return default_days
    import math
    import time as _time

    now = int(_time.time())
    since_ts = parse_since(value, now=now)
    if since_ts is None:
        return default_days
    return max(1, int(math.ceil((now - since_ts) / 86400)))


def _resolve_course_id(client: MoodleClient, raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        pass

    courses = client.get_courses()
    needle = raw.strip().lower()

    for c in courses:
        if c.get("shortname", "").lower() == needle:
            return c["id"]

    prefix_matches = []
    for c in courses:
        sn = c.get("shortname", "").lower()
        base = sn
        for sep in ("_", "-"):
            if sep in sn:
                base = sn.split(sep, 1)[0]
                break

        if needle == base:
            prefix_matches.append(c)
        elif needle in base.split("/"):
            prefix_matches.append(c)
        elif base.startswith(needle) and len(base) - len(needle) <= 2:
            prefix_matches.append(c)

    seen = set()
    unique_matches = []
    for c in prefix_matches:
        if c["id"] not in seen:
            seen.add(c["id"])
            unique_matches.append(c)
    prefix_matches = unique_matches

    if len(prefix_matches) == 1:
        return prefix_matches[0]["id"]

    if len(prefix_matches) > 1:
        ambiguous = ", ".join(sorted(c.get("shortname", "?") for c in prefix_matches))
        raise CourseResolutionError(
            f"'{raw}' is ambiguous — matches: {ambiguous}"
        )

    available = ", ".join(sorted(c.get("shortname", "?") for c in courses))
    raise CourseResolutionError(
        f"no enrolled course matching '{raw}'.\n"
        f"Available short-codes: {available}"
    )


def _invocation_hint() -> str:
    """Return the command prefix most likely to work for the user."""
    if sys.argv and "worsaga" in sys.argv[0]:
        if "worsaga.cli" in sys.argv[0]:
            python = "py" if os.name == "nt" else "python"
            return f"{python} -m worsaga.cli"
    return "worsaga"


def _upgrade_command(os_name: str | None = None) -> str:
    """Return the public PyPI upgrade command."""
    return f'pipx upgrade worsaga || pipx install --force "{PUBLIC_INSTALL_SPEC}"'


def _display_timestamp(value) -> str:
    """Return a compact timestamp for human CLI tables."""
    try:
        return timestamp_to_display(value)
    except (TypeError, ValueError, OverflowError):
        return str(value or "")


def _print_setup_success(dest) -> None:
    """Print post-setup success message with platform-aware guidance."""
    cmd = _invocation_hint()
    print(f"\nConfig saved to {dest}")
    if os.name != "nt":
        print("Permissions set to owner-only (600).")
    print()
    print("Setup complete! Try these next:")
    print(f"  {cmd} courses                    # list your enrolled courses")
    print(f"  {cmd} deadlines                  # check upcoming deadlines")
    print(f"  {cmd} contents <course> --week 1 # explore week content")
    print(f"  {cmd} materials <course> --week 1          # list available files")
    print(f"  {cmd} download <course> --week 1 --index 0 # download a file")
    print(f"  {cmd} summary <course> --week 1  # Study notes for a week")


# ── Normalizers ──────────────────────────────────────────────────


def _normalize_courses(courses: list[dict]) -> list[dict]:
    """Return a stable, minimal representation of enrolled courses."""
    return [
        {
            "id": c["id"],
            "shortname": c.get("shortname", ""),
            "fullname": c.get("fullname", ""),
        }
        for c in courses
    ]


def _normalize_contents(sections: list[dict]) -> list[dict]:
    """Return a stable, minimal representation of course sections."""
    result = []
    for s in sections:
        modules = []
        for m in s.get("modules", []):
            modules.append({
                "id": m.get("id"),
                "name": m.get("name", ""),
                "type": m.get("modname", ""),
                "url": m.get("url", ""),
            })
        result.append({
            "section": s.get("section"),
            "name": s.get("name", ""),
            "modules": modules,
        })
    return result


# ── Commands ──────────────────────────────────────────────────────


def cmd_courses(args: argparse.Namespace) -> None:
    client = _client(args)
    courses = client.get_courses()
    payload = courses if getattr(args, "raw", False) else _normalize_courses(courses)
    if _emit_data(args, payload):
        return
    if not courses:
        print("No enrolled courses found.")
        return
    print(f"{'ID':>8}  {'Short code':<20}  {'Full name'}")
    print(f"{'-' * 8}  {'-' * 20}  {'-' * 40}")
    for c in courses:
        print(f"{c['id']:>8}  {c.get('shortname', ''):.<20}  {c.get('fullname', '')}")


def cmd_deadlines(args: argparse.Namespace) -> None:
    client = _client(args)
    deadlines = get_upcoming_deadlines(client, lookahead_days=args.days)
    if _emit_data(args, deadlines):
        return
    if not deadlines:
        print(f"No deadlines in the next {args.days} days.")
        return
    print(f"{'Due':<22}  {'Days':>4}  {'Type':<12}  {'Course':<15}  {'Name'}")
    print(f"{'-' * 22}  {'-' * 4}  {'-' * 12}  {'-' * 15}  {'-' * 30}")
    for d in deadlines:
        print(
            f"{d['due_str']:<22}  {d['days_left']:>4}  {d['type']:<12}  "
            f"{d['course']:<15}  {d['name']}"
        )


def cmd_grades(args: argparse.Namespace) -> None:
    client = _client(args)
    course_id = _resolve_course_id(client, args.course) if args.course else None

    if getattr(args, "summary", False):
        summary = build_grade_summary(client, course_id=course_id)
        if _emit_data(args, summary):
            return
        print(f"Grade items: {summary['total_items']}")
        for status, count in summary["status_counts"].items():
            print(f"  {status}: {count}")
        return

    grade_result = collect_grades_data(client, course_id=course_id)
    grades = grade_result["grades"]
    if getattr(args, "missing", False):
        grades = [
            grade for grade in grades
            if grade.get("status") in {"missing", "unreleased"}
        ]
    elif not getattr(args, "all", False):
        grades = [
            grade for grade in grades
            if grade.get("status") != "unknown"
        ]

    if _emit_data(args, grades):
        return
    if not grades:
        print("No grade items found.")
        return

    print(
        f"{'Course':<15}  {'Item':<30}  {'Grade':<14}  "
        f"{'Percent':>8}  {'Weight':>8}  {'Status':<10}  {'Feedback'}"
    )
    print(
        f"{'-' * 15}  {'-' * 30}  {'-' * 14}  "
        f"{'-' * 8}  {'-' * 8}  {'-' * 10}  {'-' * 20}"
    )
    for grade in grades:
        percent = grade.get("percentage")
        weight = grade.get("weight")
        percent_text = "" if percent is None else f"{percent:g}"
        weight_text = "" if weight is None else f"{weight:g}"
        feedback = "yes" if grade.get("feedback") else ""
        print(
            f"{grade['course_shortname'][:15]:<15}  "
            f"{grade['item_name'][:30]:<30}  "
            f"{grade['grade_display'][:14]:<14}  "
            f"{percent_text:>8}  {weight_text:>8}  "
            f"{grade['status']:<10}  {feedback}"
        )
    if grade_result.get("warnings"):
        print(
            f"Skipped {len(grade_result['warnings'])} course gradebook(s) "
            "that Moodle would not allow this account to view.",
            file=sys.stderr,
        )


def cmd_assignments(args: argparse.Namespace) -> None:
    client = _client(args)
    course_id = _resolve_course_id(client, args.course) if args.course else None
    assignments = get_assignments_data(
        client,
        course_id=course_id,
        include_feedback=getattr(args, "include_feedback", False),
    )

    if getattr(args, "due_soon", False):
        assignments = [
            assignment for assignment in assignments
            if assignment.get("days_left") is not None
            and 0 <= assignment["days_left"] <= args.days
        ]

    if getattr(args, "status", None):
        wanted = args.status.strip().lower()
        assignments = [
            assignment for assignment in assignments
            if str(assignment.get("status", "")).lower() == wanted
            or str(assignment.get("submission_status", "")).lower() == wanted
        ]

    if _emit_data(args, assignments):
        return
    if not assignments:
        print("No assignments found.")
        return

    print(
        f"{'Due':<22}  {'Days':>4}  {'Course':<15}  "
        f"{'Status':<13}  {'Submitted':<9}  {'Name'}"
    )
    print(
        f"{'-' * 22}  {'-' * 4}  {'-' * 15}  "
        f"{'-' * 13}  {'-' * 9}  {'-' * 30}"
    )
    for assignment in assignments:
        days = assignment.get("days_left")
        days_text = "" if days is None else str(days)
        submitted = assignment.get("submitted")
        submitted_text = "" if submitted is None else str(submitted).lower()
        print(
            f"{assignment.get('due_str', ''):<22}  "
            f"{days_text:>4}  "
            f"{assignment['course_shortname'][:15]:<15}  "
            f"{assignment['status']:<13}  "
            f"{submitted_text:<9}  "
            f"{assignment['name']}"
        )


def cmd_forums(args: argparse.Namespace) -> None:
    client = _client(args)
    course_id = _resolve_course_id(client, args.course)
    forums = get_course_forums_data(client, course_id)
    if _emit_data(args, forums):
        return
    if not forums:
        print("No forums found.")
        return
    print(f"{'Forum':<35}  {'Type':<12}  {'Discussions':>11}  {'Announcements'}")
    print(f"{'-' * 35}  {'-' * 12}  {'-' * 11}  {'-' * 13}")
    for forum in forums:
        count = forum.get("discussion_count")
        count_text = "" if count is None else str(count)
        ann = "yes" if forum.get("is_announcement") else ""
        print(
            f"{forum['name'][:35]:<35}  {forum['type'][:12]:<12}  "
            f"{count_text:>11}  {ann}"
        )


def cmd_forum(args: argparse.Namespace) -> None:
    client = _client(args)
    course_id = _resolve_course_id(client, args.course)
    if args.action != "latest":
        raise ValueError(f"unknown forum action: {args.action}")
    discussions = get_forum_discussions_data(client, course_id)
    if _emit_data(args, discussions):
        return
    if not discussions:
        print("No forum discussions found.")
        return
    print(f"{'Modified':<22}  {'Forum':<25}  {'Unread':>6}  {'Discussion'}")
    print(f"{'-' * 22}  {'-' * 25}  {'-' * 6}  {'-' * 30}")
    for discussion in discussions:
        unread = discussion.get("unread_count")
        unread_text = "" if unread is None else str(unread)
        modified = _display_timestamp(
            discussion.get("modified_at") or discussion.get("created_at")
        )
        print(
            f"{modified:<22}  "
            f"{discussion['forum_name'][:25]:<25}  "
            f"{unread_text:>6}  {discussion['name']}"
        )


def cmd_updates(args: argparse.Namespace) -> None:
    client = _client(args)
    course_id = _resolve_course_id(client, args.course) if args.course else None
    since_days = _since_to_days(args.since, default_days=7)
    updates = get_latest_updates_data(client, course_id=course_id, since_days=since_days)
    if _emit_data(args, updates):
        return
    if not updates:
        print(f"No updates in the last {since_days} day(s).")
        return
    print(f"{'Course':<15}  {'Forum':<25}  {'Updated':<22}  {'Discussion'}")
    print(f"{'-' * 15}  {'-' * 25}  {'-' * 22}  {'-' * 30}")
    for update in updates:
        updated = _display_timestamp(
            update.get("modified_at") or update.get("created_at")
        )
        print(
            f"{str(update.get('course_shortname', ''))[:15]:<15}  "
            f"{update['forum_name'][:25]:<25}  "
            f"{updated:<22}  "
            f"{update['name']}"
        )


def cmd_notifications(args: argparse.Namespace) -> None:
    client = _client(args)
    notifications = get_notifications_data(
        client,
        unread_only=getattr(args, "unread_only", False),
    )
    if _emit_data(args, notifications):
        return
    if not notifications:
        print("No notifications found.")
        return
    print(f"{'Created':<22}  {'Read':<5}  {'Sender':<20}  {'Subject'}")
    print(f"{'-' * 22}  {'-' * 5}  {'-' * 20}  {'-' * 30}")
    for notification in notifications:
        read = notification.get("read")
        read_text = "" if read is None else str(read).lower()
        created = _display_timestamp(notification.get("created_at"))
        print(
            f"{created:<22}  "
            f"{read_text:<5}  {notification['sender'][:20]:<20}  "
            f"{notification['subject']}"
        )


def cmd_inbox(args: argparse.Namespace) -> None:
    client = _client(args)
    since_days = _since_to_days(args.since, default_days=0) if args.since else None
    messages = get_messages_data(client, since_days=since_days)
    if _emit_data(args, messages):
        return
    if not messages:
        print("No messages found.")
        return
    print(f"{'Created':<22}  {'Read':<5}  {'Sender':<20}  {'Subject'}")
    print(f"{'-' * 22}  {'-' * 5}  {'-' * 20}  {'-' * 30}")
    for message in messages:
        read = message.get("read")
        read_text = "" if read is None else str(read).lower()
        created = _display_timestamp(message.get("created_at"))
        print(
            f"{created:<22}  "
            f"{read_text:<5}  {message['sender'][:20]:<20}  "
            f"{message['subject']}"
        )


def cmd_digest(args: argparse.Namespace) -> None:
    client = _client(args)
    since_days = _since_to_days(args.since, default_days=1)
    digest = get_digest_data(client, since_days=since_days)
    if _emit_data(args, digest):
        return
    print(f"Digest: last {since_days} day(s)")
    for key in ("deadlines", "assignments", "updates", "notifications", "messages"):
        print(f"{key}: {len(digest.get(key, []))}")
    if digest.get("warnings"):
        print("Warnings:")
        for warning in digest["warnings"]:
            print(f"  {warning}")


def cmd_calendar(args: argparse.Namespace) -> None:
    client = _client(args)
    course_id = _resolve_course_id(client, args.course) if args.course else None
    events = get_calendar_events_data(
        client,
        course_id=course_id,
        days=args.days,
        week=args.week,
    )
    if _emit_data(args, events):
        return
    if not events:
        if args.week is not None:
            print(f"No calendar events for week '{args.week}' in the next {args.days} days.")
        else:
            print(f"No calendar events in the next {args.days} days.")
        return
    print(f"{'Start':<22}  {'Type':<12}  {'Course':<8}  {'Name'}")
    print(f"{'-' * 22}  {'-' * 12}  {'-' * 8}  {'-' * 30}")
    for event in events:
        course = "" if event.get("course_id") is None else str(event["course_id"])
        print(
            f"{event['start_str']:<22}  {event['event_type'][:12]:<12}  "
            f"{course:<8}  {event['name']}"
        )


def cmd_contents(args: argparse.Namespace) -> None:
    client = _client(args)
    course_id = _resolve_course_id(client, args.course)
    contents = client.get_course_contents(course_id)

    # Filter by week if requested
    if args.week is not None:
        contents = [s for s in contents if match_section(s, args.week)]

    payload = contents if getattr(args, "raw", False) else _normalize_contents(contents)
    if _emit_data(args, payload):
        return
    if not contents:
        label = f" for week '{args.week}'" if args.week is not None else ""
        print(f"No sections found{label}.")
        return
    for section in contents:
        name = section.get("name", "Untitled section")
        print(f"\n## {name}")
        modules = section.get("modules", [])
        if not modules:
            print("   (empty)")
            continue
        for mod in modules:
            mod_name = mod.get("name", "?")
            mod_type = mod.get("modname", "?")
            print(f"   [{mod_type}] {mod_name}")


def cmd_materials(args: argparse.Namespace) -> None:
    client = _client(args)
    course_id = _resolve_course_id(client, args.course)
    sections = client.get_course_contents(course_id)

    if args.week is not None:
        materials = get_section_materials(
            sections, course_id, args.week, base_url=client.base_url,
        )
    else:
        materials = extract_materials(sections, course_id, base_url=client.base_url)

    payload = (
        materials if getattr(args, "include_file_urls", False)
        else strip_file_urls(materials)
    )
    if _emit_data(args, payload):
        return

    if not materials:
        label = f" for week {args.week}" if args.week else ""
        print(f"No materials found{label}.")
        return

    print(
        f"{'Section':<30}  {'Module':<25}  {'Type':<10}  "
        f"{'File':<30}  {'Size':>10}"
    )
    print(
        f"{'-' * 30}  {'-' * 25}  {'-' * 10}  "
        f"{'-' * 30}  {'-' * 10}"
    )
    for m in materials:
        size = m["file_size"]
        size_str = (
            f"{size / 1_048_576:.1f} MB" if size >= 1_048_576
            else f"{size / 1024:.0f} KB" if size > 0
            else ""
        )
        file_display = m["file_name"] or "(link)"
        print(
            f"{m['section_name'][:30]:<30}  {m['module_name'][:25]:<25}  "
            f"{m['module_type']:<10}  {file_display[:30]:<30}  "
            f"{size_str:>10}"
        )


def _select_week_material(
    args: argparse.Namespace,
    client: MoodleClient,
    course_id: int,
) -> dict:
    """Discover materials for ``args.week`` and select exactly one.

    Shared by the ``download`` and ``extract`` commands. On no materials
    or an ambiguous/failed selection, emits the structured (or human)
    candidate error exactly like ``download`` always has and exits 1.
    """
    from worsaga.materials import candidate_summary

    sections = client.get_course_contents(course_id)
    materials = get_section_materials(
        sections, course_id, args.week, base_url=client.base_url,
    )

    if not materials:
        msg = f"No materials found for week {args.week}."
        if wants_structured(args):
            _emit_data(args, {"error": msg, "candidates": []})
        else:
            print(msg, file=sys.stderr)
        sys.exit(1)

    try:
        return select_material(
            materials, match=getattr(args, "match", None),
            index=getattr(args, "index", None),
        )
    except MaterialSelectionError as exc:
        candidates = [
            candidate_summary(c, i)
            for i, c in enumerate(exc.candidates)
        ]
        if wants_structured(args):
            _emit_data(args, {
                "error": str(exc),
                "candidates": candidates,
            })
        else:
            print(f"Error: {exc}", file=sys.stderr)
            if candidates:
                print("\nAvailable materials:", file=sys.stderr)
                for c in candidates:
                    print(
                        f"  [{c['index']}] {c['file_name'] or c['module_name']}"
                        f"  ({c['section_name']})",
                        file=sys.stderr,
                    )
        sys.exit(1)


def cmd_download(args: argparse.Namespace) -> None:
    client = _client(args)
    course_id = _resolve_course_id(client, args.course)
    chosen = _select_week_material(args, client, course_id)

    if not args.quiet:
        print(
            f"Downloading {chosen.get('file_name') or chosen.get('module_name')}...",
            file=sys.stderr,
        )

    try:
        result = download_material(client, chosen, output_dir=args.output)
    except DownloadError as exc:
        if wants_structured(args):
            _emit_data(args, {"error": str(exc), "error_code": exc.code})
        else:
            print(f"Error ({exc.code}): {exc}", file=sys.stderr)
        sys.exit(1)

    if _emit_data(args, result):
        return
    else:
        size = result["bytes_written"]
        size_str = (
            f"{size / 1_048_576:.1f} MB" if size >= 1_048_576
            else f"{size / 1024:.0f} KB" if size > 0
            else "0 B"
        )
        print(f"Saved: {result['local_path']} ({size_str})")


def cmd_extract(args: argparse.Namespace) -> None:
    client = _client(args)
    course_id = _resolve_course_id(client, args.course)
    chosen = _select_week_material(args, client, course_id)

    if not args.quiet:
        print(
            f"Extracting {chosen.get('file_name') or chosen.get('module_name')}...",
            file=sys.stderr,
        )

    max_chars = args.max_chars if args.max_chars is not None else MAX_TEXT_PER_FILE
    try:
        result = extract_material_content(
            client, chosen, max_chars=max_chars, clean=not args.raw,
        )
    except DownloadError as exc:
        if wants_structured(args):
            _emit_data(args, {"error": str(exc), "error_code": exc.code})
        else:
            print(f"Error ({exc.code}): {exc}", file=sys.stderr)
        sys.exit(1)

    if _emit_data(args, result):
        return

    unit = "Slide" if result["file_type"] == "pptx" else "Page"
    # ASCII separators only: Windows consoles often use cp1252, where
    # box-drawing characters raise UnicodeEncodeError.
    print(f"# {result['filename']} - {result['section_name']}")
    for page in result["pages"]:
        print(f"\n--- {unit} {page['page']} ---")
        if page["markdown"]:
            print(page["markdown"])
        for page_warning in page["warnings"]:
            print(f"Warning ({unit.lower()} {page['page']}): {page_warning}",
                  file=sys.stderr)
    if not result["pages"]:
        print("(no extractable text)")
    for warning in result["warnings"]:
        print(f"Warning: {warning}", file=sys.stderr)


def cmd_summary(args: argparse.Namespace) -> None:
    client = _client(args)
    course_id = _resolve_course_id(client, args.course)
    sections = client.get_course_contents(course_id)
    week_query = args.week

    if not wants_structured(args):
        section, _, section_name = find_best_section(sections, week_query)
        print(f"Week {week_query} — {section_name or '(no section found)'}")
        if section and section.get("modules"):
            overview = summarize_modules(section["modules"])
            if overview:
                print(f"Materials: {overview}")
            print()

    def _on_extract(filename: str) -> None:
        if not wants_structured(args) and not args.quiet:
            print(f"  Extracting {filename}...", file=sys.stderr)

    result = build_weekly_summary(
        client, course_id, week_query,
        sections=sections,
        on_extract=_on_extract,
    )

    if _emit_data(args, result):
        return

    print(f"Study notes ({result['method']}):")
    print(format_bullets(result["bullets"]))
    for warning in result.get("warnings", []):
        print(f"Warning: {warning}", file=sys.stderr)


def cmd_search(args: argparse.Namespace) -> None:
    client = _client(args)
    course_id = _resolve_course_id(client, args.course)
    sections = client.get_course_contents(course_id)
    results = search_course_content(sections, args.query)

    if _emit_data(args, results):
        return

    if not results:
        print(f"No matches for '{args.query}'.")
        return

    print(f"{'Section':<30}  {'Module':<30}  {'Type':<12}")
    print(f"{'-' * 30}  {'-' * 30}  {'-' * 12}")
    for r in results:
        print(
            f"{r['section_name'][:30]:<30}  "
            f"{r['module_name'][:30]:<30}  "
            f"{r['module_type']:<12}"
        )


def cmd_index(args: argparse.Namespace) -> None:
    client = _client(args)
    course_id = None
    if args.course is not None:
        course_id = _resolve_course_id(client, args.course)

    def _on_file(filename: str) -> None:
        if not wants_structured(args) and not args.quiet:
            print(f"  Indexing {filename}...", file=sys.stderr)

    result = build_text_index(
        client,
        course_id=course_id,
        week=args.week,
        max_files=args.max_files,
        on_file=_on_file,
    )

    if _emit_data(args, result):
        return

    print(f"Indexed {result['site']}")
    removed = result.get("files_removed", 0)
    removed_note = f", {removed} removed" if removed else ""
    print(
        f"  files: {result['files_indexed']} indexed, "
        f"{result['files_unchanged']} unchanged, "
        f"{result['files_failed']} failed, "
        f"{result['files_skipped_unsupported']} skipped (no extractor)"
        f"{removed_note}"
    )
    stats = result["index"]
    print(
        f"  index: {stats['documents']} files / {stats['pages']} pages "
        f"across {stats['courses']} course(s)"
    )
    print(f"  path:  {result['index_path']}")
    if result["budget_exhausted"]:
        print("\nFile budget reached - run 'worsaga index' again to continue.")
    for warning in result["warnings"]:
        print(f"Warning: {warning}", file=sys.stderr)


def cmd_search_text(args: argparse.Namespace) -> None:
    client = _client(args)
    course_id = None
    course_shortname = None
    if args.course is not None:
        # Resolve locally against the index (no network): numeric input
        # is a course ID, anything else matches the stored short-code.
        try:
            course_id = int(args.course)
        except ValueError:
            course_shortname = args.course.strip()

    result = search_text_index(
        client.base_url,
        args.query,
        course_id=course_id,
        course_shortname=course_shortname,
        limit=args.limit,
    )

    if _emit_data(args, result):
        return

    hits = result["hits"]
    if not hits:
        if result["index"]["documents"] == 0:
            print(
                "Nothing indexed for this site yet. "
                "Run 'worsaga index' first."
            )
        else:
            print(f"No matches for '{args.query}'.")
        return

    print(f"{len(hits)} match(es) for '{args.query}':\n")
    for hit in hits:
        page_ref = f"p.{hit['page']}" if hit.get("page") else ""
        location = "  ".join(
            part for part in (
                hit.get("course_shortname", ""),
                hit.get("file_name", "") or hit.get("module_name", ""),
                page_ref,
            ) if part
        )
        print(location)
        snippet = " ".join(str(hit.get("snippet", "")).split())
        print(f"  {snippet}\n")


def cmd_study_pack(args: argparse.Namespace) -> None:
    client = _client(args)
    course_id = _resolve_course_id(client, args.course)

    def _on_file(filename: str) -> None:
        if not wants_structured(args) and not args.quiet:
            print(f"  Extracting {filename}...", file=sys.stderr)

    result = build_study_pack(
        client, course_id, args.week, on_file=_on_file,
    )

    if args.stdout:
        if _emit_data(args, result):
            return
        print(result["markdown"])
        for warning in result.get("warnings", []):
            print(f"Warning: {warning}", file=sys.stderr)
        return

    path = write_study_pack(
        result["markdown"],
        args.output or os.getcwd(),
        result["suggested_filename"],
    )
    # The markdown itself lives in the written file; keep the emitted
    # payload compact.
    payload = {key: value for key, value in result.items() if key != "markdown"}
    payload["path"] = str(path)

    if _emit_data(args, payload):
        return

    print(f"Study pack written to {path}")
    print(
        f"  {result['section_name'] or '(no section found)'} - "
        f"{len(result['files'])} file(s), "
        f"{sum(f['page_count'] for f in result['files'])} page(s), "
        f"notes: {result['summary_method']}"
    )
    for warning in result.get("warnings", []):
        print(f"Warning: {warning}", file=sys.stderr)


def _print_change_table(changes: list[dict]) -> None:
    print(f"{'Detected':<22}  {'Kind':<26}  {'Course':<12}  {'Title'}")
    print(f"{'-' * 22}  {'-' * 26}  {'-' * 12}  {'-' * 30}")
    for change in changes:
        detected = _display_timestamp(change.get("detected_at"))
        print(
            f"{detected:<22}  "
            f"{str(change.get('kind', ''))[:26]:<26}  "
            f"{str(change.get('course_shortname', ''))[:12]:<12}  "
            f"{change.get('title', '')}"
        )


def cmd_sync(args: argparse.Namespace) -> None:
    client = _client(args)
    if not args.quiet and not wants_structured(args):
        print("Syncing metadata (nothing is downloaded)...", file=sys.stderr)
    result = run_sync(client, lookahead_days=args.days)

    if _emit_data(args, result):
        return

    print(f"Synced {result['site']}")
    for name, stats in result["categories"].items():
        if not stats["synced"]:
            print(f"  {name:<10}  (skipped - fetch failed, see warnings)")
            continue
        notes = []
        if stats["baseline"]:
            notes.append("baseline")
        if stats["new"]:
            notes.append(f"{stats['new']} new")
        if stats["updated"]:
            notes.append(f"{stats['updated']} updated")
        if stats["adopted"]:
            notes.append(f"{stats['adopted']} adopted (newly visible course)")
        note_text = f"  ({', '.join(notes)})" if notes else ""
        print(f"  {name:<10}  {stats['items']:>4} items{note_text}")

    changes = result["changes"]
    if any(s["baseline"] for s in result["categories"].values() if s["synced"]):
        print("\nBaseline established - changes will be reported from the next sync.")
    if changes:
        print(f"\nChanges detected: {len(changes)}")
        _print_change_table(changes)
    else:
        print("\nNo changes since the last sync.")
    for warning in result["warnings"]:
        print(f"Warning: {warning}", file=sys.stderr)


def cmd_changes(args: argparse.Namespace) -> None:
    import time as _time

    client = _client(args)
    # Use the exact --since timestamp: "1h" must mean one hour, not a
    # whole day rounded up.
    since_ts = parse_since(args.since, now=int(_time.time()))
    if since_ts is None:
        since_ts = int(_time.time()) - 7 * 86400
    changes = get_recent_changes(
        client.base_url,
        since_ts=since_ts,
        category=args.category,
    )
    if _emit_data(args, changes):
        return
    if not changes:
        label = f" in category '{args.category}'" if args.category else ""
        print(
            f"No changes recorded{label} in the requested window "
            f"(--since {args.since}). Run 'worsaga sync' to check for new ones."
        )
        return
    _print_change_table(changes)


def cmd_watch(args: argparse.Namespace) -> None:
    client = _client(args)
    # Clamp before announcing so the message matches actual behaviour.
    interval = max(
        MIN_WATCH_INTERVAL,
        parse_interval(args.interval, default=DEFAULT_WATCH_INTERVAL),
    )
    structured = wants_structured(args)

    if not structured and not args.quiet:
        print(
            f"Watching {client.base_url} every {interval}s "
            "(Ctrl+C to stop)...",
            file=sys.stderr,
        )

    def _on_cycle(result: dict) -> None:
        if structured:
            # A clean stream contract: NDJSON (one compact object per
            # line) for JSON, explicit document separators for YAML.
            if getattr(args, "yaml", False):
                # Render before emitting the separator so a missing
                # PyYAML fails cleanly instead of leaving a stray "---".
                rendered = render_structured(result, yaml_mode=True)
                print("---")
                print(rendered, flush=True)
            else:
                print(json.dumps(result, default=str), flush=True)
            return
        stamp = _display_timestamp(result.get("synced_at"))
        if not result.get("ok"):
            print(
                f"[{stamp}] sync failed: {result.get('error')}",
                file=sys.stderr,
            )
            return
        changes = result.get("changes", [])
        print(f"[{stamp}] cycle {result['cycle']}: {len(changes)} change(s)")
        if changes:
            _print_change_table(changes)
        notification = result.get("notification")
        if notification and not notification.get("sent") and not args.quiet:
            print(
                f"  (notification not sent: {notification.get('error', '')})",
                file=sys.stderr,
            )
        for warning in result.get("warnings", []):
            print(f"Warning: {warning}", file=sys.stderr)

    try:
        summary = run_watch(
            client,
            interval_seconds=interval,
            max_cycles=args.cycles,
            notify=not args.no_notify,
            lookahead_days=args.days,
            on_cycle=_on_cycle,
        )
    except KeyboardInterrupt:
        if not structured and not args.quiet:
            print("\nWatch stopped.", file=sys.stderr)
        return

    if not structured and not args.quiet:
        print(
            f"Watch finished: {summary['cycles']} cycle(s), "
            f"{summary['changes_total']} change(s), "
            f"{summary['failures']} failed cycle(s).",
            file=sys.stderr,
        )


def cmd_autosync(args: argparse.Namespace) -> None:
    if args.force_local and args.action != "remove":
        raise ValueError("--force-local only applies to the 'remove' action")
    if args.action == "status":
        result = autosync_status()
        if _emit_data(args, result):
            return
        labels = {"installed": "installed", "absent": "not installed",
                  "unknown": "state unknown"}
        state = labels.get(
            result.get("state", ""),
            "installed" if result["installed"] else "not installed",
        )
        print(f"Auto-sync: {state} ({result.get('method', '?')})")
        record = result.get("record")
        if record:
            print(f"  interval: {record.get('interval_minutes', '?')} min")
            print(f"  command:  {' '.join(record.get('command', []))}")
        if result.get("last_sync_at"):
            print(
                "  last sync (manual or scheduled): "
                f"{_display_timestamp(result['last_sync_at'])}"
            )
        if result.get("error"):
            print(f"Warning: {result['error']}", file=sys.stderr)
        if result.get("record_error"):
            print(f"Warning: {result['record_error']}", file=sys.stderr)
        return

    if args.action == "install":
        seconds = parse_interval(
            args.interval, default=DEFAULT_INTERVAL_MINUTES * 60,
        )
        result = install_autosync(
            max(1, seconds // 60), dry_run=args.dry_run,
        )
    else:
        result = remove_autosync(
            dry_run=args.dry_run, force_local=args.force_local,
        )

    if _emit_data(args, result):
        if result.get("error"):
            sys.exit(1)
        return

    label = "install" if args.action == "install" else "remove"
    if result.get("error"):
        print(f"Error: auto-sync {label} failed: {result['error']}",
              file=sys.stderr)
        if result.get("record_error"):
            print(f"Warning: {result['record_error']}", file=sys.stderr)
        if result.get("record_cleanup_error"):
            print(
                f"Warning: {result['record_cleanup_error']}", file=sys.stderr,
            )
        sys.exit(1)

    suffix = " (dry run - nothing was changed)" if result["dry_run"] else ""
    if args.action == "install":
        print(
            f"Auto-sync {label}: {result['method']}, "
            f"every {result['interval_minutes']} min{suffix}"
        )
    else:
        print(f"Auto-sync {label}: {result['method']}{suffix}")
    for action in result.get("actions", []):
        if "run" in action:
            print(f"  run:    {' '.join(action['run'])}")
        elif "write" in action:
            print(f"  write:  {action['write']}")
        elif "delete" in action:
            print(f"  delete: {action['delete']}")
    if result.get("warning"):
        print(f"Warning: {result['warning']}", file=sys.stderr)
    if result.get("record_error"):
        print(f"Warning: {result['record_error']}", file=sys.stderr)
    if result.get("record_cleanup_error"):
        print(f"Warning: {result['record_cleanup_error']}", file=sys.stderr)
    if not result["dry_run"]:
        done = "installed" if args.action == "install" else "removed"
        if args.action == "install" and result.get("verified"):
            done += " and verified with the scheduler"
        if result.get("scheduler_untouched"):
            print("Local auto-sync files removed; scheduler unchanged.")
        else:
            print(f"Auto-sync {done}.")


def cmd_doctor(args: argparse.Namespace) -> None:
    if _demo_mode(args):
        info = DemoMoodleClient().site_info()
        if _emit_data(args, {
                "ok": True,
                "demo": True,
                "userid": info["userid"],
                "username": info["username"],
                "sitename": info["sitename"],
            }):
            return
        print("OK (demo mode — fake data, no network)")
        print(f"  User:   {info['username']} (id: {info['userid']})")
        print(f"  Site:   {info['sitename']}")
        return

    # Resolve config — report missing credentials as a diagnostic, not a crash.
    try:
        cfg = MoodleConfig.load(
            url=args.url,
            token=args.token,
            userid=args.userid,
            creds_path=getattr(args, "creds_path", None),
        )
    except ValueError as e:
        if wants_structured(args):
            _emit_data(args, {"ok": False, "error": str(e)})
        else:
            print(f"FAIL: {e}")
        sys.exit(1)

    try:
        info = test_connection(cfg)
    except Exception as e:
        if wants_structured(args):
            _emit_data(args, {"ok": False, "error": str(e)})
        else:
            print(f"FAIL: {e}")
        sys.exit(1)

    userid = info.get("userid", 0)
    sitename = info.get("sitename", "")
    username = info.get("username", "")

    if _emit_data(args, {
            "ok": True,
            "userid": userid,
            "username": username,
            "sitename": sitename,
        }):
        return

    print("OK")
    if username:
        print(f"  User:   {username} (id: {userid})")
    elif userid:
        print(f"  User ID: {userid}")
    if sitename:
        print(f"  Site:   {sitename}")


def cmd_config(args: argparse.Namespace) -> None:
    import platform as _platform

    creds_path = getattr(args, "creds_path", None)
    found = _find_config_file(creds_path)
    os_name = _platform.system().lower()  # "linux", "windows", "darwin"

    if _emit_data(args, {
            "config_path": str(found) if found else str(DEFAULT_CONFIG_PATH),
            "config_dir": str(_PLATFORM_CONFIG_DIR),
            "found": found is not None,
            "os": os_name,
        }):
        return

    if found:
        print(f"Config file: {found}")
    else:
        print("No config file found.")
        print(f"Default path: {DEFAULT_CONFIG_PATH}")
    print(f"Config dir:  {_PLATFORM_CONFIG_DIR}")
    print(f"OS:          {os_name}")


def cmd_update(args: argparse.Namespace) -> None:
    from worsaga import __version__

    current_version = __version__
    upgrade_command = _upgrade_command()

    if _emit_data(args, {
            "current_version": current_version,
            "latest_version": None,
            "update_available": None,
            "source": "pypi",
            "install_spec": PUBLIC_INSTALL_SPEC,
            "upgrade_command": upgrade_command,
        }):
        return

    print(f"Current version: {current_version}")
    print("Release source: PyPI")
    print("\nTo upgrade or reinstall, run:")
    print(f"  {upgrade_command}")
    print("\nThis command is a guide only. worsaga does not self-update in place.")


def cmd_setup(args: argparse.Namespace) -> None:
    # Non-interactive mode: all required args provided on CLI
    setup_url = getattr(args, "url", None) or getattr(args, "setup_url", None)
    setup_token = getattr(args, "token", None) or getattr(args, "setup_token", None)
    setup_userid = getattr(args, "userid", None) or getattr(args, "setup_userid", None)

    # The setup subcommand has its own --url/--token/--userid that shadow
    # the top-level ones. argparse puts the subcommand's values on args
    # directly, but we also want to honour the top-level flags if the
    # subcommand's own weren't given.  The subcommand attrs are always
    # present (defaulting to None), so we just use what we have.

    if setup_url and setup_token:
        # Fully non-interactive path
        print("worsaga setup (non-interactive)")
        print("=" * 40)
        print(f"\nVerifying connection to {setup_url}... ", end="", flush=True)
        try:
            cfg = MoodleConfig(url=setup_url.rstrip("/"), token=setup_token, userid=0)
            info = test_connection(cfg)
        except Exception as e:
            print("FAILED")
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        detected_userid = info.get("userid", 0)
        if setup_userid is not None:
            userid = setup_userid
        elif detected_userid:
            userid = detected_userid
            print(f"OK (detected user ID: {userid})")
        else:
            userid = 0
            print("OK (user ID not detected — set manually if needed)")

        dest = MoodleConfig.write_config(url=setup_url, token=setup_token, userid=userid)
        _print_setup_success(dest)
        return

    # Interactive fallback
    if should_show_banner(json_mode=getattr(args, "json", False), quiet=getattr(args, "quiet", False)):
        print_banner()
    print("worsaga setup")
    print("=" * 40)
    print()
    print("This will save your Moodle credentials to:")
    print(f"  {DEFAULT_CONFIG_PATH}")
    print()
    print("You need:")
    print("  1. Your Moodle site URL (e.g. https://moodle.example.ac.uk)")
    print("  2. A web-service token (see README for how to get one)")
    print()

    url = input("Moodle URL: ").strip()
    if not url:
        print("Error: URL is required.", file=sys.stderr)
        sys.exit(1)

    token = getpass.getpass("API token: ").strip()
    if not token:
        print("Error: token is required.", file=sys.stderr)
        sys.exit(1)

    userid_raw = input("User ID (press Enter to auto-detect): ").strip()
    userid_override: int | None = None
    if userid_raw:
        try:
            userid_override = int(userid_raw)
        except ValueError:
            print("Error: User ID must be a number.", file=sys.stderr)
            sys.exit(1)

    print("\nVerifying connection... ", end="", flush=True)
    try:
        cfg = MoodleConfig(url=url.rstrip("/"), token=token, userid=0)
        info = test_connection(cfg)
    except Exception as e:
        print("FAILED")
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    detected_userid = info.get("userid", 0)
    if userid_override is not None:
        userid = userid_override
    elif detected_userid:
        userid = detected_userid
        print(f"OK (detected user ID: {userid})")
    else:
        userid = 0
        print("OK (user ID not detected — set manually if needed)")

    dest = MoodleConfig.write_config(url=url, token=token, userid=userid)
    _print_setup_success(dest)


# ── Main ──────────────────────────────────────────────────────────


def _reconfigure_streams() -> None:
    """Make stdout/stderr tolerate characters their encoding can't represent.

    Windows consoles and pipes often use legacy code pages (cp1252),
    where printing extracted course text — or any non-ASCII character —
    raises UnicodeEncodeError and kills the command. Substituting a
    replacement character is always preferable to crashing mid-output.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            # Non-TextIOWrapper streams (test harnesses, exotic hosts)
            # simply keep their existing behaviour.
            pass


def main(argv: list[str] | None = None) -> None:
    _reconfigure_streams()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "json", False) and getattr(args, "yaml", False):
        parser.error("--json and --yaml cannot be used together")

    if args.command is None:
        if should_show_banner(json_mode=args.json, quiet=args.quiet):
            print_banner()
        parser.print_help()
        sys.exit(0)

    dispatch = {
        "courses": cmd_courses,
        "deadlines": cmd_deadlines,
        "grades": cmd_grades,
        "assignments": cmd_assignments,
        "forums": cmd_forums,
        "forum": cmd_forum,
        "updates": cmd_updates,
        "notifications": cmd_notifications,
        "inbox": cmd_inbox,
        "digest": cmd_digest,
        "calendar": cmd_calendar,
        "contents": cmd_contents,
        "materials": cmd_materials,
        "download": cmd_download,
        "extract": cmd_extract,
        "summary": cmd_summary,
        "setup": cmd_setup,
        "search": cmd_search,
        "index": cmd_index,
        "search-text": cmd_search_text,
        "study-pack": cmd_study_pack,
        "sync": cmd_sync,
        "changes": cmd_changes,
        "watch": cmd_watch,
        "auto-sync": cmd_autosync,
        "doctor": cmd_doctor,
        "update": cmd_update,
        "config": cmd_config,
    }
    try:
        dispatch[args.command](args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
    except urllib.error.HTTPError as e:
        print(f"Error: HTTP {e.code} — {e.reason}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        print(f"Error: network request failed — {reason}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
