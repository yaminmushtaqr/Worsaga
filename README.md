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

Non-interactive setup (for scripts) — the token is piped in, so it never
appears in your shell history or in the process list:

```bash
pass show moodle/token | worsaga setup --url https://moodle.example.ac.uk --token-stdin
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

1. **Explicit arguments** passed on the command line (`--url`, `--userid`, and
   the token via `--token-stdin`)
2. **Environment variables**: `WORSAGA_URL`, `WORSAGA_TOKEN`, `WORSAGA_USERID`
3. **Config file** (first found):
   - `$WORSAGA_CREDS_PATH` (if set)
   - Platform-native config directory (see below)

### How to supply the token

In order of preference:

1. **`worsaga setup`** — the interactive prompt. The token is never echoed and
   is saved to the config file below.
2. **A credentials file** — written by `setup`, or maintained yourself. It is
   created readable only by you (`0600` on Linux and macOS; on Windows it
   inherits your user profile's permissions) and is replaced atomically, so it
   always holds either the old contents or the new ones, never a mixture.
3. **`WORSAGA_TOKEN`** — the environment variable, for CI and containers.
4. **`--token-stdin`** — for scripts that hold the token in a password
   manager or secret store:

   ```bash
   pass show moodle/token | worsaga setup --url https://moodle.example.ac.uk --token-stdin
   pass show moodle/token | worsaga --token-stdin courses
   ```

`--token YOUR_TOKEN` still works but is **deprecated** and prints a warning:
command-line arguments are saved in your shell history and are visible to
every other process on the machine through the process list. If you have used
it, reset that token on your Moodle token page and switch to one of the
options above.

Worsaga's own helper processes (the auto-sync scheduler and desktop
notifications) run with `WORSAGA_TOKEN` removed from their environment. The
token is never written to the cache or the search index, it is stripped from
the `file_url` values in every result, and it is redacted from Moodle's own
error messages before they are shown. Redaction at *every* output boundary is
upcoming hardening rather than a guarantee today, so treat command output you
paste elsewhere with the same care you would give any log.

The Moodle URL must use `https://` — the API token is sent with every
request, so plain HTTP would expose it. `http://` is accepted only for a
Moodle running on this machine (`localhost` or a loopback IP address). It
must also be a plain site address: no user name or password in the URL, and
no query string or fragment.

`userid` is only a hint. Worsaga reads the authenticated user's real id from
the Moodle site itself and uses that for every request; if the configured
value disagrees, it warns and uses the site's answer.

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
worsaga extract ECON101 --week 3 --match slides   # Per-page text, nothing saved
worsaga summary ECON101 --week 3   # Study notes for a week (extractive)
worsaga search ECON101 regression  # Search course content by keyword
worsaga index                # Build the local full-text search index
worsaga search-text "price elasticity"  # Full-text search, no network
worsaga study-pack ECON101 --week 3     # Export a Markdown study pack
worsaga sync                 # Sync metadata to the local cache, report changes
worsaga changes --since 7d   # Show changes detected by previous syncs
worsaga watch --interval 15m # Foreground sync loop with notifications
worsaga auto-sync install    # Register a scheduled background sync
worsaga auto-sync status     # Is the background sync registered?
worsaga auto-sync remove     # Unregister it again
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

The server runs over stdio. **By default it offers 14 of its 26 tools** —
the ones that describe your own academic picture:

`list_courses`, `get_deadlines`, `get_grades`, `get_grade_summary`,
`get_assignments`, `get_assignment_status`, `get_calendar_events`,
`get_course_contents`, `get_week_materials` (discovery),
`search_course_content`, `search_text` (offline full-text search),
`get_changes` (replay what earlier syncs recorded, no network),
`get_autosync_status` (read-only scheduled-sync check), and
`get_connection_info` (read-only auth/site/user check).

The other 12 read other people's writing, fetch file *contents*, or write
to Worsaga's local stores, so they are behind a named capability and are
**not listed at all** until you enable them — an agent cannot see a tool
it has not been given:

| Capability | Tools |
|---|---|
| `forums` | `get_course_forums`, `get_forum_discussions`, `get_latest_updates` |
| `messages` | `get_messages` |
| `notifications` | `get_notifications` |
| `digest` | `get_digest` |
| `sync` | `sync_now` |
| `materials` | `download_material`, `extract_material`, `export_study_pack`, `get_weekly_summary` |
| `index` | `build_search_index` |

