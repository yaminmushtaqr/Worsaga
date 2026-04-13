"""CLI entrypoint for worsaga.

Usage:
    worsaga courses              List enrolled courses
    worsaga deadlines [--days N] Show upcoming deadlines
    worsaga contents <id|code>   Show course sections & modules
    worsaga materials <id|code>  Discover downloadable materials
    worsaga download <id|code>   Download a material file
    worsaga summary <id|code>    Generate weekly study summary
    worsaga search <id|code> <q> Search course content by keyword
    worsaga doctor               Check auth and connectivity
    worsaga config [path]        Show active config file location
    worsaga setup                Guided first-time configuration
    worsaga update               Show how to upgrade safely
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.request

from worsaga.banner import print_banner, should_show_banner
from worsaga.client import MoodleClient
from worsaga.config import (
    DEFAULT_CONFIG_PATH,
    MoodleConfig,
    _PLATFORM_CONFIG_DIR,
    _find_config_file,
    test_connection,
)

from worsaga.deadlines import get_upcoming_deadlines
from worsaga.materials import (
    MaterialSelectionError,
    download_material,
    extract_materials,
    get_section_materials,
    match_section,
    search_course_content,
    select_material,
)
from worsaga.summaries import (
    build_summary,
    find_best_section,
    format_bullets,
    get_downloadable_files,
    summarize_modules,
)


class CourseResolutionError(ValueError):
    """Raised when a course identifier cannot be resolved."""


def _build_parser() -> argparse.ArgumentParser:
    from worsaga.banner import _get_version

    parser = argparse.ArgumentParser(
        prog="worsaga",
        description="Study kit for university LMS systems. Moodle is supported today.",
    )
    parser.add_argument(
        "-V", "--version", action="version",
        version=f"%(prog)s {_get_version()}",
    )
    parser.add_argument(
        "--json", action="store_true", help="Output machine-readable JSON"
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

    # Shared flags inherited by every subcommand so that --json and --quiet
    # work both before and after the subcommand name.  Using SUPPRESS avoids
    # overwriting a value already set by the top-level parser.
    _shared = argparse.ArgumentParser(add_help=False)
    _shared.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS,
        help="Output machine-readable JSON",
    )
    _shared.add_argument(
        "-q", "--quiet", action="store_true", default=argparse.SUPPRESS,
        help="Suppress progress output on stderr",
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
        help="Moodle course ID (integer) or course short-code (e.g. EC100)",
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
            "The file_url field in JSON output is for provenance only — do not\n"
            "fetch it directly. Use 'worsaga download' for authenticated\n"
            "retrieval."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mt.add_argument(
        "course",
        help="Moodle course ID (integer) or course short-code (e.g. EC100)",
    )
    mt.add_argument(
        "--week",
        default=None,
        help="Filter to a specific teaching week (number or name substring)",
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
            "  worsaga materials EC100 --week 3          # discover files\n"
            "  worsaga download EC100 --week 3 --index 0 # fetch one\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    dn.add_argument(
        "course",
        help="Moodle course ID (integer) or course short-code (e.g. EC100)",
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

    sm = sub.add_parser(
        "summary", parents=[_shared], help="Generate weekly study summary",
    )
    sm.add_argument(
        "course",
        help="Moodle course ID (integer) or course short-code (e.g. EC100)",
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
        help="Moodle course ID (integer) or course short-code (e.g. EC100)",
    )
    sr.add_argument("query", help="Search keyword (case-insensitive)")

    sub.add_parser("doctor", parents=[_shared], help="Check auth and connectivity")
    sub.add_parser("update", parents=[_shared], help="Show how to upgrade safely")

    cfg = sub.add_parser("config", parents=[_shared], help="Show configuration info")
    cfg.add_argument(
        "action", nargs="?", default="path", choices=["path"],
        help="Config action (default: path)",
    )

    return parser


# ── Helpers ───────────────────────────────────────────────────────


def _client(args: argparse.Namespace) -> MoodleClient:
    """Build a MoodleClient, respecting top-level credential overrides."""
    return MoodleClient(
        MoodleConfig.load(
            url=args.url,
            token=args.token,
            userid=args.userid,
            creds_path=getattr(args, "creds_path", None),
        )
    )


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
    """Return a safe upgrade command for the current platform."""
    os_name = os_name or os.name
    if os_name == "nt":
        return "py -m pip install --upgrade worsaga"
    return "python3 -m pip install --upgrade worsaga"


def _get_latest_pypi_version(timeout: float = 3.0) -> str | None:
    """Return the latest version from PyPI, or None if unavailable."""
    try:
        with urllib.request.urlopen(
            "https://pypi.org/pypi/worsaga/json", timeout=timeout
        ) as response:
            data = json.load(response)
        return data.get("info", {}).get("version") or None
    except Exception:
        return None


def _version_key(version: str) -> tuple[int, ...]:
    """Return a comparable numeric tuple for simple dotted versions."""
    parts = re.findall(r"\d+", version)
    return tuple(int(part) for part in parts)


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
    print(f"  {cmd} summary <course> --week 1  # AI study notes for a week")


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
    if args.json:
        payload = courses if getattr(args, "raw", False) else _normalize_courses(courses)
        print(json.dumps(payload, indent=2))
        return
    if not courses:
        print("No enrolled courses found.")
        return
    print(f"{'ID':>8}  {'Short code':<20}  {'Full name'}")
    print(f"{'─' * 8}  {'─' * 20}  {'─' * 40}")
    for c in courses:
        print(f"{c['id']:>8}  {c.get('shortname', ''):.<20}  {c.get('fullname', '')}")


def cmd_deadlines(args: argparse.Namespace) -> None:
    client = _client(args)
    deadlines = get_upcoming_deadlines(client, lookahead_days=args.days)
    if args.json:
        print(json.dumps(deadlines, indent=2))
        return
    if not deadlines:
        print(f"No deadlines in the next {args.days} days.")
        return
    print(f"{'Due':<20}  {'Days':>4}  {'Type':<12}  {'Course':<15}  {'Name'}")
    print(f"{'─' * 20}  {'─' * 4}  {'─' * 12}  {'─' * 15}  {'─' * 30}")
    for d in deadlines:
        print(
            f"{d['due_str']:<20}  {d['days_left']:>4}  {d['type']:<12}  "
            f"{d['course']:<15}  {d['name']}"
        )


def cmd_contents(args: argparse.Namespace) -> None:
    client = _client(args)
    course_id = _resolve_course_id(client, args.course)
    contents = client.get_course_contents(course_id)

    # Filter by week if requested
    if args.week is not None:
        contents = [s for s in contents if match_section(s, args.week)]

    if args.json:
        payload = contents if getattr(args, "raw", False) else _normalize_contents(contents)
        print(json.dumps(payload, indent=2))
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

    if args.json:
        print(json.dumps(materials, indent=2))
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
        f"{'─' * 30}  {'─' * 25}  {'─' * 10}  "
        f"{'─' * 30}  {'─' * 10}"
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


def cmd_download(args: argparse.Namespace) -> None:
    from worsaga.materials import _candidate_summary

    client = _client(args)
    course_id = _resolve_course_id(client, args.course)
    sections = client.get_course_contents(course_id)
    materials = get_section_materials(
        sections, course_id, args.week, base_url=client.base_url,
    )

    if not materials:
        msg = f"No materials found for week {args.week}."
        if args.json:
            print(json.dumps({"error": msg, "candidates": []}, indent=2))
        else:
            print(msg, file=sys.stderr)
        sys.exit(1)

    try:
        chosen = select_material(
            materials, match=getattr(args, "match", None),
            index=getattr(args, "index", None),
        )
    except MaterialSelectionError as exc:
        candidates = [
            _candidate_summary({**c, "_index": i})
            for i, c in enumerate(exc.candidates)
        ]
        if args.json:
            print(json.dumps({
                "error": str(exc),
                "candidates": candidates,
            }, indent=2))
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

    if not args.quiet:
        print(
            f"Downloading {chosen.get('file_name') or chosen.get('module_name')}...",
            file=sys.stderr,
        )

    result = download_material(client, chosen, output_dir=args.output)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        size = result["bytes_written"]
        size_str = (
            f"{size / 1_048_576:.1f} MB" if size >= 1_048_576
            else f"{size / 1024:.0f} KB" if size > 0
            else "0 B"
        )
        print(f"Saved: {result['local_path']} ({size_str})")


def cmd_summary(args: argparse.Namespace) -> None:
    from worsaga.extraction import extract_file_text

    client = _client(args)
    course_id = _resolve_course_id(client, args.course)
    sections = client.get_course_contents(course_id)

    week_query = args.week
    section, section_type, section_name = find_best_section(sections, week_query)

    # Display label: use raw query for non-numeric, resolved for numeric
    week_label = week_query

    if args.json:
        # For JSON mode: build full structured output
        file_texts: list[tuple[str, str]] = []
        if section and section.get("modules"):
            files = get_downloadable_files(section["modules"])
            for finfo in files:
                url = finfo["fileurl"]
                if not url:
                    continue
                data = client.download_file(url)
                if data:
                    text = extract_file_text(data, finfo["filename"], clean=True)
                    if text:
                        file_texts.append((finfo["filename"], text))

        result = build_summary(
            file_texts, section_type=section_type,
        )
        result["section_name"] = section_name
        result["week"] = week_query
        result["course_id"] = course_id
        print(json.dumps(result, indent=2))
        return

    # Human-friendly output
    print(f"Week {week_label} — {section_name or '(no section found)'}")
    if section and section.get("modules"):
        overview = summarize_modules(section["modules"])
        if overview:
            print(f"Materials: {overview}")
        print()

        files = get_downloadable_files(section["modules"])
        if files:
            file_texts = []
            for finfo in files:
                url = finfo["fileurl"]
                if not url:
                    continue
                if not args.quiet:
                    print(f"  Extracting {finfo['filename']}...", file=sys.stderr)
                data = client.download_file(url)
                if data:
                    text = extract_file_text(data, finfo["filename"], clean=True)
                    if text:
                        file_texts.append((finfo["filename"], text))

            result = build_summary(
                file_texts, section_type=section_type,
            )
        else:
            result = build_summary([], section_type=section_type)
    else:
        result = build_summary([], section_type=section_type)

    print(f"Study notes ({result['method']}):")
    print(format_bullets(result["bullets"]))


def cmd_search(args: argparse.Namespace) -> None:
    client = _client(args)
    course_id = _resolve_course_id(client, args.course)
    sections = client.get_course_contents(course_id)
    results = search_course_content(sections, args.query)

    if args.json:
        print(json.dumps(results, indent=2))
        return

    if not results:
        print(f"No matches for '{args.query}'.")
        return

    print(f"{'Section':<30}  {'Module':<30}  {'Type':<12}")
    print(f"{'─' * 30}  {'─' * 30}  {'─' * 12}")
    for r in results:
        print(
            f"{r['section_name'][:30]:<30}  "
            f"{r['module_name'][:30]:<30}  "
            f"{r['module_type']:<12}"
        )


def cmd_doctor(args: argparse.Namespace) -> None:
    # Resolve config — report missing credentials as a diagnostic, not a crash.
    try:
        cfg = MoodleConfig.load(
            url=args.url,
            token=args.token,
            userid=args.userid,
            creds_path=getattr(args, "creds_path", None),
        )
    except ValueError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        else:
            print(f"FAIL: {e}")
        sys.exit(1)

    try:
        info = test_connection(cfg)
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        else:
            print(f"FAIL: {e}")
        sys.exit(1)

    userid = info.get("userid", 0)
    sitename = info.get("sitename", "")
    username = info.get("username", "")

    if args.json:
        print(json.dumps({
            "ok": True,
            "userid": userid,
            "username": username,
            "sitename": sitename,
        }, indent=2))
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

    if args.json:
        print(json.dumps({
            "config_path": str(found) if found else str(DEFAULT_CONFIG_PATH),
            "config_dir": str(_PLATFORM_CONFIG_DIR),
            "found": found is not None,
            "os": os_name,
        }, indent=2))
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
    latest_version = _get_latest_pypi_version()
    upgrade_command = _upgrade_command()
    update_available = (
        latest_version is not None
        and _version_key(latest_version) > _version_key(current_version)
    )
    ahead_of_pypi = (
        latest_version is not None
        and _version_key(current_version) > _version_key(latest_version)
    )

    if args.json:
        print(json.dumps({
            "current_version": current_version,
            "latest_version": latest_version,
            "update_available": update_available,
            "ahead_of_pypi": ahead_of_pypi,
            "upgrade_command": upgrade_command,
        }, indent=2))
        return

    print(f"Current version: {current_version}")
    if latest_version:
        print(f"Latest PyPI version: {latest_version}")
    else:
        print("Latest PyPI version: unavailable")

    if update_available:
        print("\nAn update is available. Run:")
        print(f"  {upgrade_command}")
    elif ahead_of_pypi:
        print("\nYour current build is newer than the latest PyPI release.")
    else:
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
    print("  1. Your Moodle site URL (e.g. https://moodle.lse.ac.uk)")
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

    print("\nVerifying connection... ", end="", flush=True)
    try:
        cfg = MoodleConfig(url=url.rstrip("/"), token=token, userid=0)
        info = test_connection(cfg)
    except Exception as e:
        print("FAILED")
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    detected_userid = info.get("userid", 0)
    if userid_raw:
        userid = int(userid_raw)
    elif detected_userid:
        userid = detected_userid
        print(f"OK (detected user ID: {userid})")
    else:
        userid = 0
        print("OK (user ID not detected — set manually if needed)")

    dest = MoodleConfig.write_config(url=url, token=token, userid=userid)
    _print_setup_success(dest)


# ── Main ──────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        if should_show_banner(json_mode=args.json, quiet=args.quiet):
            print_banner()
        parser.print_help()
        sys.exit(0)

    dispatch = {
        "courses": cmd_courses,
        "deadlines": cmd_deadlines,
        "contents": cmd_contents,
        "materials": cmd_materials,
        "download": cmd_download,
        "summary": cmd_summary,
        "setup": cmd_setup,
        "search": cmd_search,
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
