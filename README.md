# Worsaga

Worsaga is a study kit for university LMS systems.

This repository is the **open-source core** of Worsaga — the CLI and the MCP server — released under the Apache License 2.0 (see [LICENSE](LICENSE)). The wider Worsaga platform, app, and architecture are developed separately and are not part of this repo.

## Platform support

- Supported now: **Moodle**
- Coming soon: **Blackboard**, **Canvas**

Worsaga is intentionally positioned as a broader LMS study layer, not a Moodle-only product. Moodle just happens to be the first integration.

## What's in this repo

This repo contains only the open-core pieces:

- The `worsaga` command-line interface.
- The `worsaga.mcp_server` MCP server for agents.
- The read-only Moodle client and supporting Python modules they share.

The broader Worsaga product — hosted platform, end-user app, and overall system architecture — lives outside this repository and is not open source. Issues, PRs, and discussions here should stay scoped to the CLI and MCP core.

## Quick start

### Install

Install from PyPI with whichever tool you already have:

```bash
uv tool install worsaga      # recommended — adds worsaga to PATH automatically
pipx install worsaga         # alternative
pip install worsaga          # if you just want the library, or don't have uv/pipx
```

For local development from a checked-out repo:

```bash
pip install -e .
```

**Windows note:** If `worsaga` is not recognised after `pip install`, the Python
Scripts folder may not be on your PATH. Either:

1. Add it manually — find the path with:
   ```
   py -c "import site; print(site.getusersitepackages())"
   ```
   Then add that directory to your PATH in **Windows Settings > System > Environment Variables**.

2. Or use the module form directly (works immediately, no PATH changes needed):
   ```
   py -m worsaga.cli courses
   ```

### Set up credentials

```bash
worsaga setup
```