Enable them with `WORSAGA_MCP_CAPABILITIES`, comma-separated, or `all`:

```json
{
  "mcpServers": {
    "worsaga": {
      "command": "worsaga-mcp",
      "env": { "WORSAGA_MCP_CAPABILITIES": "materials,index,sync" }
    }
  }
}
```

The value is read once at start-up, and the server prints the active
profile to stderr so you can see what a session was given. An unknown
capability name is ignored with a warning rather than refusing to start.

Two things hold whatever the profile. Every tool clamps its numeric
arguments — day windows, result limits, file budgets — into a documented
range rather than trusting them. And every tool whose result can contain
text written by other people says so in its own description, so an agent
reading a forum post or an instructor's feedback is told to treat it as
material to report on, never as instructions to follow.

Every tool that takes a `course_id` accepts either the numeric id or a
course short-code — an exact match or an unambiguous prefix, e.g.
`get_grades("ECON101")` — so an agent need not call `list_courses` first.

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

The default profile then serves the built-in fictional dataset. The
walkthrough below reads a week's materials, so give the demo server the
`materials` capability as well:

```json
{
  "mcpServers": {
    "worsaga-demo": {
      "command": "worsaga-mcp",
      "env": {
        "WORSAGA_DEMO": "1",
        "WORSAGA_MCP_CAPABILITIES": "materials"
      }
    }
  }
}
```

Example prompt once connected:

> Summarise my study week: check my deadlines, then pull the week 3 summary
> for ECON101.

Here is that prompt running against the demo dataset in Claude Code
(all data shown is fictional):

![Worsaga MCP demo transcript: an agent lists upcoming deadlines and week 3
study notes from the fake dataset](docs/demo-mcp-transcript.png)

## Discovery, download, and extraction

There are three distinct steps — **discovery**, **download**, and
**extraction** — with separate commands for each:

| Purpose | CLI | MCP tool |
|---------|-----|----------|
| **List** available files (metadata only) | `worsaga materials` | `get_week_materials()` |
| **Download** a file (authenticated) | `worsaga download` | `download_material()` |
| **Extract** per-page text (in memory, nothing saved) | `worsaga extract` | `extract_material()` |

`materials` / `get_week_materials` return file metadata only. Raw Moodle
`file_url` values are omitted by default because they require token
authentication (the CLI can include them with `--include-file-urls` for
provenance). Downloads go through Worsaga's authenticated download path, which
never exposes your token, caps file size at 50 MB, and never leaves a
partially written file behind.

```bash
worsaga materials ECON101 --week 3               # discover
worsaga download ECON101 --week 3 --match slides --output downloads/
worsaga extract ECON101 --week 3 --match slides  # read, page by page
```

CLI downloads save to the current directory by default; pass `--output DIR`
to keep course files in a dedicated folder (recommended inside a git
checkout, so private course material never sits next to `git add`).

`extract` fetches the file into memory and returns structured per-page text
(per-slide for PPTX) with light Markdown rendering — captions, learning
objectives, and references are preserved by default (`--raw` skips cleaning).
Pages dominated by images are flagged rather than silently empty. Nothing is
written to disk.

If multiple materials match, you get a structured candidate list with indices
to pick from (`--index 0`).

## Sync and change detection

`worsaga sync` fetches metadata-only snapshots — upcoming deadlines, file
metadata, grades, and forum discussions; never file contents — into a local
SQLite cache and reports what changed since the last sync: new deadlines,
moved deadlines, new or updated files, grade updates, and new or updated
forum discussions. The first sync establishes a baseline and reports no
changes.

```bash
worsaga sync                     # sync and report changes
worsaga changes --since 7d       # replay recorded changes (no network)
worsaga changes --category grades
```

### What gets collected

Forum discussions are other people's writing, so a sync **nobody is
watching** leaves them alone: `worsaga watch`, the scheduled auto-sync, and
the MCP `sync_now()` collect deadlines, files, and grades. A foreground
`worsaga sync` you typed yourself still collects all four.

`--categories` overrides that in either direction, and
`WORSAGA_SYNC_CATEGORIES` sets a persistent default for both:

```bash
worsaga sync --categories deadlines,grades   # collect less
worsaga watch --categories all               # opt forums back in
export WORSAGA_SYNC_CATEGORIES=deadlines,files,grades
```

A category you did not select is reported as `(not selected)` and is *not*
treated as a failed one: it produces no change events, its cached rows and
its baseline are left exactly as they were, and it does not count towards
the run's `outcome`. Switching it back on later resumes from where it
stopped rather than re-reporting everything.

