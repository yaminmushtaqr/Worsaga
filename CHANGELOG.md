# Changelog

All notable changes to Worsaga are documented in this file.

## 0.8.2 (unreleased)

### Added

- MCP tools now accept a course short-code anywhere they take a
  `course_id`, not just a numeric id. Every course-taking tool
  (`get_grades`, `get_course_contents`, `get_week_materials`,
  `download_material`, `extract_material`, `get_assignments`,
  `get_course_forums`, `get_calendar_events`, `build_search_index`, and
  the rest) resolves an `int | str` argument the same way the CLI does — an
  int or digit-string is used directly, a name is matched case-insensitively
  by exact short-code and then by unambiguous prefix — so an agent no longer
  has to call `list_courses` and match ids itself. An unknown name returns a
  structured `{"error", "error_code": "course_not_found"}` dict; an ambiguous
  prefix returns the new `{"error", "error_code": "course_ambiguous",
  "candidates": [{id, shortname, fullname}, ...]}`. The CLI and MCP now share
  one resolver in `worsaga.courses`.
- New read-only MCP tool `get_connection_info` — a cheap "am I connected?"
  check that reports `authenticated`, `demo_mode`, the Moodle `site_url`
  (base URL only), `site_name`, the authenticated `user_id` and
  `user_display_name`, the `worsaga_version`, and a `config_source` hint
  (`env` / `file` / `demo` / `unset`, with the file *path* only, never its
  contents). It makes at most one `core_webservice_get_site_info` call and
  returns a structured `{"error", "error_code": "auth" | "network"}` dict on
  failure. The token never appears in any field. This brings the MCP tool
  count to 26.