The setup command will prompt for your Moodle URL, API token, and user ID, then verify
the connection and save credentials. See [Getting a Moodle API token](#getting-a-moodle-api-token) below if you don't have a token yet.

Non-interactive setup (for scripts and agents):

```bash
worsaga setup --url https://moodle.example.ac.uk --token YOUR_TOKEN
```

## Getting a Moodle API token

### Via the Moodle web interface (recommended)

1. Log in to your Moodle site in a browser.
2. Click your profile picture (top right) → **Preferences**.
3. Under **User account**, click **Security keys**.
4. Find the **Moodle mobile web service** row.
5. Click **Reset** to generate a new token (or copy the existing one if shown).
6. Copy the token string — this is the long alphanumeric value you need for setup.

> **Tip:** Copy the token value itself (a long string of letters and numbers), not the key name like "Webservice-A3JsY" shown in the table.

### Via the token URL (advanced)

If the method above doesn't work for your institution, you can request a token directly:

```
https://<your-moodle>/login/token.php?username=YOUR_USER&password=YOUR_PASS&service=moodle_mobile_app
```

**Note:** Only use this over HTTPS. Never share your token.

## Configuration

Credentials are resolved in this order:

1. **Explicit arguments** passed to `MoodleConfig.load(url=..., token=...)`
2. **Environment variables**: `WORSAGA_URL`, `WORSAGA_TOKEN`, `WORSAGA_USERID`
3. **Config file** (first found):
   - `$WORSAGA_CREDS_PATH` (if set)
   - Platform-native config directory (see below)

The config directory is determined by `platformdirs` so it follows each OS's conventions: `~/.config/worsaga/` on Linux, `~/Library/Application Support/worsaga/` on macOS, and `%APPDATA%\worsaga\` on Windows. Run `worsaga config` to see the active path on your system, or `worsaga config --json` for machine-readable output.

### Config file format

```json
{
  "url": "https://moodle.example.ac.uk",
  "token": "your_token_here",
  "userid": 12345
}
```

### Environment variables

```bash
export WORSAGA_URL="https://moodle.example.ac.uk"
export WORSAGA_TOKEN="your_token_here"
export WORSAGA_USERID="12345"
```

## CLI usage

```
worsaga courses              # List enrolled courses
worsaga deadlines            # Show upcoming deadlines (14-day window)
worsaga deadlines --days 7   # Shorter look-ahead
worsaga contents 12345       # Show sections for course ID 12345
worsaga contents EC100       # Look up by course short-code
worsaga contents MG488       # Prefix match: finds MG488_2526 if unique
worsaga contents EC100 --week 3    # Filter to a specific week
worsaga materials EC100      # List all downloadable materials (discovery)
worsaga materials EC100 --week 3   # Materials for week 3 only
worsaga download EC100 --week 3 --match slides   # Download a file (authenticated)
worsaga download EC100 --week 3 --index 0        # Download by index
worsaga summary EC100 --week 3     # AI-generated study notes for a week
worsaga setup                # Guided first-time setup
worsaga update               # Show the safe upgrade command
```

Add `--json` before the command for machine-readable JSON output:

```
worsaga --json courses
worsaga --json deadlines
```

## MCP server

For use with Claude Code, OpenClaw, or other MCP-capable agents:

```bash
pip install "worsaga[mcp]"
python -m worsaga.mcp_server
```

The server runs as a stdio-based MCP server. Tools: `list_courses`, `get_deadlines`, `get_course_contents`, `get_week_materials` (discovery), `search_course_content`, `get_weekly_summary`, `download_material` (authenticated fetch).

**Claude Code**: Add to your Claude Code settings / MCP Servers config:
```json
{
  "mcpServers": {
    "worsaga": {
      "command": "python",
      "args": ["-m", "worsaga.mcp_server"]
    }
  }
}
```
Set `WORSAGA_URL`, `WORSAGA_TOKEN`, `WORSAGA_USERID` as environment variables or in `~/.config/worsaga/config.json`.

**OpenClaw**: Add to your agent or gateway config:
```yaml
mcpServers:
  moodle:
    command: python
    args: ["-m", "worsaga.mcp_server"]
    env:
      WORSAGA_URL: "https://your-moodle.example.ac.uk"
      WORSAGA_TOKEN: "your_token_here"
      WORSAGA_USERID: "12345"
```

## CI

Packaging and install smoke tests run automatically on every push and pull request across Linux, macOS, and Windows. The workflow builds both sdist and wheel, installs each in a clean virtual environment, and verifies the CLI entrypoints work.

## Development

To work on worsaga itself (contributors / developers):

```bash
git clone <repo-url> && cd worsaga
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
```

### Running tests

```bash
pytest

# On Windows, if pytest isn't found on PATH:
py -m pytest
```

## Safety

This package is **read-only by design**. Every Moodle API call is checked against a hardcoded allowlist in `ALLOWED_FUNCTIONS` inside the client module. Write-like operations — submitting assignments, posting forum replies, uploading files, creating events, deleting content — are blocked with a `SafetyError` before any network request is made.

- Your token cannot modify anything on Moodle/Canvas/Blackboard, even accidentally.
- Agents (Claude Code, OpenClaw, etc.) can use these tools safely.
- Treat your API token like a password: never commit it to git, never share it, use HTTPS only.

### Known limitations

- **Token portability varies by institution.** Some Moodle instances require specific web-service configurations. Check with your Moodle administrator about enabling REST web services.
- **Rate limiting.** Moodle servers may throttle rapid API calls. The package handles errors gracefully but cannot bypass institutional limits.
- **Only Moodle REST API v2 is supported.**
- **Course discovery by short-code uses prefix matching** (e.g. "MG488" matches "MG488_2526") — see CLI usage above.

## For Agent Authors

worsaga is designed to be agent-friendly:

- Zero configuration needed for discovery — call `list_courses()` with no arguments to explore.
- Self-documenting tool names and clear purpose descriptions.
- Structured output by default — use `--json` on CLI, or rely on structured dict/JSON from the Python API.
- Graceful degradation with clear error messages when credentials are missing.

### Discovery vs. download

There are two distinct steps — **discovery** and **download** — with separate commands for each:

| Purpose | CLI | MCP tool |
|---------|-----|----------|
| **List** available files (metadata only) | `worsaga materials` | `get_week_materials()` |
| **Download** a file (authenticated) | `worsaga download` | `download_material()` |

`materials` / `get_week_materials` return metadata including a `file_url` field. This URL is the raw Moodle address retained for **provenance only** — do not fetch it directly, as it requires token authentication that is handled internally by `download` / `download_material`.

### Downloading materials

The `download` command (CLI) and `download_material` tool (MCP) provide authenticated file downloads without exposing the Moodle token. The flow:

1. **Discover** materials: `worsaga materials EC100 --week 3`
2. **Download** one: `worsaga download EC100 --week 3 --match slides`

If multiple materials match, you get a structured candidate list with indices:

```bash
worsaga --json download EC100 --week 3
# → {"error": "3 materials match. Use --index ...", "candidates": [...]}

worsaga --json download EC100 --week 3 --index 0
# → {"local_path": "/path/to/week3_slides.pdf", "file_name": "week3_slides.pdf", ...}
```

The same workflow is available via MCP:

```
get_week_materials(course_id=42, week="3")          # discover
download_material(course_id=42, week="3", match="slides")  # fetch
```

Example integration in an agent system prompt:
```
You have access to worsaga. First run list_courses() to see available courses,
then get_deadlines() to check upcoming work, then explore specific courses with
get_course_contents(). To see what files are available, use get_week_materials(). To
fetch a file, use download_material() — it handles authentication internally. Never
fetch file_url values directly; always use download_material().
```

## License

This repo — the worsaga CLI and MCP server — is released under the [Apache License 2.0](LICENSE). The rest of the Worsaga platform (hosted service, end-user app, and overall system architecture) is developed separately and is not covered by this license.
