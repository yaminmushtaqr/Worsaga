# Changelog

All notable changes to Worsaga are documented in this file.

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