- Forum-discussion, notification, message, and grade records now carry
  derived timestamp fields alongside their raw Unix epochs, matching the
  `due_iso` / `due_str` and `start_iso` / `start_str` fields already on
  deadlines and calendar events. Forum discussions gain
  `created_iso` / `created_str` and `modified_iso` / `modified_str`;
  notifications and messages gain `created_iso` / `created_str`; grade
  records gain a `graded_at` epoch (from Moodle's `gradedategraded`) plus
  `graded_iso` / `graded_str`. `*_iso` is UTC ISO-8601; `*_str` is a local
  display string; both are empty when no timestamp is present. The epoch
  ints are unchanged.
- Live progress for the all-course commands. `digest`, `sync`,
  `assignments`, `updates`, `grades`, and each `watch` cycle now print a
  one-line `[k/N] label` progress indicator to **stderr** as each course
  (or forum, assignment, or digest source) completes, so a full-account run
  no longer looks hung for minutes with no output. `watch` additionally
  announces `Sync cycle started (N courses)...` at the start of every cycle.
  Progress is stderr-only (stdout stays a clean data channel), and is
  suppressed by `-q/--quiet` and in `--json`/`--yaml` machine modes. It is
  wired through an optional callback on the shared orchestrators, so the MCP
  server (which shares them over stdio) passes nothing and stays silent.

### Fixed

- CLI `--json`/`--yaml` modes now emit the same structured error dict the
  MCP tools return when a course argument cannot be resolved, instead of
  leaving stdout empty. An unknown id or short-code produces
  `{"error", "error_code": "course_not_found"}` and an ambiguous prefix
  produces `{"error", "error_code": "course_ambiguous", "candidates":
  [{id, shortname, fullname}, ...]}` on stdout (exit 1), so machine
  callers can branch instead of hitting a JSON parse error on empty
  output. Human mode is unchanged.
- The `courses` table pads short codes with spaces instead of the old
  dotted leader (`ECON101_2526........`), which had become misleading now
  that `...` elsewhere marks a genuinely truncated value; over-long short
  codes get the same `...` indicator as every other table.
- Live progress labels are cleaned before display: assignment names are
  HTML-unescaped (`Group 1 &amp; 2 ...` now shows as `Group 1 & 2 ...`,
  matching the final table), and `updates` progress lines are prefixed
  with the course short-code (`STAT120_2526: Announcements`) since most
  Moodle forums share the default name "Announcements".
- The remaining hand-rolled human tables now mark a truncated cell with an
  ASCII `...` indicator instead of a silent hard slice, via the shared
  `truncate_cell` helper. The `grades`, `assignments`, `forums`,
  `forum latest`, `updates`, `notifications`, and `inbox` tables were
  cutting long values with no sign they had been cut (for example the
  notifications "Sender" column showed `Worsaga Demo Univers`); a cut value
  now reads `Worsaga Demo Univ...`. Column widths are unchanged.
- `worsaga setup` no longer crashes with a raw `EOFError` traceback when
  stdin is not an interactive terminal. It now detects a non-TTY stdin up
  front and exits cleanly (exit 1) with guidance toward the
  non-interactive alternatives (`--url`/`--token`, the `WORSAGA_URL` /
  `WORSAGA_TOKEN` / `WORSAGA_USERID` environment variables, or a
  `WORSAGA_CREDS_PATH` JSON file). A mid-prompt end-of-input or `Ctrl-C`
  is likewise caught and reported on one line (`Ctrl-C` exits 130). The
  existing config file is never written on any abort path.
- A week query that matches no section is now an explicit failure instead
  of silently fabricating output. Previously `study-pack` / `summary` (and
  the MCP `get_weekly_summary` / `export_study_pack`) would invent a
  plausible pack or summary — a titled document with fallback boilerplate —
  for a nonsense week such as `zzz_nonsense`. They now report
  `no section matching week '<week>' in course <id/shortname>` on stderr
  (exit 1) with the available section names, and the MCP tools return a
  structured `{"error", "error_code": "week_not_found", "available_sections"}`
  dict. `get_week_materials` returns the same structured error for an
  unmatched week rather than an empty list. A week that *does* match a
  section but has no downloadable materials remains a valid empty state and
  is unchanged.
- `find_best_section` no longer loses a name/number match in a
  materials-free course: a section that matches the week by name but holds
  no files (and where no other section has files either) is returned as the
  matched empty section instead of `None`, so `None` now reliably means
  "no section matched at all".
- Consistent exit codes for empty weeks between `materials` and `download`.
  Listing an empty-but-valid week with `materials --week` stays exit 0 (an
  empty listing is a valid answer) and now states that a section was found
  but has no downloadable files; `download` on the same week stays exit 1
  (it could not fetch anything); and a week that matches no section at all
  is exit 1 for both, with the clear week-not-found message.
- Domain failures in the MCP server now return an agent-branchable
  `{"error", "error_code"}` dict instead of raising a bare `isError` string
  built from raw Moodle DB wording. A bad course id (in
  `get_course_contents`, `get_grades`, `get_grade_summary`,
  `get_week_materials`, `search_course_content`, `get_weekly_summary`,
  `get_calendar_events`, `export_study_pack`, `download_material`,
  `extract_material`) returns `error_code: "course_not_found"`; an
  assignment id that is not in the course (`get_assignment_status`) returns
  `error_code: "assignment_not_found"`. The client raises the new
  `CourseNotFoundError` / `AssignmentNotFoundError` in place of the raw
  "Can't find data record in database table course." message, so the CLI
  also reports a friendly `Course <id> not found (not enrolled or does not
  exist).` (exit 1). The full `error_code` vocabulary is documented on the
  MCP server (`ERROR_CODES`).
- The `extract_material` MCP tool's response is now deterministically
  bounded to about 130,000 characters. Previously the per-page `text` cap
  applied to `text` alone while each page also carried a same-size
  `markdown` field, so a large PDF could return roughly twice the intended
  size. `markdown` is now omitted by default (the `text` field carries the
  content; pass `include_markdown=True` for the Markdown view, with the
  text budget reduced so the combined response stays within the bound). A
  file too large to fit is truncated with an explicit warning that says how
  to get the rest. The CLI `extract` command is unchanged.
- A below-minimum `--interval` is no longer silently overridden. `worsaga
  watch` (floor 60s) and `worsaga auto-sync install` (floor 5 min)
  previously clamped a too-small interval up with no indication — a
  `--interval 30s` watch quietly ran every 60s, and `auto-sync install
  --interval 30s|45|2m|90s` quietly became every 5 min. Both now print a
  one-line stderr warning stating the requested and applied values (e.g.
  `Warning: interval 30s is below the minimum for watch; using 60s.`), and
  each floor is documented in the command's `--interval` help text. The
  clamping behaviour itself is unchanged.
- The `notifications` "Sender" column is no longer always blank.
  `message_popup_get_popup_notifications` on many Moodle instances returns
  only a numeric `useridfrom` and no `userfromfullname`, which the sender
  mapping did not read. It now prefers the sender's full name (including a
  nested `userfrom` object) and falls back to a `User <id>` label from
  `useridfrom`/`userfromid` before omitting the sender entirely.
