# Worsaga

Worsaga is an open-source, local-first study toolkit for Moodle and university
LMS workflows.

It provides a CLI and MCP server for safely reading course data such as courses,
deadlines, grades, assignments, forums, messages, calendar events, course
materials, and extractive weekly summaries.

Worsaga is read-only by design. It does not submit assignments, post messages,
upload files, mark items as read, or mutate Moodle state.

## Status

Worsaga currently supports Moodle. Other LMS providers are not supported yet.

## Install

```bash
pipx install "worsaga[mcp]"
worsaga --version
```

For local development:

```shell
git clone https://github.com/yaminmushtaqr/worsaga.git
cd worsaga
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,mcp,yaml]"
pytest
```

**Windows note:** if `worsaga` is not recognised after a plain `pip install`,
use the module form `py -m worsaga.cli`, or install with `pipx` which handles
PATH setup for you.

## Setup

```bash
worsaga setup
```

The guided setup prompts for your Moodle site URL, API token, and user ID,
verifies the connection, and saves credentials locally. Then check your
connection and start exploring:

```bash
worsaga doctor
worsaga courses
worsaga summary <course> --week <n>
```

Non-interactive setup (for scripts):

```bash
worsaga setup --url https://moodle.example.ac.uk --token YOUR_TOKEN
```

### Getting a Moodle API token

1. Open your institution's Moodle token page while signed in:

   ```text
   https://<your-moodle-site>/user/managetoken.php
   ```

2. Find the **Moodle mobile web service** row.
3. Click **Reset** to generate a new token (or copy the existing one if shown).
4. Copy the token string — the long alphanumeric value, not the key name.

If the direct token page does not work for your institution: log in to Moodle,
click your profile picture → **Preferences** → **Security keys**, and copy the
**Moodle mobile web service** token from there.

Treat your Moodle token like a password. Never share it, never commit it, and
only use it over HTTPS.

## Demo mode (no Moodle account needed)

You can try the full CLI and MCP server without credentials, configuration, or
network access. Demo mode serves a built-in fictional dataset — fake courses,
deadlines, forums, and locally generated PDFs that are clearly marked as fake:

```bash
worsaga --demo courses
worsaga --demo deadlines
worsaga --demo materials ECON101 --week 3
worsaga --demo summary ECON101 --week 3
```

Every command works with `--demo`, including `--json` output. Alternatively set
the `WORSAGA_DEMO=1` environment variable, which also puts the MCP server into
demo mode (see below). Demo mode never contacts any Moodle site.

## Configuration

Credentials are resolved in this order:

1. **Explicit arguments** passed on the command line (`--url`, `--token`, `--userid`)
2. **Environment variables**: `WORSAGA_URL`, `WORSAGA_TOKEN`, `WORSAGA_USERID`
3. **Config file** (first found):
   - `$WORSAGA_CREDS_PATH` (if set)
   - Platform-native config directory (see below)

