# Worsaga Roadmap

This roadmap tracks public work for Worsaga. Private planning, credentials, and
local scratch files must stay out of the repository.

## Current baseline

Version `0.6.0` includes:

- CLI and MCP server entrypoints.
- Read-only Moodle client with a hardcoded allowlist.
- Course discovery, deadlines, calendar events, contents, materials, downloads,
  weekly summaries, search, grades, assignments, forums, updates,
  notifications, inbox, digest, doctor, config, setup, and update commands.
- Structured JSON output.
- Optional YAML output.
- Token-safe authenticated downloads with conservative size, file-count, and
  page limits, structured download errors, and local-timezone display.
- Demo mode: the full CLI and MCP server without Moodle credentials, using
  clearly fake course data and generated fake PDFs, never contacting a real
  Moodle site.
- Per-page structured extraction (`worsaga extract`, `extract_material`)
  that preserves educational content (captions, objectives, references) by
  default.
- Unit tests with mocked Moodle payloads.

## New in 0.6.0: cache, sync, and changes

- Local SQLite cache in the platform-native user data directory.
- `worsaga sync` with metadata-only defaults.
- Change detection for new deadlines, new files, grade updates, and forum
  updates, with explicit per-category baselines.
- Tokens and authenticated URLs stay out of the cache.
- HTTPS-only Moodle URLs (localhost development excepted).

## New in 0.7.0 (unreleased): full-text search and study exports

- Local full-text index over course material text (SQLite FTS5), built
  in memory from authenticated fetches — `worsaga index`.
- `worsaga search-text`: offline, ranked full-text search with
  per-page snippets and course filtering.
- Markdown study-pack exports for a course week — `worsaga study-pack`.
- Tokens and authenticated URLs stay out of the index and the exports.

## New in 0.8.0 (unreleased): notifications and auto-sync

- Watch mode for local sync loops — `worsaga watch`.
- Local notification backends where practical (Windows toast, macOS
  `osascript`, Linux `notify-send`), best effort and dependency-free.
- `worsaga auto-sync install`, `status`, and `remove`, with `--dry-run`
  previews of every scheduler change.

## Later: optional AI workflows

- Optional local study assistant workflows over cached data.
- Deterministic, non-LLM summaries always remain available.
- External model use is explicit and configurable, never a default.

## Exploring: future LMS support

Moodle is the only supported provider today. Future LMS support is being
explored; no other provider is supported or promised yet.

## Non-goals

- Submitting assignments.
- Posting forum replies.
- Uploading files.
- Marking resources as viewed.
- Fetching raw Moodle `file_url` values directly.
- Storing tokens in cache or logs.