- Table columns that truncate a long section, module, or file name in the
  `materials` listing (and any table rendered through the shared
  `format_table` helper) now show an ASCII `...` indicator on the cut cell,
  so a truncated name is never mistaken for the whole name. The column
  widths are unchanged and the indicator is ASCII (never the Unicode
  ellipsis) so cosmetic output stays cp1252-safe on legacy consoles.
- `--yaml` output now emits non-ASCII characters literally
  (`allow_unicode=True`) instead of escaping them as `\uXXXX`, so an en-dash
  stays an en-dash. This is safe on legacy consoles because stdout/stderr
  are reconfigured with `errors="replace"`, degrading an unencodable
  character to a replacement rather than crashing the command.

### Changed

- The per-course / per-forum metadata fan-outs behind `digest`, `sync`,
  `assignments`, `updates`, and `grades` now run on a small bounded thread
  pool (default 6 workers, `WORSAGA_CONCURRENCY` to override) instead of one
  blocking request at a time. On a real multi-course account this turns
  multi-minute waits into seconds. Results are always reassembled in the
  original course order and the diff/write phase of `sync` stays
  single-threaded, so change detection is byte-for-byte identical to the
  sequential path; per-course permission warnings stay attributed to their
  own course. The shared read-only client is safe to use across threads (it
  holds only immutable config and opens a fresh connection per request).
- The MCP `list_courses` and `get_course_contents` tools now return
  compact, normalized records through Worsaga's own model layer instead of
  the raw Moodle payloads. `list_courses` returns `id`, `shortname`,
  `fullname`, `category`, `start_at`, `end_at` and drops the HTML course
  `summary`, `enrolledusercount`, and course image. `get_course_contents`
  returns one record per section (`section_id`, `section_num`,
  `section_name`, a plain-text `summary` with the section HTML stripped)
  holding compact module records (`module_id`, `module_name`,
  `module_type`, `view_url`, and a `files` list of token-free metadata
  matching `get_week_materials`, including its `dedupe_key`). Both routes
  pass the token/`file_url` sanitisation boundary.

### Security

- Closed a latent token-leak path in the MCP `get_course_contents` tool. It
  previously returned the verbatim `core_course_get_contents` payload,
  which on mobile-service-enabled Moodle instances embeds the webservice
  token in per-file `fileurl` values. The tool now strips all raw
  `file_url` values and routes the result through the sanitisation boundary
  used by the other discovery tools, so no token or authenticated URL can
  appear in its output.

## 0.8.1

### Changed

- Maintenance re-release of the 0.8 feature set under a fresh, immutable
  package version. Runtime behaviour is unchanged from 0.8.0.
- Release automation now builds and tests without access to the PyPI OIDC
  permission, grants that permission only to the isolated publish job, and
  pins the GitHub-provided actions to current immutable revisions.

## 0.8.0

### Added

- Watch mode. `worsaga watch [--interval 15m] [--cycles N]` runs the
  metadata sync in a foreground loop, prints detected changes each
  cycle, and raises a local desktop notification when something
  changed. Structured mode is a clean stream: `--json` emits one
  compact JSON object per line (NDJSON) and `--yaml` separates cycles
  with `---` document markers. A failed cycle (network down) is
  reported — with a timestamp — and the loop continues; intervals are
  clamped to at least 60 seconds (announced post-clamp) and
  `--cycles 0` runs nothing.
- Local notification backends, best effort and dependency-free: a
  Windows toast via PowerShell/WinRT, macOS `osascript`, and Linux
  `notify-send`, with a structured no-backend result everywhere else.
  Notification content is course metadata only — never tokens or URLs
  — and content is escaped/argv-passed so it can never inject into the
  notification scripts.
