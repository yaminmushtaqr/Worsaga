# Worsaga

Worsaga is an open-source, local-first study toolkit for Moodle and university
LMS workflows.

It provides a CLI and MCP server for safely reading course data such as courses,
deadlines, grades, assignments, forums, messages, calendar events, course
materials, and extractive weekly summaries.

Worsaga is read-only **against Moodle**: it does not submit assignments, post
messages, upload files, mark items as read, or mutate Moodle state in any
way. It does write on your own machine — configuration, a cache, a search
index, downloads, and study packs. Both halves are set out in
[Responsible use](#responsible-use) and [What Worsaga stores](#what-worsaga-stores).

## Status

Worsaga currently supports Moodle. Other LMS providers are not supported yet.

Worsaga is an independent open-source project and is not affiliated with or
endorsed by Moodle HQ or Moodle Pty Ltd.

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

### Worsaga never asks for your password

Worsaga never asks for, accepts, or transmits your university password.
There is no field for one, no prompt for one, and no code path that would
know what to do with one. It authenticates with a **web-service token** that
Moodle's own interface issues to you (Preferences → Security keys), which you
paste in locally.

That token is equivalent to your Moodle access, so treat it as you would the
password itself: keep it on your machine, and never paste it into a hosted
service, a shared configuration file, a pastebin, or a chat window. Worsaga's
MCP server is a local process on your own machine speaking over stdio — not a
hosted service, and not something that sends your token anywhere except to
your own Moodle site.

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
macOS, and `%LOCALAPPDATA%\worsaga\worsaga\` on Windows — that is
`AppData\Local`, not `AppData\Roaming`, so the token is not swept into a
roaming profile that follows you onto other machines. Run `worsaga config`
to see the paths that are actually in use on your machine, or
`worsaga config --json` for machine-readable output. Every local store is
listed in [What Worsaga stores](#what-worsaga-stores).

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

### Where MCP results end up

Everything a tool returns flows into whichever agent or host you connected
the server to — course contents, deadlines, grades, and, where you enable
those capabilities, messages and forum posts other people wrote. That host
decides what happens next: it may keep the results in a conversation
history, send them to a model provider, sync them to a server, or include
them in logs. Worsaga has no say in any of it once the result leaves the
tool.

So connect only hosts you trust with your academic record, and treat the
capability profile as the real boundary: it is what bounds the questions a
host is able to ask in the first place, since a withheld tool is absent from
the list rather than present and refusing. The default profile — the
[capability table](#mcp-server) above — keeps other people's writing and
file contents out of reach until you deliberately enable them.

Worsaga's supported deployment is described in
[One machine, one user](#one-machine-one-user).

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
worsaga download ECON101 --week 3 --match slides # saves to the downloads dir
worsaga download ECON101 --week 3 --match slides --output ~/coursework/econ
worsaga extract ECON101 --week 3 --match slides  # read, page by page
```

Downloads go to **Worsaga's own downloads directory** unless you say
otherwise — `worsaga config` prints the path, and the command itself states
the full destination of what it wrote. `--output DIR` chooses somewhere else,
and `--output .` writes to the current directory (which is what earlier
versions did by default).

Prefer a dedicated folder **outside any git repository**. Course materials
are usually somebody else's copyrighted work, and a repository is a place
whose purpose is to publish what is in it — one `git add -A` is all it takes.
Whenever the destination turns out to be inside a git working tree, `download`
and `study-pack` say so on stderr and carry on; it is a warning, not a
refusal, because there are good reasons to keep coursework under version
control and Worsaga cannot know whether yours is one of them.

`WORSAGA_DOWNLOADS_DIR` moves the default permanently. It must be an
absolute path: the CLI and the MCP server run with unrelated working
directories, so a relative value would name two different places. A
relative one is reported and ignored rather than guessed at.

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
worsaga watch --notify-details   # put change titles in the notification
```

Notifications are deliberately uninformative. A desktop notification is
drawn by the operating system over whatever is on screen at the time — a
shared display, a projector, a screen recording, a lock screen — so the
default body is counts and course short-codes only:

```text
Worsaga: 3 changes
2 in ECON101, 1 in CS210
```

`--notify-details` restores the item titles for someone who has decided
their screen is private. Being precise about what that costs:

- your Moodle token is stripped at the notification boundary itself, so it
  cannot reach a toast however it got into the text;
- grade values, instructor feedback, and file contents are never part of
  what Worsaga puts in a notification in either mode, and no flag turns
  them on;
- detailed mode shows item titles exactly as their authors wrote them. A
  discussion title is free text somebody typed, so it may contain a URL or
  anything else. That is precisely why counts-only is the default and
  titles are something you switch on deliberately.

Each cycle is an unattended sync, so it takes the narrower collection
default described above — deadlines, files, and grades — unless
`--categories` or `WORSAGA_SYNC_CATEGORIES` says otherwise.

If cycles start failing — the network is down, the site is unreachable —
the interval backs off: it doubles per consecutive failure, up to eight
intervals or one hour (whichever is smaller), with a little jitter, and
prints one `Backing off: next cycle in Xs (N consecutive failures).` line
to stderr. The next cycle that works returns it to the interval you asked
for. A sync in which the site *rejected your credentials* stops unattended
syncing entirely — one rejection is almost always a revoked or expired
token, and retrying it unattended is load with no prospect of success — so
it stays paused until you run `worsaga sync` yourself and it succeeds;
`worsaga auto-sync status` says so when that has happened.

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
worsaga study-pack ECON101 --week 3            # writes to the downloads dir
worsaga study-pack ECON101 --week 3 --output ~/coursework/econ
worsaga study-pack ECON101 --week 3 --stdout   # print instead of writing
```

Packs land in the same downloads directory as `download`, with the same
`--output` and the same git-repository warning.

Every pack opens with a provenance header — the Worsaga version that built
it, the site, course, and week it came from, the UTC generation time, and a
line saying it is personal study material whose sources keep their original
rights and should not be redistributed. A pack reproduces a week's teaching
material verbatim and is exactly the kind of file that gets copied between
machines and forwarded to a friend, so what it is travels with it.

Like every other surface, both features keep tokens and authenticated URLs
out of storage and output, and both work in demo mode. The MCP equivalents
are `build_search_index()`, `search_text()`, and `export_study_pack()`.

## Responsible use

Worsaga reads a Moodle site through **Moodle's External Services framework**
— the standard REST web-service interface, the same framework Moodle uses
for its official mobile app. Nothing is scraped, no HTML is parsed, no
private interface is used, and no part of Moodle is accessed in a way its
operators have not switched on. Access is by a token their own installation
issued to you.

### Read-only, on two axes

**Against Moodle, Worsaga cannot write.** Every API call is checked against
a hardcoded allowlist in the client; write-like operations — submitting
assignments, posting replies, uploading files, creating events, marking
items read, deleting content — are blocked before any network request is
made. Each allowlisted function also carries the exact set of request
parameters Worsaga may send, so a call cannot be widened beyond what the
feature needs. There is no configuration, flag, or environment variable that
adds a write function to the list.

**Locally, Worsaga does write.** It is a study tool, not a viewer: it saves
your configuration and token, a sync cache, a full-text search index,
downloaded files and study packs, small operational-state records, and — if
you ask for it — a scheduled task registered with your operating system's
scheduler. All of it is on your own machine, listed file by file in
[What Worsaga stores](#what-worsaga-stores), and none of it leaves. There is
no hosted service and no telemetry.

### Your own account, at a considerate rate

Worsaga only ever reads the authenticated user's own data. The user id comes
from the Moodle site itself rather than from configuration, and
course-scoped reads are confined to the courses that user is enrolled in —
both checked before a request goes out.

It also paces itself, on every surface. Each Worsaga process applies:

- never more than **2 requests in flight** per site;
- request starts paced at least 250 ms apart;
- exponential backoff with jitter on a refusal that carries no
  `Retry-After`;
- a bounded retry budget per site.

And, machine-wide across every Worsaga process:

- a cooldown the **site itself** asked for — `Retry-After` on a `429` or
  `503` — is written down and honoured by every other Worsaga process, so
  a `watch` loop and a sync you start by hand back off together instead of
  each discovering the limit separately;
- a sync in which the site rejected the credentials **stops** unattended
  syncing rather than retrying forever;
- every request carries an identifying `User-Agent` naming Worsaga, its
  version, and its repository.

The two knobs move one way only: `WORSAGA_MIN_REQUEST_GAP_MS` can raise the
gap and `WORSAGA_MAX_IN_FLIGHT` can lower the concurrency. There is no way
to turn the limiter off.

### Authorisation is yours to establish

Worsaga cannot determine whether you are legally or contractually authorised
to use any particular site, and it does not try to. That judgement is yours:

- use **your own account**, on sites you are entitled to use;
- read your institution's acceptable-use and IT policies, and follow them —
  they, not this README, govern what you may do with your Moodle access;
- never use a staff, shared, service, or delegated token to reach data
  outside your own role. A token that can see other people's work is not
  something Worsaga is built for, and using one here would put their data on
  your disk.

If you are unsure whether web-service access is permitted for you, ask your
institution's IT service or your Moodle administrator before setting Worsaga
up.

### Course materials are somebody else's work

Lecture slides, recordings, problem sets, readings, and the posts other
people wrote in a forum are typically copyrighted, usually by your
institution or its staff, sometimes by a publisher. Downloads, study packs,
extracted text, cached metadata, and search results are for **your own
personal study**.

Do not redistribute them: not to a classmate who missed the lecture, not to
a course-notes marketplace, not to a public repository, not to a file-sharing
site or a Discord server. Study packs carry a line saying so at the top of
every file for exactly this reason. Keeping downloads out of a git repository
(see [Discovery, download, and extraction](#discovery-download-and-extraction))
is part of the same care.

### If your institution has not enabled web services

Some institutions switch Moodle's web services off. That is their decision to
make. When Worsaga meets such a site it says so plainly — *"This Moodle site
has not enabled web-service access. That is the institution's decision, and
Worsaga cannot be used with it."* — and stops, including stopping any
unattended syncing, since the answer will not change by asking again.

Worsaga documents no way around that decision and supports none. If you think
the service should be available to you, ask your Moodle administrator; there
is a section for them [below](#for-moodle-administrators).

### One machine, one user

The supported deployment is **a single person running Worsaga on their own
machine**, reading their own account, with the MCP server as a local stdio
process belonging to that same person.

Running it as a shared, multi-user, remote, or hosted service is explicitly
unsupported. Nothing in Worsaga separates users, authorises requests, or
scopes a store to a caller: the credentials are one person's, the cache and
index are one person's, and the token in the configuration file is available
to anything running as that OS user. A deployment that puts it behind a
network endpoint is one where somebody else's agent reads somebody's
academic record.

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
- **The first time a run reads other people's *writing* against a real
  site, Worsaga says so once** on stderr: that a local copy is being kept,
  that it is your personal study material, and that it should be treated
  as text to read rather than instructions to act on. It fires on the
  surfaces that fetch that writing wholesale — forum posts, private
  messages, and notifications, including when the digest pulls them in or
  a sync has forums switched on. It deliberately does *not* fire on
  grades: instructor feedback is
  handled by keeping less of it (above) rather than by a warning. The
  notice is recorded per site (in the state directory) so it appears once,
  never in demo mode, and never under `-q`.
- Allowlist entries whose responses carry other people's personal content
  are marked `others_personal` in
  `worsaga.client.ALLOWED_FUNCTION_POLICIES` and exported as
  `worsaga.OTHERS_PERSONAL_FUNCTIONS`, so the boundary is checked by the
  test suite rather than by memory. The MCP layer has a separate and
  deliberately broader label, `third_party`, on every tool whose result
  could carry text somebody else wrote — including section summaries,
  event descriptions, and extracted file text — because that one answers
  "could this be a prompt-injection payload?" rather than "is this
  personal data?".

### Token hygiene at the output boundary

The Moodle token is redacted from everything Worsaga prints or returns. The
CLI wraps its own stdout and stderr, and every MCP tool result (and any
exception message) passes through the same filter. Two rules apply: the
configured token in any of its encoded spellings, and any `token`-like
query parameter whatever its value — which is what catches the credentials
Moodle mints into the links it hands back, such as a notification's
`contexturl` or a calendar event URL.

Reading changes nothing on Moodle, but read-only LMS data is still sensitive.
Course materials, grades, messages, notifications, and Moodle URLs may all
contain private information. Do not paste `worsaga --json` output publicly if
it includes course, grade, message, or material data, and connect only agent
hosts you trust — see [Where MCP results end up](#where-mcp-results-end-up).

### Known limitations

- **Token availability varies by institution.** Some Moodle instances
  restrict web-service tokens, and some do not offer web services at all —
  see [If your institution has not enabled web
  services](#if-your-institution-has-not-enabled-web-services). Ask your
  Moodle administrator about REST web services.
- **Rate limiting.** Worsaga cannot bypass an institutional rate limit and
  does not try to. When its retries run out it stops with `the site is
  rate-limiting requests; try again later.` The pacing it applies of its own
  accord is described in [Your own account, at a considerate
  rate](#your-own-account-at-a-considerate-rate).
- **Only the Moodle REST API is supported.**

## What Worsaga stores

Everything Worsaga keeps is a file on your own machine. Nothing is uploaded,
and there is no account, server, or telemetry anywhere in the project.

**Run `worsaga config`** (or `worsaga config --json`) to see where the
config file, downloads directory, cache, search index, and state directory
actually resolve to on your system, including any relocation you have set.
The table below describes the defaults, using `DATA` for the
platform-native data directory and `CONFIG` for the config directory:

| Platform | `CONFIG` and `DATA` |
|---|---|
| Linux | `~/.config/worsaga/` and `~/.local/share/worsaga/` |
| macOS | `~/Library/Application Support/worsaga/` (both) |
| Windows | `%LOCALAPPDATA%\worsaga\worsaga\` (both) |

| What | Where | What it contains | Default state | How to delete it |
|---|---|---|---|---|
| Credentials | `CONFIG/config.json` | Moodle URL, **web-service token**, user id | Plaintext JSON, owner-only where the OS supports it | Delete the file; also reset the token in Moodle |
| Sync cache | `DATA/cache.db` (`WORSAGA_CACHE_PATH`) | Deadlines, file metadata, grades, forum discussion metadata, recorded change events | Forums excluded from unattended syncs; instructor feedback stored as presence + hash, never text, unless `--store-feedback` | Delete the file |
| Search index | `DATA/search.db` (`WORSAGA_INDEX_PATH`) | Extracted page text of your course materials (SQLite FTS5) | Built only when you run `worsaga index` | Delete the file |
| Downloads | `DATA/downloads/` (`WORSAGA_DOWNLOADS_DIR`) | Course files you downloaded | Created on first download | Delete the directory |
| Study packs | `DATA/downloads/` unless `--output` says otherwise | A week's extracted material as Markdown | Written only when you ask for one | Delete the files |
| Auto-sync record | `DATA/autosync.json` | The scheduled command line and install metadata — never credentials | Only after `worsaga auto-sync install` | `worsaga auto-sync remove` |
| Auto-sync registration | Task Scheduler (Windows), `~/Library/LaunchAgents/*.plist` (macOS), `~/.config/systemd/user/worsaga-autosync.{service,timer}` (Linux) | A command line that loads credentials the same way a manual run does | Only after `worsaga auto-sync install` | `worsaga auto-sync remove` |
| First-use notice record | `DATA/notices.json` (`WORSAGA_STATE_DIR`) | Which sites have already seen the third-party-content notice | Written the first time a real site's forums, messages, or notifications are read | Delete the file |
| Sync outcome and circuit state | `DATA/syncstate.json` | Per-site outcome, failure counts, failure class, circuit-breaker state | Written after each sync | Delete the file |
| Rate-limit cooldown | `DATA/backpressure.json` | Per-origin backoff deadlines shared between processes | Written when a site asks for fewer requests | Delete the file |
| Lock files | `DATA/cache.db.sync-*.lock`, `DATA/syncstate.lock`, `DATA/notices.lock` | Ownership tokens and liveness stamps; no course data | Held only while a sync or a state write runs | Delete when nothing is running |

Worsaga writes **no log files of its own**. A scheduled sync's output is
whatever your platform's scheduler captures — the systemd journal, macOS's
unified log, Task Scheduler's history — and is deleted through that platform,
not through Worsaga.

Deleting all of it is deleting `CONFIG/` and `DATA/`, plus any `--output`
directories you chose, and running `worsaga auto-sync remove` first if a
scheduled sync is registered. A single command that does all of it is
something the project wants to add; it does not exist yet, so today this is
a manual job.

**Check for relocations before you assume you are done.** Any of these
moves a store out of the directories above, and a store you moved is a
store `rm -rf ~/.local/share/worsaga` will not touch:

| Override | Moves |
|---|---|
| `WORSAGA_CREDS_PATH`, or `--creds-path` | the credentials file, token and all |
| `WORSAGA_CACHE_PATH` | the sync cache |
| `WORSAGA_INDEX_PATH` | the search index |
| `WORSAGA_STATE_DIR` | the notices, sync-state, and backpressure records |
| `WORSAGA_DOWNLOADS_DIR` | downloads and study packs |

Every override that names a store's location must be an **absolute** path
(`WORSAGA_CACHE_PATH`, `WORSAGA_INDEX_PATH`, `WORSAGA_STATE_DIR`,
`WORSAGA_DOWNLOADS_DIR`); a relative value is refused with a warning on
stderr and the default location is used instead. The CLI, the MCP server,
and a scheduled sync run from unrelated working directories, so a relative
value would give each of them a different file — and the cooldown, the
circuit state, and the sync locks are only machine-wide while every
process agrees on one.

`worsaga config` prints the resolved location of each, which is the
reliable way to find out rather than remembering what you set.

One honest caveat: deleting a local file does not necessarily delete every
copy of it. Time Machine, File History, `restic`/Borg backups, filesystem
snapshots (APFS, Btrfs, ZFS, VSS), and cloud file-sync clients may hold
earlier versions of any of these paths — including the configuration file
with your token in it. If a token has been exposed, resetting it in Moodle is
what actually revokes it; deleting the local file is not a substitute.

## For Moodle administrators

If you run a Moodle site and you are looking at Worsaga because you saw its
traffic, this section is for you.

**How to recognise it.** Every request carries a `User-Agent` of the form:

```text
worsaga/<version> (+https://github.com/yaminmushtaqr/worsaga)
```

for example `worsaga/0.8.2 (+https://github.com/yaminmushtaqr/worsaga)`.
Worsaga identifies itself deliberately; it does not imitate a browser or the
mobile app.

**What it does.** It calls a hardcoded allowlist of read-only functions over
your standard REST web-service endpoint (`/webservice/rest/server.php`),
authenticated by a token your own installation issued to the student
through Moodle's normal token interface. Each allowlisted function is
restricted to a fixed set of parameters, and self-scoped parameters are
filled in from the identity the site itself reports, so a request cannot be
widened to another user's data.

It is built to be a quiet client:

- at most **2 requests in flight** per site;
- request starts paced at least 250 ms apart;
- `Retry-After` honoured on `429` and `503`, with exponential backoff and
  jitter when the header is absent. The pacing, the in-flight limit, and
  the retry budget are per Worsaga process; a cooldown the *site* asked
  for is persisted and honoured machine-wide, so a `watch` loop and a
  manual sync back off together rather than each rediscovering the limit;
- a circuit breaker that stops unattended syncing after a sync in which
  the site rejected the credentials, rather than retrying a rejected token
  forever;
- one sync per site at a time, enforced by an interprocess lock.

**What it never does.** It does not ask for, accept, or transmit a user's
password. It does not call `login/token.php` or any other login endpoint. It
does not scrape HTML or use any interface other than the web-service API. It
calls no write function — submissions, posts, uploads, event creation, and
read-marking are all blocked before a request is made. And it reads only the
authenticated user's own data.

**If your site does not offer web services.** Worsaga reports that plainly to
its user and stops, including stopping any scheduled syncing. It documents no
workaround and supports none.

**Reaching the project.** Issues, questions, and reports are welcome at
<https://github.com/yaminmushtaqr/worsaga/issues>. For anything that should
not be public, see [SECURITY.md](SECURITY.md).

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
