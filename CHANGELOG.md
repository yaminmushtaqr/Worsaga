# Changelog

All notable changes to Worsaga are documented in this file.

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