- `worsaga auto-sync install|status|remove`: register a periodic
  `worsaga sync --quiet` with the platform scheduler (Task Scheduler
  on Windows, a launchd LaunchAgent on macOS, a systemd user timer on
  Linux; systems without user systemd get a clear cron suggestion).
  `install`/`remove` support `--dry-run`, which shows the exact
  commands and files involved without changing anything (including the
  local metadata record); `status` is strictly read-only (it never
  creates the cache as a side effect) and reports a three-valued
  `state`: installed, absent, or unknown. Absence is only concluded
  from machine-readable evidence — a successful full task/job listing
  without the job, or systemd's `LoadState=not-found` — never from a
  bare nonzero exit, so access-denied or bus failures read as unknown
  rather than absent. Removal is strict: a failed `launchctl unload`
  or `systemctl disable` reports an error and deletes nothing, an
  unknown scheduler state aborts without mutation, a launchd job that
  outlived its plist is stopped by label, and an enabled-but-inactive
  systemd timer still counts as installed — an active job can never
  be orphaned. A machine whose `systemctl` is missing fails closed
  whenever unit files, a systemd install record, or an unreadable local
  record exist (absent unit files prove nothing: systemd can keep a
  loaded timer until reload);
  the explicit `remove --force-local` escape hatch deletes only
  Worsaga's local files, never queries or changes the scheduler, and
  reports the manual cleanup command. Install re-queries the scheduler
  afterwards and reports `verified`, since schedulers can accept a
  registration without guaranteeing the job runs; a record-write
  failure after successful registration is reported structurally
  (`installed: true`, `record_written: false`, `record_error`) instead
  of raising past an already-active job. During reinstall, the prior
  record is atomically quarantined before the scheduler changes; if that
  cannot be done, installation aborts without touching the scheduler,
  and a later write failure makes `status` omit the quarantined stale
  interval/command instead of presenting it as current. The record is
  written atomically. The scheduled
  command line contains no credentials. A local `autosync.json`
  record keeps `status` honest without parsing locale-dependent
  scheduler output, and `status` reports the cache's most recent sync
  time (manual or scheduled — sync provenance is not recorded).
- MCP: read-only `get_autosync_status` tool. Installing or removing
  the scheduled sync stays CLI-only by design.
- New public API: `worsaga.run_watch`, `worsaga.send_notification`,
  `worsaga.notification_backend`, `worsaga.install_autosync`,
  `worsaga.autosync_status`, `worsaga.remove_autosync`,
  `worsaga.read_last_sync_at` (read-only cache timestamp reader).

## 0.7.0 (not published separately — first shipped in 0.8.0)

### Added

- Local full-text search over course materials. `worsaga index` (CLI)
  and `build_search_index` (MCP) fetch supported files (PDF, PPTX,
  DOCX, TXT) in memory, extract their text, and store it page by page
  in a SQLite FTS5 index in the platform-native user data directory
  (`WORSAGA_INDEX_PATH` overrides the location). Files unchanged since
  the last run are skipped without a fetch, and a per-run file budget
  makes repeated runs resume where the previous one stopped.
  Full-course builds reconcile deletions: files removed or renamed on
  Moodle disappear from the index (`files_removed` in the report), but
  only for courses whose material list was successfully enumerated —
  a failed fetch is never mistaken for an emptied course, and
  week-scoped builds never delete.
- `worsaga search-text <query>` (CLI) and `search_text` (MCP): offline
  full-text search over the indexed material text, with course
  filtering, per-hit course/section/file/page context, highlighted
  snippets, and relevance ranking. Results distinguish "no match" from
  "nothing indexed yet".
- Markdown study-pack exports. `worsaga study-pack <course> --week N`
  (CLI) and `export_study_pack` (MCP) build a single self-contained
  Markdown document for a teaching week — deterministic study notes, a
  materials overview, and the extracted per-page content of the
  section's supported files (up to eight; larger sections are included
  in listed order with an explicit warning). Each file is downloaded
  once, in memory; packs are written UTF-8 and never overwrite
  existing files.
- Token hygiene extends to both new storage/output surfaces: raw
  `file_url` values are never stored or exported, the only URL kept is
  the token-free `view_url`, embedded `token=`-style values are
  redacted, and the index database is owner-only (0600) on POSIX.
  Entire responses — index build reports, search hits, and every
  study-pack field (course names, bullets, file names, warnings), not
  just the markdown — pass through the same sanitizer.
- New public API: `worsaga.build_text_index`, `worsaga.search_text_index`,
  `worsaga.TextIndexStore`, `worsaga.TextIndexError`,
  `worsaga.default_index_path`, `worsaga.build_study_pack`,
  `worsaga.write_study_pack`.

## 0.6.0

### Added