The config directory follows each OS's conventions via `platformdirs`:
`~/.config/worsaga/` on Linux, `~/Library/Application Support/worsaga/` on
macOS, and `%APPDATA%\worsaga\` on Windows. Run `worsaga config` to see the
active path, or `worsaga config --json` for machine-readable output.

```json
{
  "url": "https://moodle.example.ac.uk",
  "token": "your_token_here",
  "userid": 12345
}
```

## CLI usage

```
worsaga courses              # List enrolled courses
worsaga deadlines            # Show upcoming deadlines (14-day window)
worsaga deadlines --days 7   # Shorter look-ahead
worsaga grades               # Show grade items across enrolled courses
worsaga grades ECON101 --missing    # Missing/unreleased grade items
worsaga assignments          # Show assignment statuses
worsaga assignments ECON101 --due-soon --status not_submitted
worsaga forums ECON101       # Show course forums
worsaga forum latest ECON101 # Latest discussions for a course
worsaga updates --since 7d   # Recent forum updates
worsaga notifications        # Moodle popup notifications
worsaga inbox                # Moodle messages
worsaga digest --since 24h   # Live digest with warnings for partial failures
worsaga calendar --days 30   # Calendar events
worsaga calendar ECON101 --week 3   # Calendar events for a teaching week
worsaga contents ECON101     # Show sections for a course
worsaga contents ECON101 --week 3  # Filter to a specific week
worsaga materials ECON101    # List downloadable materials (discovery)
worsaga materials ECON101 --week 3 # Materials for week 3 only
worsaga download ECON101 --week 3 --match slides  # Download a file (authenticated)
worsaga download ECON101 --week 3 --index 0       # Download by index
worsaga summary ECON101 --week 3   # Study notes for a week (extractive)
worsaga search ECON101 regression  # Search course content by keyword
worsaga doctor               # Check auth and connectivity
worsaga setup                # Guided first-time setup
worsaga update               # Show the safe upgrade command
```

Course arguments accept a Moodle course ID or a short-code; short-codes use
prefix matching (e.g. `ECON101` matches `ECON101_2526` when unique).

Add `--json` before the command for machine-readable JSON output:

```
worsaga --json courses
worsaga --json deadlines
```

YAML output is optional:

```
pip install "worsaga[yaml]"
worsaga --yaml courses
```

## MCP server

For use with Claude Code or any MCP-capable agent, install with the `mcp`
extra, then run:

```bash
worsaga-mcp
```

The server runs over stdio. Tools: `list_courses`, `get_deadlines`,
`get_grades`, `get_grade_summary`, `get_assignments`, `get_assignment_status`,
`get_course_forums`, `get_forum_discussions`, `get_latest_updates`,
`get_notifications`, `get_messages`, `get_digest`, `get_calendar_events`,
`get_course_contents`, `get_week_materials` (discovery),
`search_course_content`, `get_weekly_summary`, `download_material`
(authenticated fetch).

Minimal MCP configuration:

```json
{
  "mcpServers": {
    "worsaga": {
      "command": "worsaga-mcp",
      "args": []
    }
  }
}
```

Set `WORSAGA_URL`, `WORSAGA_TOKEN`, and `WORSAGA_USERID` as environment
variables, or configure credentials once with `worsaga setup`.

To try the MCP server without Moodle credentials, use demo mode instead:

```json
{
  "mcpServers": {
    "worsaga-demo": {
      "command": "worsaga-mcp",
      "args": [],
      "env": { "WORSAGA_DEMO": "1" }
    }
  }
}
```

All tools then serve the built-in fictional dataset — ask your agent to
"summarise my study week" to see it in action.

Example prompt once connected:

> Summarise my study week: check my deadlines, then pull the week 3 summary
> for ECON101.

Here is that prompt running against the demo dataset in Claude Code
(all data shown is fictional):

![Worsaga MCP demo transcript: an agent lists upcoming deadlines and week 3
study notes from the fake dataset](docs/demo-mcp-transcript.png)

## Discovery vs. download

There are two distinct steps — **discovery** and **download** — with separate
commands for each:

| Purpose | CLI | MCP tool |
|---------|-----|----------|
| **List** available files (metadata only) | `worsaga materials` | `get_week_materials()` |
| **Download** a file (authenticated) | `worsaga download` | `download_material()` |

`materials` / `get_week_materials` return file metadata only. Raw Moodle
`file_url` values are omitted by default because they require token
authentication (the CLI can include them with `--include-file-urls` for
provenance). Downloads go through Worsaga's authenticated download path, which
never exposes your token, caps file size at 50 MB, and never leaves a
partially written file behind.

```bash
worsaga materials ECON101 --week 3               # discover
worsaga download ECON101 --week 3 --match slides # fetch
```

If multiple materials match, you get a structured candidate list with indices
to pick from (`--index 0`).

## Safety and privacy

Worsaga uses allowlisted read-only Moodle web-service calls. Every API call is
checked against a hardcoded allowlist in the client; write-like operations —
submitting assignments, posting replies, uploading files, creating events,
deleting content — are blocked before any network request is made.

- Credentials stay local. There is no hosted service and no telemetry.
- Downloads go through Worsaga's authenticated download path.
- Treat your API token like a password: never commit it, never share it, use
  HTTPS only.
- Respect your institution's acceptable-use policy for web-service access.

Worsaga is read-only, but read-only LMS data can still be sensitive. Course
materials, grades, messages, notifications, and Moodle URLs may contain private
information. Only connect Worsaga to agent systems you trust, and do not paste
`worsaga --json` output publicly if it includes course, grade, message, or
material data.

### Known limitations

- **Token availability varies by institution.** Some Moodle instances restrict
  web-service tokens. Check with your Moodle administrator about REST web
  services.
- **Rate limiting.** Moodle servers may throttle rapid API calls. Worsaga
  handles errors gracefully but cannot bypass institutional limits.
- **Only the Moodle REST API is supported.**

## Contributing

Bug reports, reproducible issues, documentation corrections, and security
reports are welcome through GitHub Issues. Worsaga is not accepting unsolicited
feature pull requests at this stage — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

Worsaga is open-source software licensed under the GNU Affero General Public
License v3.0. See [LICENSE](LICENSE).

The Worsaga name, logo, and related branding are not licensed under the AGPL.
See [TRADEMARKS.md](TRADEMARKS.md).

Worsaga is currently a personal, non-commercial open-source developer project.
There is no paid offering, hosted service, support contract, subscription,
advertising, sponsorship, or commercial licence at this time. See
[COMMERCIAL.md](COMMERCIAL.md).