### What gets stored

Instructor feedback is the other piece of somebody else's writing a sync
would otherwise keep. The cache stores whether feedback exists
(`feedback_present`) and a truncated hash of it (`feedback_hash`) — enough
that editing feedback still shows up as a grade change — but not the words.
`worsaga grades` is unaffected: it reads the gradebook live, so nothing is
withheld from you. The table marks which items have feedback;
`worsaga grades --json` carries the full text of each one.

Pass `--store-feedback` (or set `WORSAGA_SYNC_STORE_FEEDBACK=1`) if you do
want the text cached. Even then it stays out of the recorded change events
that `worsaga changes` and the MCP `get_changes()` replay.

The first sync after upgrading to this behaviour reports the affected grade
items as `re-fingerprinted (storage format changed, not Moodle)` and emits
no change events for them — a storage change is not news about your course.

`worsaga sync` exits **1** when it could not fetch a single category, so a
script or a scheduled job can tell a working sync from one that silently
reached nothing; a partial sync prints its warnings and exits 0. With
`--json` the same verdict is in the `outcome` field (`success`, `partial`,
`failed`, or `skipped`).

The cache lives in the platform-native user data directory
(`WORSAGA_CACHE_PATH` overrides the location). Tokens and authenticated URLs
are never written to it, and cache rows are keyed by Moodle site, so demo-mode
data never mixes with real course data. The MCP equivalents are `sync_now()`
and `get_changes()`.

## Watch mode and auto-sync

`worsaga watch` runs the sync loop in your terminal: it re-syncs on an
interval, prints any detected changes, and raises a desktop notification
when something changed (Windows toast, macOS notification, or
`notify-send` on Linux — best effort, no extra dependencies). Stop it with
Ctrl+C.

```bash
worsaga watch                    # sync every 15 minutes until Ctrl+C
worsaga watch --interval 1h --no-notify
worsaga watch --categories all   # include forums in every cycle
```

Each cycle is an unattended sync, so it takes the narrower collection
default described above — deadlines, files, and grades — unless
`--categories` or `WORSAGA_SYNC_CATEGORIES` says otherwise.

If cycles start failing — the network is down, the site is unreachable —
the interval backs off: it doubles per consecutive failure, up to eight
intervals or one hour (whichever is smaller), with a little jitter, and
prints one `Backing off: next cycle in Xs (N consecutive failures).` line
to stderr. The next cycle that works returns it to the interval you asked
for. Repeated *authentication* failures stop unattended syncing entirely
until you run `worsaga sync` yourself and it succeeds; `worsaga auto-sync
status` says so when that has happened.

Only one sync per site runs at a time on a machine. If a `watch` loop, the
scheduled auto-sync, and a manual `worsaga sync` overlap, the later ones
say `Sync skipped: another Worsaga sync is already running for this site.`
and make no requests at all, rather than fetching every course twice.

For unattended syncs, `worsaga auto-sync` registers a periodic
`worsaga sync --quiet --unattended` with your platform's scheduler — Task
Scheduler on Windows, launchd on macOS, a systemd user timer on Linux:

```bash
worsaga auto-sync install --interval 30m --dry-run  # preview, changes nothing
worsaga auto-sync install --interval 30m            # register it
worsaga auto-sync status                            # read-only check
worsaga auto-sync remove                            # unregister cleanly
```

`--dry-run` prints the exact commands and files an install or remove would
touch. Install re-checks the scheduler afterwards and reports whether the
registration was verified; `status` is strictly read-only and also shows
the cache's most recent sync time — manual or scheduled, since Worsaga
does not record which trigger ran a sync — so you can see whether data has
moved recently. Removal refuses to delete anything while the scheduled job
is still active, or when the scheduler cannot be queried at all; if you need
to clear stale local state anyway, `worsaga auto-sync remove --force-local`
deletes only Worsaga's own files, never touches the scheduler, and tells you
what to remove manually. The
scheduled command line never contains credentials — the background sync
loads configuration the same way a manual run does. Check what it found
later with `worsaga changes`.

## Full-text search and study packs

`worsaga index` builds a local full-text index over your course materials:
supported files (PDF, PPTX, DOCX, TXT) are fetched in memory, their text is
extracted page by page, and only that text is stored — in a SQLite FTS5
database in the platform-native user data directory (`WORSAGA_INDEX_PATH`
overrides the location). Files unchanged since the last run are skipped, so
re-running is cheap; a per-run file budget makes large sites indexable in
resumable steps; and full-course runs remove index entries for files that
were deleted or renamed on Moodle (never on failed fetches, and never from
week-scoped runs).