- Local metadata cache and sync. `worsaga sync` (CLI) and `sync_now`
  (MCP) fetch metadata-only snapshots — upcoming deadlines, file
  metadata, grades, and forum discussions; never file contents — into a
  local SQLite cache in the platform-native user data directory
  (`WORSAGA_CACHE_PATH` overrides the location).
- Change detection across syncs: new deadlines, moved deadlines, new
  files, updated files, grade updates, and new/updated forum
  discussions. The first sync establishes a baseline and reports no
  changes; detected changes are recorded and can be replayed with
  `worsaga changes [--since 7d] [--category ...]` (CLI) or
  `get_changes` (MCP) without touching the network.
- Cache rows are keyed by Moodle site, so demo-mode data never mixes
  with real course data. Tokens and authenticated URLs are stripped at
  the storage boundary and never reach the cache file; a failed
  category fetch is reported as a warning and skipped rather than
  mistaken for an empty (or changed) Moodle.
- New public API: `worsaga.run_sync`, `worsaga.get_recent_changes`,
  `worsaga.CacheStore`, `worsaga.default_cache_path`.

### Changed

- Deadline entries (CLI `deadlines`, MCP `get_deadlines`) now include
  the Moodle activity `id` alongside the existing fields.
- `worsaga changes --since` is now exact: `--since 1h` queries one hour,
  not a whole day rounded up. `get_recent_changes` accepts `since_ts`.
- `get_upcoming_deadlines` accepts `strict=True` to propagate
  assignment/quiz fetch failures instead of returning a partial list
  (sync uses this so a partial snapshot is never treated as complete).
- Change events include every fingerprinted field in `before`/`after`,
  so no change is opaque (e.g. feedback-only grade updates).

### Changed (breaking)

- Moodle URLs must use HTTPS. The API token travels with every request,
  so plain `http://` would expose it on the wire; `http://` is accepted
  only for localhost development servers. Existing configs with
  non-local `http://` URLs now fail with a clear error.
- The `mod_assign_get_grades` web-service function was removed from the
  read-only allowlist along with `MoodleClient.get_assignment_grades`.
  Its response can include other students' grades for teacher-capable
  tokens, and the authenticated user's own grade/feedback data already
  comes from per-user submission status and the gradebook report. The
  CLI `--include-feedback` flag is now a documented no-op.
- Legacy `.ppt` and `.doc` files are no longer listed as supported
  extraction formats (there was never an extractor for them); they no
  longer consume the summary pipeline's download budget, and extraction
  reports a clear "convert to .pptx/.docx" warning.

### Fixed

- PPTX extraction now honours the real presentation slide order (from
  `presentation.xml` relationships, not XML file names), keeps
  image-only and empty slides as numbered pages, reports true per-slide
  image counts, and strips headers repeated across slides (cleaning
  frequencies are computed document-wide, not per page). Corrupt
  archives produce a structured warning instead of silent zero pages.
- PPTX/DOCX parsing is bounded by archive safety budgets (member count,
  per-member and total decompressed size), so a small download can no
  longer decompress into unbounded memory (zip bomb).
- Downloads write to a uniquely named temporary file and atomically
  claim their destination, so concurrent or repeated downloads can no
  longer truncate a pre-existing `.part` file or overwrite each other.
- Concurrent `sync` runs serialize through an immediate SQLite
  transaction instead of recording duplicate change events.
- Baseline state is recorded explicitly per site and category, so a
  legitimately empty category still finishes baselining and its first
  real item is reported as a change.
- Grades sync records per-course coverage: items from a course whose
  gradebook was previously unreadable are adopted silently on recovery
  instead of being reported as spurious `grade_updated` events.
- Cache sanitization now also drops any key containing `token`, redacts
  `token=`/`sesskey=` values embedded in stored strings, and the cache
  file is created owner-only (0600) on POSIX, matching the credentials
  file.
- The public-release audit script piped file lists into its scan
  function, which ran the failure counter in a subshell — scan findings
  printed but could never fail the audit. Scans now run in the main
  shell (process substitution), so findings are fatal again. GitHub
  Actions pinned by full commit SHA are explicitly allowed by the
  credential-shape scan.
- The publish workflow now verifies that the pushed tag matches the
  package version, publishes only on tag pushes (manual dispatch is a
  dry run), and pins all actions to full commit SHAs. A `.gitattributes`
  keeps shell scripts LF so the audit runs from Windows checkouts.

