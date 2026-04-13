# AGENTS.md - worsaga

This repo is designed for agent use.

## Core rule: discovery first, download second

For Moodle materials, there are **two distinct steps**:

1. **Discovery**
   - CLI: `worsaga materials <course> --week <n>`
   - MCP: `get_week_materials(course_id=..., week=...)`
2. **Authenticated download**
   - CLI: `worsaga download <course> --week <n> --match ...` or `--index ...`
   - MCP: `download_material(course_id=..., week=..., match=..., index=...)`

Do **not** fetch raw `file_url` values directly.
- `file_url` is kept in discovery output for provenance/debugging and internal plumbing.
- Normal retrieval should always go through `download` / `download_material`, which handle authentication safely.

## Recommended CLI flow

```bash
# 1. Discover available files
worsaga --json materials FM476 --week 3

# 2. Download one specific file
worsaga --json download FM476 --week 3 --match "Lec 3"

# or select by candidate index if multiple match
worsaga --json download FM476 --week 3 --index 0
```

## Recommended MCP flow

```python
get_week_materials(course_id=12098, week="3")
download_material(course_id=12098, week="3", match="Lec 3")
```

## Output expectations

`download` / `download_material` should return metadata like:
- `local_path`
- `file_name`
- `module_name`
- `section_name`
- `mime_type`
- `file_size`
- `bytes_written`
- optional `view_url`

They should **not** expose tokens or authenticated URLs.

## Smoke test recipe for agents

When verifying this workflow end-to-end:

1. Ensure credentials are available (`WORSAGA_CREDS_PATH` or normal config/env resolution).
2. Run `worsaga --version`.
3. Run `worsaga --json materials <course> --week <n>`.
4. Confirm the target file appears in the structured results.
5. Run `worsaga --json download <course> --week <n> --match ... --output <dir>`.
6. Confirm the returned `local_path` exists and that `bytes_written` matches the file size on disk.

## Bulk download guidance

For future indexing / wiki / QA pipelines:
- iterate over `materials` results, then call `download` per file
- use `dedupe_key` to avoid repeat fetches
- add pacing/backoff to avoid Moodle rate limits
- choose a predictable output structure such as `<course>/<week>/`

## Safety

This package is read-only by design.
- Never bypass the package with direct write-capable Moodle calls.
- Never treat `file_url` as the public contract for downloads.
- Never commit credentials or tokens.
