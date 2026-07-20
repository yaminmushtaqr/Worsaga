# AGENTS.md - worsaga

This repo is designed for agent use.

Worsaga is an open-source, local-first, read-only study toolkit for Moodle,
licensed under AGPL-3.0-only. Moodle is the only supported LMS provider today.

## Scope of this repository

This repo contains the CLI (`worsaga`), the MCP server (`worsaga.mcp_server`),
and the shared read-only Moodle client they use.

When working in here:

- Worsaga is AGPLv3 open source. Do not add paid, commercial, or
  hosted-service logic.
- Moodle is the only supported provider today. Do not add claims of support
  for other LMS providers.
- Keep all Moodle actions read-only (see the Safety section below).
- Keep credentials, tokens, private course data, downloaded materials, and
  local config out of git.
- Never commit planning, idea, feature-draft, or private-strategy documents,
  deny-lists, or anything under `notes-private/` — see `CLAUDE.md`. The only
  identifying content permitted in tracked files is the maintainer name and
  public repository URLs where deliberately chosen.
- External code contributions are not accepted at this stage; bug reports and
  security reports are welcome.
- Do not copy code from GPL, AGPL, MIT, or Apache projects unless the
  dependency and licence decision is explicit and reviewed.
- Do not reintroduce earlier brand names or add migration shims for them.

## Core rule: discovery first, download second

For Moodle materials, there are **two distinct steps**:

1. **Discovery**
   - CLI: `worsaga materials <course> --week <n>`
   - MCP: `get_week_materials(course_id=..., week=...)`
2. **Authenticated download**
   - CLI: `worsaga download <course> --week <n> --match ...` or `--index ...`
   - MCP: `download_material(course_id=..., week=..., match=..., index=...)`

Do **not** fetch raw `file_url` values directly.
- Raw `file_url` values are omitted from default discovery output (MCP
  `get_week_materials` never returns them; the CLI includes them only with
  `--include-file-urls`, for provenance/debugging).
- Normal retrieval should always go through `download` / `download_material`, which handle authentication safely.
- MCP `download_material` saves into Worsaga's own downloads directory;
  `output_dir` is a relative subdirectory inside it, never an arbitrary path.

## Recommended CLI flow

```bash
# 1. Discover available files
worsaga --json materials PSY110 --week 3

# 2. Download one specific file
worsaga --json download PSY110 --week 3 --match "Lec 3"

# or select by candidate index if multiple match
worsaga --json download PSY110 --week 3 --index 0
```

## Recommended MCP flow

```python
get_week_materials(course_id=42, week="3")
download_material(course_id=42, week="3", match="Lec 3")
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

For future indexing / search pipelines:
- iterate over `materials` results, then call `download` per file
- use `dedupe_key` to avoid repeat fetches
- add pacing/backoff to avoid Moodle rate limits
- choose a predictable output structure such as `<course>/<week>/`

## Safety

This package is read-only by design.
- Never bypass the package with direct write-capable Moodle calls.
- Never treat `file_url` as the public contract for downloads.
- Never commit credentials or tokens.