```bash
worsaga index                          # index all courses
worsaga index ECON101 --week 3         # index one week of one course
worsaga search-text "price elasticity" # search — offline, ranked, snippets
worsaga search-text tax --course ECON101 --limit 5
```

`worsaga study-pack` exports a single Markdown document for a teaching week:
deterministic study notes, a materials overview, and the full extracted
per-page content of the section's supported files (up to eight; a larger
section is included in listed order with an explicit warning).

```bash
worsaga study-pack ECON101 --week 3            # writes ECON101-week-3-study-pack.md
worsaga study-pack ECON101 --week 3 --output packs/
worsaga study-pack ECON101 --week 3 --stdout   # print instead of writing
```

Like every other surface, both features keep tokens and authenticated URLs
out of storage and output, and both work in demo mode. The MCP equivalents
are `build_search_index()`, `search_text()`, and `export_study_pack()`.

## Safety and privacy

Worsaga uses allowlisted read-only Moodle web-service calls. Every API call is
checked against a hardcoded allowlist in the client; write-like operations —
submitting assignments, posting replies, uploading files, creating events,
deleting content — are blocked before any network request is made. Each
allowlisted function also carries the exact set of request parameters Worsaga
may send, so a call cannot be widened beyond what the feature needs.

- Worsaga only ever reads the authenticated user's own data. The user id
  comes from the Moodle site, not from configuration, and course-scoped
  reads are confined to the courses that user is enrolled in — both checked
  before any request goes out.
- Credentials stay local. There is no hosted service and no telemetry.
- Downloads go through Worsaga's authenticated download path.
- Treat your API token like a password: never commit it, never share it, use
  HTTPS only.
- Respect your institution's acceptable-use policy for web-service access.

### Other people's content

Some of what Moodle will hand you was written by somebody else: forum
posts, private messages, notifications, and the feedback an instructor
wrote on your work. Worsaga treats that as a distinct category.

- **It is not collected in the background by default.** See
  [What gets collected](#what-gets-collected) — an unattended sync leaves
  forums alone, and instructor feedback is stored as a hash rather than as
  text.
- **It is not offered to agents by default.** The MCP tools that read it
  are behind a capability and are absent from the tool list until enabled.
- **The first time a run reads it against a real site, Worsaga says so
  once** on stderr: that a local copy is being kept, that it is your
  personal study material, and that it should be treated as text to read
  rather than instructions to act on. The notice is recorded per site (in
  the state directory) so it appears once, never in demo mode, and never
  under `-q`.
- Allowlist entries whose responses carry it are marked `third_party` in
  `worsaga.client.ALLOWED_FUNCTION_POLICIES`, so the boundary is checked by
  the test suite rather than by memory.

### Token hygiene at the output boundary

The Moodle token is redacted from everything Worsaga prints or returns. The
CLI wraps its own stdout and stderr, and every MCP tool result (and any
exception message) passes through the same filter. Two rules apply: the
configured token in any of its encoded spellings, and any `token`-like
query parameter whatever its value — which is what catches the credentials
Moodle mints into the links it hands back, such as a notification's
`contexturl` or a calendar event URL.

Worsaga is read-only, but read-only LMS data can still be sensitive. Course
materials, grades, messages, notifications, and Moodle URLs may contain private
information. Only connect Worsaga to agent systems you trust, and do not paste
`worsaga --json` output publicly if it includes course, grade, message, or
material data.

### Known limitations

- **Token availability varies by institution.** Some Moodle instances restrict
  web-service tokens. Check with your Moodle administrator about REST web
  services.
- **Rate limiting.** Worsaga paces itself: never more than 2 requests in
  flight per site, at least 250 ms between request starts, and when a site
  answers `429` or `503` it honours the `Retry-After` wait (or backs off
  exponentially when there is none), across every process on the machine.
  It cannot bypass an institutional limit and does not try to — when the
  retries run out it stops with `the site is rate-limiting requests; try
  again later.` The two knobs move one way only:
  `WORSAGA_MIN_REQUEST_GAP_MS` can raise the gap and `WORSAGA_MAX_IN_FLIGHT`
  can lower the concurrency; there is no way to turn the limiter off.
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