## 0.5.0

### Added

- Per-page structured extraction is now user-facing. The new
  `worsaga extract` CLI command and `extract_material` MCP tool fetch a
  material into memory (nothing is written to disk) and return its text
  page by page — slide by slide for PPTX — with per-page Markdown
  rendering, image counts, low-text-density flags, and structured
  warnings. Selection works exactly like `download` (`--match`,
  `--index`, candidate lists on ambiguity), and demo mode is fully
  supported.
- Light cleaning is applied by default and preserves educational
  content — figure/table captions, learning objectives, source lines,
  and references. Pass `--raw` (CLI) or `clean=false` (MCP) for the
  unmodified extractor output; `--max-chars` / `max_chars` caps the
  total text.
- New public API: `worsaga.extract_material_content`.

## 0.4.0

First clean public release.

### Changed

- Relicensed to GNU AGPL-3.0-only. The full licence text now ships as
  `LICENSE`, and package metadata uses the `AGPL-3.0-only` SPDX expression.
- README, AGENTS.md, banner, and MCP server descriptions rewritten for the
  public, PyPI-installed distribution. Moodle is the only supported LMS
  provider today; no other provider is claimed or promised.
- `worsaga update` now points at the public PyPI/pipx upgrade path.
- Examples and test fixtures use fictional course codes (`ECON101`, `CS210`,
  `PSY110`, `STAT120`) and example hostnames only.
- Slide-noise extraction heuristics generalised; they no longer reference any
  specific institution.

### Changed (breaking)

- Raw Moodle `file_url` values are no longer included in default
  `materials` JSON output or MCP `get_week_materials` responses. These
  URLs require token authentication and were documented as
  provenance-only; agent configs that read `file_url` must switch to the
  authenticated `download` / `download_material` path, or pass the new
  CLI flag `--include-file-urls` where provenance is explicitly needed.
- `MoodleClient.download_file` now raises `worsaga.DownloadError` (with a
  stable `code`: `auth`, `not_found`, `network`, `oversize`,
  `invalid_url`, `empty`) instead of returning `None` on failure, and
  caps downloads at 50 MB by default.
- Human-readable timestamps are now rendered in the system local
  timezone with an explicit `UTC±HH:MM` label (previously always UTC).
  Machine-readable fields (`due_iso`, `start_iso`) remain UTC ISO-8601.
- MCP `download_material` saves into Worsaga's platform-native downloads
  directory instead of the server process working directory; its
  `output_dir` parameter is now a relative subdirectory inside that
  directory, and absolute or traversal paths are rejected.

### Safety

- Downloads are size-capped (50 MB) with a `Content-Length` precheck and
  overflow detection; oversize files are skipped with a structured error —
  never silently truncated. Files are written to a temp path and renamed
  atomically, so a failed download cannot leave a partial file.
- Weekly summaries cap the number of downloaded files (6) and PDF
  extraction stops after 150 pages; skipped files surface as warnings.
- Removed `core_enrol_get_enrolled_users` from the read-only allowlist:
  it exposes other students' personal data and no feature needs it.
- Every request now sends a `User-Agent: worsaga/<version>` header.

### Added

- Demo mode: `worsaga --demo <command>` (or `WORSAGA_DEMO=1`, which also
  covers `worsaga-mcp`) serves a built-in fictional dataset — fake courses,
  deadlines, grades, forums, messages, calendar events, and locally generated
  fake PDFs — with no credentials, configuration, or network access. Try:
  `worsaga --demo summary ECON101 --week 3`.
- Public governance and policy docs: `CONTRIBUTING.md`, `SECURITY.md`,
  `COMMERCIAL.md`, `TRADEMARKS.md`, `THIRD_PARTY_NOTICES.md`, `ROADMAP.md`,
  and `RELEASE.md`.
- Generic public-release audit script (`scripts/audit_public_release.sh`)
  that scans tracked files and unpacked build artifacts.
- CI: test matrix (Python 3.10/3.12/3.14 plus macOS and Windows), ruff lint,
  secret scanning, version-consistency check, release audit, and packaging
  smoke tests.

### Removed

- The previous restrictive licence notice and closed positioning language.
- Private install instructions and local agent settings from the repository.

## 0.3.0 and earlier

Earlier versions were limited pre-release builds and are not supported.
