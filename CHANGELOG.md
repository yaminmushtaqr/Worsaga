# Changelog

All notable changes to Worsaga are documented in this file.

## 0.8.2 (unreleased)

### Changed

- **BREAKING: `worsaga download` and `worsaga study-pack` no longer write
  to the current directory.** Without `--output` they used to save into
  whatever directory the shell happened to be sitting in. On a student's
  machine that is frequently a git checkout, and course materials are
  usually somebody else's copyrighted work: one `git add -A` away from
  being published. Both commands now default to Worsaga's own downloads
  directory — the same one the MCP `download_material` and
  `export_study_pack` tools have always used, so the two surfaces cannot
  drift apart:

  | OS | Default location |
  |---|---|
  | Linux | `~/.local/share/worsaga/downloads/` |
  | macOS | `~/Library/Application Support/worsaga/downloads/` |
  | Windows | `%LOCALAPPDATA%\worsaga\worsaga\downloads\` |

  `worsaga config` now prints the resolved path, and both commands state
  the full destination of what they wrote. **To get the old behaviour
  back, pass `--output .`** — explicitly, per command. `--output DIR`
  is otherwise unchanged, and the new `WORSAGA_DOWNLOADS_DIR` moves the
  default somewhere else permanently. It must be an absolute path (`~` is
  expanded): the CLI and the MCP server run with unrelated working
  directories, so a relative value would name two different places, and
  one of them would be wherever an agent host happened to be launched
  from. A relative value is reported on stderr and ignored.

  Whenever the resolved destination — default or explicit — turns out to
  be inside a git working tree, both commands print one warning line to
  stderr (suppressed by `-q`). It is a warning, never a refusal: there
  are good reasons to keep coursework under version control, and Worsaga
  is not in a position to know whether yours is one of them.
- **Study packs now open with a provenance header.** Every generated pack
  — written to a file, printed with `--stdout`, or returned by the MCP
  `export_study_pack` — begins with the Worsaga version that produced it,
  the source site, course, and week, the UTC generation time in ISO 8601,
  and the line *"Personal study material. Source materials retain their
  original rights; do not redistribute without permission."* A pack is a
  verbatim reproduction of a week's teaching material and it is a file
  that gets kept, copied between machines, and forwarded, so what it is
  travels with it rather than living only in this documentation. Packs
  were already created owner-only; that is unchanged.
- **Desktop notifications no longer carry change titles by default.**
  `worsaga watch` used to put up to three change titles in the
  notification body — an assignment name, a graded item — and a desktop
  notification is drawn by the operating system over whatever is on
  screen at the time: a shared display, a projector, a screen recording,
  a lock screen. The default body is now counts and course short-codes
  only ("Worsaga: 3 changes" / "2 in ECON101, 1 in CS210"). The new
  `worsaga watch --notify-details` (`notify_details=True` on `run_watch`)
  restores titles for someone who has decided their screen is private.
  What each mode promises, exactly: the Moodle token is stripped at the
  notification boundary itself (`send_notification` now redacts the title
  and the body, which the CLI's stdout wrapper could never do — this text
  leaves as subprocess argv); grade values, instructor feedback, and file
  contents are never composed into a notification in either mode, and no
  flag turns them on; and detailed mode reproduces item titles as their
  authors wrote them, which can include a URL or anything else somebody
  typed. That last point is why counts-only is the default.
- **A site with web services switched off is no longer reported as an
  authentication failure.** Moodle's `enablewsdescription` and
  `servicenotavailable` error codes were classified as rejected
  credentials, so a student whose institution simply does not offer
  web-service access was told to check a token that had never been
  rejected. They are now their own class: the CLI, the MCP
  `get_connection_info` (`error_code: "service_disabled"`), and a sync's
  `failure_class` all report *"This Moodle site has not enabled
  web-service access. That is the institution's decision, and Worsaga
  cannot be used with it."* Unattended syncing stops rather than
  retrying an answer that will not change. Worsaga documents no way
  around such a decision, and will not.
- **Downloaded filenames are now safe on Windows wherever they were
  made.** `_sanitize_filename` already replaced separators and wildcards,
  but left two Windows-specific hazards intact: names whose stem is a
  reserved device (`CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`,
  `LPT1`-`LPT9` — case-insensitive, and reserved with any extension, so
  `con.pdf` counts), and trailing dots, which Win32 strips silently so
  that `report.` and `report` become the same file and one download can
  overwrite another. Reserved stems are now prefixed with an underscore
  and trailing dots removed, on **every** platform: a pack built on Linux
  is routinely copied to a Windows machine afterwards. A name that
  sanitises away to nothing returns the caller's fallback rather than an
  empty string, which would have resolved to the destination directory
  itself.
- **Unattended syncs no longer collect forum discussions.** A forum
  discussion is other people's writing, and a scheduled job accumulating it
  in the background is a different proposition from a student opening
  `worsaga updates`. `worsaga watch`, the scheduled auto-sync
  (`worsaga sync --unattended`), and the MCP `sync_now()` now collect
  `deadlines`, `files`, and `grades`. A foreground `worsaga sync` you typed
  yourself still collects all four. New `--categories` (on `sync` and
  `watch`, comma-separated or `all`) and `WORSAGA_SYNC_CATEGORIES` override
  the default in either direction; `sync_now()` takes the same selector as a
  `categories` string. An unknown name is refused before any request, naming
  the valid values.

  A category that was not selected is **not** a category that failed: it is
  reported with `"selected": false` (printed as `(not selected)`), produces
  no change events and no tombstones, leaves its cached rows and its
  baseline untouched, and is excluded from the run's `outcome` — so a
  minimised sync is a `success`, not a permanent `partial`. Re-enabling a
  category later resumes the diff from where it stopped.
- **Instructor feedback text is no longer written to the local cache.** The
  sync stores `feedback_present` (a bool) and `feedback_hash` (a truncated
  SHA-256) instead, which keeps feedback-only grade changes detectable
  without keeping the words; the grades fingerprint now covers the hash
  rather than the text. `worsaga grades` is unchanged — it reads the
  gradebook live, so nothing is withheld from you: the table marks which
  items carry feedback and `worsaga grades --json` carries its full text.
  `--store-feedback` (or `WORSAGA_SYNC_STORE_FEEDBACK=1`) opts the text back
  into the cache; even then it stays out of the recorded change events that
  `worsaga changes` and `get_changes()` replay, so no configuration turns
  that surface into a feed of instructor comments.

  Existing caches migrate silently. The grades fingerprint is versioned, so
  the first sync after upgrading recognises rows written in the old shape,
  adopts them, and reports them as
  `N re-fingerprinted (storage format changed, not Moodle)` — rather than
  telling you that every grade you have ever received changed today.
  Adopting is not the same as going blind: the fields whose meaning did not
  move (grade, percentage, status, graded, graded date) are still compared,
  so a grade that genuinely changed on that same run is still reported. The
  one thing that cannot be judged is feedback itself, since the old row
  holds text and the new one holds a hash; a feedback-only edit made across
  the upgrade is caught on the following sync. A fingerprint this version
  cannot recognise at all — empty, truncated, or written by a *newer*
  Worsaga after a downgrade — is adopted without any event, and the
  round-trip is safe: an older Worsaga rewrites bare fingerprints, and the
  next upgrade migrates them again silently.
- **The MCP server now registers 14 of its 26 tools by default.** The
  default profile is the authenticated user's own academic picture:
  `list_courses`, `get_deadlines`, `get_grades`, `get_grade_summary`,
  `get_assignments`, `get_assignment_status`, `get_calendar_events`,
  `get_course_contents`, `get_week_materials`, `search_course_content`,
  `search_text`, `get_changes`, `get_autosync_status`, and
  `get_connection_info`.
  Everything that reads other people's writing, fetches file contents, or
  writes to a local store is behind a named capability and is **absent from
  the tool list** until enabled — not present-and-refusing, because a tool
  an agent can see is a tool an agent can be talked into calling. The
  capabilities are `forums`, `messages`, `notifications`, `digest`,
  `sync` (`sync_now`), `materials` (`download_material`,
  `extract_material`, `export_study_pack`, `get_weekly_summary`), and
  `index` (`build_search_index`). `get_changes` stays in the default
  profile: it makes no request and writes nothing, it replays events from
  data the user already chose to sync, and forums are outside the
  unattended collection default, so by default that feed is first-party.
  Set `WORSAGA_MCP_CAPABILITIES` to a comma-separated list or `all`; it is
  read once at start-up and the active profile is printed to stderr. An
  unknown name is ignored with a warning rather than refusing to start.

### Added

- **A first-use notice for third-party content.** The first time a run
  reads what other people wrote (forums, messages, notifications) against a
  real site, Worsaga prints one notice on stderr: that a local copy is kept
  as personal study material, that it should be treated as text to read
  rather than instructions to act on, and how to collect less. It is
  recorded per site in the state directory so it appears once, and it is
  never shown in demo mode or under `-q`.
- **Token redaction at every boundary.** A new `worsaga.redact` module is
  the single definition of what a secret looks like, and it now governs
  every way data leaves the process. Two rules: the configured token in any
  of its encoded spellings (raw, percent-, plus-, and double-encoded, in
  upper- and lower-case escape forms), and any `token`-like query parameter
  whatever its value — with `=` written literally, as `%3D`, or as `%253D`.
  The second rule closes the gaps where Moodle's own links passed through
  unchanged: a notification's `contexturl`, a calendar event URL, and forum
  and assignment `view_url` fields are redacted at the record factory.

  Applied at: the CLI's stdout and stderr (including their binary
  `.buffer`, and across writes, so a value split by `print("a", token)` is
  still caught); every MCP tool result, including mapping *keys*; the
  exception a tool body raises; argument-validation failures inside
  FastMCP, which never reach a tool body at all; every logging handler in
  both the CLI and the MCP server, message and traceback alike — which
  matters because FastMCP configures logging at import, so its handler
  holds a reference to the real stderr that no later wrapping can reach;
  and the local SQLite cache, whose sanitizer previously carried its own
  weaker pattern and so persisted encoded forms that the output boundary
  caught. The token is registered from every source it can arrive by,
  including `--token-stdin`.
- **MCP input caps on every tool, whatever the profile.** Day windows are
  clamped to 0-730 (look-ahead) and 0-365 (look-back), `search_text` results
  to 200, `get_changes` to 500 events, and `build_search_index` to its
  100-file budget, which can no longer be raised from the tool call.
  Negative and absurd values are clamped rather than trusted, and a
  non-numeric argument falls back to the documented default.
- **MCP tool annotations** (`readOnlyHint`, `destructiveHint`,
  `idempotentHint`, `openWorldHint`) as advisory metadata, plus a
  "third-party content" line in the description of all 17 tools whose
  results can carry text written by other people, telling the reading agent
  to treat it as data and never as instructions.
- `others_personal` policy metadata on the allowlisted functions whose
  responses carry other people's personal content
  (`mod_forum_get_forums_by_courses`, `mod_forum_get_forum_discussions`,
  `message_popup_get_popup_notifications`, `core_message_get_messages`),
  exported as `worsaga.OTHERS_PERSONAL_FUNCTIONS` and checked by the test
  suite, so a future allowlist entry that returns other people's content
  cannot be added without a deliberate decision. It is deliberately
  narrower than the MCP layer's "third-party content" tool label, which
  asks the broader question "could this text be a prompt-injection
  payload?" and so also covers section summaries, event descriptions,
  extracted file text, and instructor feedback. The tests assert that the
  narrow set nests inside the broad one.

- MCP tools now accept a course short-code anywhere they take a
  `course_id`, not just a numeric id. Every course-taking tool
  (`get_grades`, `get_course_contents`, `get_week_materials`,
  `download_material`, `extract_material`, `get_assignments`,
  `get_course_forums`, `get_calendar_events`, `build_search_index`, and
  the rest) resolves an `int | str` argument the same way the CLI does — an
  int or digit-string is confirmed against your enrolled courses, a name is
  matched case-insensitively by exact short-code and then by unambiguous
  prefix — so an agent no longer has to call `list_courses` and match ids
  itself. An unknown id or name returns a structured
  `{"error", "error_code": "course_not_found"}` dict; an ambiguous prefix
  returns the new `{"error", "error_code": "course_ambiguous",
  "candidates": [{id, shortname, fullname}, ...]}`. The CLI and MCP now share
  one resolver in `worsaga.courses`. The one exception is `search_text`,
  which queries only the local index and so filters by a numeric id directly,
  keeping its no-network contract.
- New read-only MCP tool `get_connection_info` — a cheap "am I connected?"
  check that reports `authenticated`, `demo_mode`, the Moodle `site_url`
  (base URL only), `site_name`, the authenticated `user_id` and
  `user_display_name`, the `worsaga_version`, and a `config_source` hint
  (`env` / `file` / `demo` / `unset`, with the file *path* only, never its
  contents). It makes at most one `core_webservice_get_site_info` call and
  returns a structured `{"error", "error_code": "auth" | "network" |
  "rate_limited" | "service_disabled"}` dict on failure. The token never
  appears in any field. This brings the MCP tool count to 26.
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
- `--token-stdin`, a way to pass the API token that never puts it in the
  command line. Available globally (`pass show moodle | worsaga --token-stdin
  courses`) and on `setup` (`worsaga setup --url <URL> --token-stdin`, which
  reads the first line of stdin, strips it, and writes the credentials file).
  It works piped and typed, echoes nothing, and never appears in an error
  message. `setup --token-stdin` requires `--url`, because the guided prompts
  cannot read a URL from a stdin the token has already been taken from.

- A new `principal_mismatch` value in the MCP `error_code` vocabulary.
  `sync_now`, `build_search_index`, and `search_text` return
  `{"error", "error_code": "principal_mismatch"}` when the local store they
  would use belongs to a different Moodle account, instead of raising an
  unstructured tool error. The message names both user ids and the file to
  remove.

- **A network etiquette engine.** Worsaga now paces every request it makes
  to a Moodle site, whatever command or tool asked for it: **never more
  than 2 requests in flight per site at once**, and **at least 250 ms
  between request starts**. The fan-outs still use four worker threads,
  because most of their time is parsing and cache work — the wire is what
  is limited. Both knobs move in the polite direction only:
  `WORSAGA_MIN_REQUEST_GAP_MS` can *raise* the gap and `WORSAGA_MAX_IN_FLIGHT`
  can *lower* the concurrency; smaller gaps and larger concurrency are
  ignored, and there is no way to switch the limiter off. Demo mode is
  entirely offline and unthrottled.

- **Server backoff is honoured, and shared across processes.** When a site
  answers `429 Too Many Requests` or `503 Service Unavailable`, Worsaga
  reads `Retry-After` in both of its forms (a number of seconds and an
  HTTP-date), waits that long — capped at two minutes, floored at zero so a
  skewed clock cannot produce a negative wait — and retries at most three
  times per request. Without the header it backs off exponentially with
  full jitter (1s, 2s, 4s ... capped at 60s). The wait applies to the whole
  site rather than to the one refused request — and is in place before the
  refused request gives up its slot, so a queued worker cannot slip onto
  the wire ahead of it. It is also written to a small state file that other
  Worsaga processes re-read as they go, so a running `worsaga watch` and a
  `worsaga sync` you start by hand back off together instead of each
  discovering the limit separately; the recorded deadline only ever moves
  later, so a short wait cannot cut a long one short. Retries draw on one
  shared per-site budget, so four workers meeting the same limit cannot
  multiply it. When the retries run out the command fails with a plain
  `the site is rate-limiting requests; try again later.`; every MCP tool
  returns the new `rate_limited` error code; downloads report
  `DownloadError` code `rate_limited`.

- **Truthful sync outcomes.** `worsaga sync`, `sync_now`, and each `watch`
  cycle now report an `outcome`: `success` (every category synced),
  `partial` (some did), `failed` (none did), or `skipped` (another Worsaga
  process was already syncing this site). Previously a run that reached
  Moodle and could not fetch a single category returned normally with an
  empty change list — indistinguishable from a healthy sync that found
  nothing new. A failed run also carries a coarse `failure_class` (`auth`,
  `network`, `rate_limited`, `service_disabled`, `other`).

- **One sync per site at a time.** A sync takes an interprocess lock beside
  the cache before it fetches anything, so a `watch` loop, the scheduled
  auto-sync, and a manual run cannot fetch every course two or three times
  over. A run that finds the lock held returns `outcome: "skipped"` (CLI:
  one line and exit 0; MCP `sync_now`: `{"error", "error_code":
  "sync_in_progress"}`) without making a single request. A lock left behind
  by a crash is recovered immediately when the owning process is provably
  gone (POSIX), or after two hours of silence where liveness cannot be
  checked (Windows has no safe standard-library equivalent); a long sync
  keeps saying it is alive as it goes, so a slow first sync is never
  interrupted. Each lock carries an ownership token and nothing ever
  deletes a lock that is not its own.

- **Watch backs off when it keeps failing.** Consecutive failed cycles
  double the interval, capped at eight intervals or one hour (whichever is
  smaller) with +/-10% jitter, and print one `Backing off: next cycle in Xs
  (N consecutive failures).` line to stderr. A successful or partial cycle
  returns to the base interval. A *skipped* cycle leaves the streak exactly
  as it was — neither counting against it nor clearing it — because
  "another process was already syncing" says nothing about whether the site
  is reachable, and clearing the streak on it would resume hammering a
  broken site at full rate.

- **A circuit breaker for rejected credentials.** After a sync fails
  because Moodle rejected the token, unattended runs — `watch` cycles and
  the scheduled auto-sync — stop before making any request and say
  `circuit open: fix credentials then run 'worsaga sync' manually`. A site
  that has switched web services off opens the breaker too, with its own
  wording: nothing the user does will change that answer, and a scheduled
  job that keeps asking is load nobody asked for. Running `worsaga sync`
  yourself always tries,
  and any successful sync closes the breaker. `worsaga auto-sync status`
  now shows the last outcome, the consecutive-failure count and its class,
  and whether scheduled syncs are paused. Network failures and rate limits
  never open the breaker; they are temporary and worth retrying.

- Fewer requests for the same answers. `worsaga digest` fetches the
  enrolled-course list once and shares it with the deadline, assignment,
  and forum-update sources instead of letting each rediscover it (3
  requests down to 1); a sync run shares one list across course discovery,
  grades, files, and forums (4 down to 1).

### Deprecated

- Passing the token as a command-line argument. `--token VALUE` (global) and
  `worsaga setup --token VALUE` still work exactly as before, but now print a
  one-line warning to stderr: an argument is recorded in shell history and is
  visible to every other process on the machine through the process list, so
  a secret should never travel that way. Released versions up to and
  including 0.8.1 documented this as the ordinary non-interactive path, so
  anyone who used it should treat that token as exposed and reset it. Use
  `WORSAGA_TOKEN`, a credentials file, `--token-stdin`, or the interactive
  `worsaga setup` prompt instead; the help text and README now list them in
  that order of preference.

### Fixed

- **Messages Worsaga prints are now ASCII, so they read correctly on a
  Windows console.** Em dashes, en dashes, and the typographic bullet in
  summary output cannot be encoded by a cp437 console at all, and a
  cp1252 one emits bytes that a UTF-8 terminal emulator draws as
  replacement characters — so `worsaga doctor`, `worsaga setup`, several
  `--help` texts, the download and network error messages, and the
  bullets in `worsaga summary` all showed mojibake in the middle of a
  sentence meant to explain something. The default bullet marker in
  `format_bullets` is now `-` (pass `marker=` for anything else). A test
  walks every module's AST and fails on a non-ASCII string constant
  outside a docstring, so the rule is enforced rather than remembered;
  the banner's deliberate block art and the text-cleaning regexes that
  must match curly quotes are exempt by name.
- `worsaga sync --unattended` no longer tells a user whose institution has
  disabled web services to fix their credentials. That branch printed the
  authentication wording whatever opened the circuit; it now reads from
  `failure_class`, and the service-disabled case says the site has not
  enabled web-service access and that syncing stays paused until it does —
  without suggesting a manual sync, which would fail identically.
- `worsaga config` now also reports the resolved cache path, search-index
  path, and state directory, so the deletion instructions in the README
  can point at one command that tells you where everything actually is,
  including anything a `WORSAGA_*` override has relocated.
- `worsaga watch` no longer reports a cycle that fetched nothing as a
  successful one. A sync that reached Moodle and failed every category, and
  a sync refused because the local cache belongs to a different Moodle
  account, both returned normally — so the cycle was counted `ok`, the
  failure count stayed at zero, and the run looked exactly like a healthy
  cycle with no changes. Both are now failed cycles: they count in the
  summary, print the reason, and drive the new backoff.

- A Moodle reply that is not a web-service response no longer surfaces as a
  raw JSON decode error. A sign-in page, a captive portal, or a proxy error
  page in place of the API now raises a typed failure naming the declared
  content type and the size — never any part of the body, which can contain
  anything. Web-service replies are also read under a hard 16 MiB cap
  (downloads keep their own 50 MiB cap), so a misbehaving endpoint cannot
  make the process allocate without bound.

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
  watch` (floor 300s) and `worsaga auto-sync install` (floor 15 min)
  previously clamped a too-small interval up with no indication — a
  `--interval 30s` watch quietly ran on the floor, and `auto-sync install
  --interval 30s|45|2m|90s` quietly became the minimum period. Both now
  print a one-line stderr warning stating the requested and applied values
  (e.g. `Warning: interval 30s is below the minimum for watch; using
  300s.`), and each floor is documented in the command's `--interval` help
  text. The clamping behaviour itself is unchanged.
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

- **`worsaga sync` now exits 1 when the sync fetched nothing at all.** It
  previously exited 0 whatever happened, so a scheduled job or a script had
  no way to tell a working sync from one that silently reached nothing. A
  partial sync still exits 0 and prints its warnings exactly as before, and
  a run skipped because another sync held the lock also exits 0. Scripts
  that treated `worsaga sync` as always-succeeding need to expect a
  non-zero exit on total failure. In `--json`/`--yaml` mode the payload
  carries the same verdict in its new `outcome` field.

- "Last sync" timestamps no longer advance on a run that synced nothing.
  `worsaga auto-sync status` and the MCP `get_auto_sync_status` report the
  last run that actually fetched something, so a site that has been failing
  all day no longer looks freshly synced.

- The scheduled auto-sync command is now `worsaga sync --quiet
  --unattended`, so it honours the credential circuit breaker. A job
  registered by an earlier version keeps working unchanged (without the
  flag) until it is reinstalled.

- The per-course / per-forum metadata fan-outs behind `digest`, `sync`,
  `assignments`, `updates`, and `grades` now run on a small bounded thread
  pool (default 4 workers; `WORSAGA_CONCURRENCY` overrides it, clamped to
  1-8) instead of one blocking request at a time. On a real multi-course
  account this turns multi-minute waits into seconds. Results are always
  reassembled in the original course order and the diff/write phase of
  `sync` stays single-threaded, so change detection is byte-for-byte
  identical to the sequential path; per-course permission warnings stay
  attributed to their own course. The shared read-only client is safe to use
  across threads (it holds only immutable config and opens a fresh
  connection per request). The bounds are deliberately conservative: Moodle
  core applies no server-side rate limiting to web-service calls and
  ecosystem guidance for well-behaved clients is around two concurrent
  connections. Since this same release the worker pool is no longer what
  paces Worsaga at all — the per-origin limiter described above is, and it
  holds the wire to two concurrent requests however many workers are
  running.
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
- Raised the polling floors so a mistyped interval cannot turn Worsaga into
  a hammer on a shared server. `worsaga watch --interval` now clamps to a
  minimum of 300s (was 60s) and `worsaga auto-sync install --interval` to 15
  minutes (was 5). The defaults are unchanged (15 minutes for `watch`, 30
  minutes for `auto-sync`), a below-floor request is accepted and clamped to
  the floor with a warning stating both values rather than rejected, and
  each command's `--interval` help text states the new floor. Every cycle is
  a full metadata sync across all enrolled courses, so a tighter loop cost
  the server real work without surfacing changes meaningfully sooner.
- Every outgoing request now identifies the project in its `User-Agent`:
  `worsaga/<version> (+https://github.com/yaminmushtaqr/worsaga)`. Standard
  bot etiquette — an administrator who sees the traffic can tell what it is
  and who to contact.
- The optional `mcp` extra now requires `mcp>=1.28,<2` (was `mcp>=1.0`).
  mcp 2.0 removed `mcp.server.fastmcp`, which the MCP server imports, so an
  unpinned `pip install worsaga[mcp]` could install a release that fails at
  import. CI now runs the suite against both ends of the supported range.
- Stricter validation of the configured Moodle URL, and one canonical
  spelling of it. The base URL is the origin every file download is checked
  against, so it now has to be a plain site address: a URL with credentials
  (`https://user:pass@moodle.example.edu`), a query string or fragment, no
  host (`https:///moodle`), or a non-HTTP(S) scheme is rejected outright.
  The local-development exemption from HTTPS is now decided by parsing the
  host as an IP address rather than by how it is spelled, so
  `http://127.example.com` and `http://127.0.0.1.nip.io` — ordinary DNS
  names anyone can register and point anywhere — are no longer accepted as
  local. A genuinely local instance still works over plain HTTP:
  `http://localhost:8080`, `http://127.0.0.1`, `http://[::1]:8080`.
  Normalisation stays deliberately minimal — the scheme and host are
  lowercased, an explicit `:443`/`:80` is dropped, a trailing slash is
  stripped, and the path is otherwise untouched — so an ordinary configured
  value such as `https://moodle.university.example/moodle` is unchanged and
  existing caches and sync history keep matching.

### Security

- Every file Worsaga creates to hold credentials or course data is now
  owner-only from the moment it exists, through one shared primitive
  (`worsaga.secureio`) rather than a permission
  dance repeated at each call site. Previously the mode was applied *after*
  creation, or not at all: on POSIX with an ordinary `umask 022`, the
  placeholder that `download` and `study-pack` reserve was created `0755`
  (world-readable **and** executable), a written study pack kept that mode,
  and the cache and search-index databases were created `0644` by SQLite and
  only chmod-ed afterwards — which never covered the rollback journal SQLite
  writes beside them, so indexed course text was readable by any other local
  user for the duration of a write. Files are now created with mode `0600` in
  the `os.open` call itself, and the databases are created before SQLite opens
  them so the database *and* its journal are private from the first byte; a
  database left loose by an older release is tightened on the next open.
  Directories Worsaga creates (the config, data, and download directories, and
  a `--output` directory it has to create) are made `0700` at creation,
  including intermediate levels; a directory that already exists is never
  chmod-ed, since the user may have shared it on purpose. On Windows POSIX
  modes do not apply and these paths keep inheriting the user profile's ACLs.
  The launchd plist and systemd unit files `auto-sync install` writes stay
  ordinary `0644`: they hold no secrets — the scheduled command line carries
  no credentials — and the platform's service manager has to be able to read
  them.
- The credentials file is now written atomically and never through a symbolic
  link. It is written to a private sibling temp file, fsynced, and renamed
  over the destination, so `config.json` always holds either the old content
  or the new content and never a mixture, and no reader can observe a
  half-written one. A destination that exists as a symbolic link (or anything
  other than a regular file) is refused with a clear error instead of
  followed, and because the rename replaces the link itself, a link planted
  between the check and the write cannot redirect the token either. The same
  refusal now guards the cache and search-index paths before SQLite opens
  them, and the study-pack export routes its content through the same write
  instead of reopening the reserved filename — a symlink swapped in after the
  name was reserved is refused rather than followed.
- `MoodleConfig` no longer prints the token in its `repr`. The config object
  is held by every client, so it reached tracebacks, log records, `--verbose`
  style dumps, and agent transcripts verbatim; it now renders as
  `MoodleConfig(url='https://...', token='***', userid=7)`, saying nothing
  about the token beyond whether one is set. Reading `config.token` is
  unchanged.
- The API token is redacted from Moodle's own error text. When a web-service
  call returns an `exception` payload, the server-chosen `message` and
  `errorcode` become a `MoodleRequestError` that the CLI prints and agents
  surface; any occurrence of the configured token (raw or percent-encoded, as
  it appears after `urlencode`) is now replaced with `***` first. Stock Moodle
  does not echo `wstoken` back, but a plugin, reverse proxy, or WAF that
  quotes the offending request can, and that message ends up in terminals,
  logs, and bug reports. This is a targeted fix at the one place server text
  becomes a Worsaga error string, not a general output-boundary redaction —
  that is separate, upcoming work.
- Worsaga's own subprocesses no longer inherit the API token. The scheduler
  helpers (`schtasks`, `launchctl`, `systemctl`) and the desktop-notification
  helpers (PowerShell toast, `osascript`, `notify-send`) previously ran with
  the full environment, which on many systems means any process able to read
  `/proc/<pid>/environ` could recover `WORSAGA_TOKEN` from them. They now run
  with `WORSAGA_TOKEN` removed and everything else (`PATH`, `SystemRoot`,
  `DISPLAY`, `DBUS_SESSION_BUS_ADDRESS`, ...) intact.
- Local stores are now bound to the Moodle account that filled them. The sync
  cache, the full-text index, and the auto-sync record each record the
  *verified* user id (the one the site itself reports) alongside their
  existing site keying. A write from a different account for the same site is
  refused with an error naming both user ids and the file to remove, instead
  of silently diffing one account's Moodle against another's baseline or
  answering searches from another account's documents — the case where two
  Moodle accounts share one OS login (re-enrolment, a shared lab machine, a
  personal and a staff token). The identity is read at the moment the store is
  written, never earlier, so a run whose first call failed and whose later
  calls succeeded cannot slip past the check unattributed; a run that verified
  no identity at all writes nothing into a store that already belongs to an
  account, and says so, rather than failing hard on a network outage. A store
  with no stamp is adopted by the first
  authenticated caller, with a one-line notice when it already held data, so
  existing local data survives the upgrade. Reads that make no network request
  (`worsaga changes`, an offline `search-text`) are deliberately not guarded:
  there is no verified identity available offline, and a check against an
  unverified id would guarantee nothing — the OS user boundary is the real
  line there, and the module documents this rather than implying more. This is
  an interim guard; full per-account namespacing of the store paths is planned
  for 0.9.0.
- The authenticated user is now verified against the Moodle site instead of
  taken on trust from configuration. `WORSAGA_USERID` (and the `userid` in a
  credentials file or `--userid`) is treated as a hint: on first use the
  client reads the real user id from `core_webservice_get_site_info`, uses
  that for every self-scoped read, and — if the configured value disagrees —
  prints one warning naming both and proceeds with the site's answer. The
  configured value never reaches the wire. Previously a token with elevated
  (teacher or admin) capabilities plus a foreign `userid` would have
  collected that other person's courses, gradebook, and messages. If
  site-info cannot be fetched the request fails; there is no fall back to
  the unverified value. Cost: at most one extra `core_webservice_get_site_info`
  call per client instance — one per command in practice, and the same cheap
  call the official Moodle app makes at startup — memoised and shared with
  `worsaga doctor` / `get_connection_info`, which already made it.
- Every allowlisted web-service function now carries an enforced parameter
  allowlist. The client's policy table records the complete set of request
  parameters Worsaga sends for each function, derived from the wrapper that
  calls it, and any other parameter raises the new `MoodleParameterError`
  before a request is built (array arguments such as `courseids[0]` are
  matched by base name). A caller reaching `MoodleClient.call` directly can
  no longer widen a request with extra Moodle arguments the feature never
  needed — including optional user-identity arguments such as the `userid`
  that `mod_assign_get_submission_status` accepts. The `exposed` flag in the
  same table is now enforced too: `core_webservice_get_site_info` is
  internal to the client and is reachable only through its
  `site_info()` wrapper, not through public `call()`.
- Course-scoped reads are now confined to the courses the authenticated user
  is enrolled in. Course contents, grades, assignments, forums, quizzes,
  calendar-by-course, materials, downloads, summaries, and study packs all
  check the course id against the enrolment list before the request, and a
  numeric course id passed to the CLI or an MCP tool is checked too (it used
  to be forwarded verbatim). The check lives in the client's own dispatcher,
  keyed off the parameters each allowlisted function uses to name a course,
  so raw `MoodleClient.call` is bound by it as well as the convenience
  wrappers. The enrolment set is memoised per client and
  refreshed whenever the course list is fetched, so flows that already list
  courses make no extra request. An id outside the set produces the existing
  structured failure — a friendly `Error:` line and exit 1 in the CLI,
  `{"error", "error_code": "course_not_found"}` from the MCP tools.
- Removed the fabricated fallback targets behind `assignments`, `grades`,
  and `forums`. An unrecognised course id used to be turned into a synthetic
  `{"id": <id>, "shortname": "<id>"}` record that was then sent to Moodle and,
  if the server answered, presented as a course; an unrecognised forum id was
  likewise fabricated into a placeholder forum whose id was handed straight to
  `mod_forum_get_forum_discussions`. Both are gone: an unknown course is
  `course_not_found` and a forum that is not one of the validated course's own
  forums is the new `forum_not_found` (MCP) / `Error:` exit 1 (CLI). Neither
  produces a record for something that was never fetched, and neither probes
  the server with an id the account has no claim to.
- `core_course_get_courses` is off the read-only allowlist. It reads course
  metadata by arbitrary id, so it can describe courses this account is not
  enrolled in; nothing called it. Enrolment-scoped discovery through
  `core_enrol_get_users_courses` is the sanctioned path, and a test now
  asserts every allowlisted function is actually reached by a client wrapper,
  so an unused capability cannot sit on the list again.
- Worsaga can no longer be pointed at another person's data, in two layers.
  The client's `get_user_grade_items` used to take an optional user id and a
  `get_course_grades` helper took a whole list of them — with a token
  carrying elevated (teacher or admin) capabilities, either could have
  returned other students' gradebooks. No Worsaga feature ever passed a user
  id, so behaviour is unchanged for every real caller, but the capability
  existed in the API. `get_user_grade_items(course_id)` is now self-only by
  construction (no user-id argument at all), `get_course_grades` is deleted,
  and `core_grades_get_grades` is off the read-only allowlist entirely,
  alongside `core_enrol_get_enrolled_users` and `mod_assign_get_grades`.
  Underneath the wrappers, the client's own dispatcher now enforces the
  same rule for every allowlisted function: a user-identity parameter
  (`userid`, `useridto`) is filled in with the authenticated user when a
  caller omits it, and naming anyone else raises `MoodleScopeError` before
  any network request is made — so the guarantee no longer depends on which
  method a caller reaches for. The CLI `grades` command and the MCP
  `get_grades` / `get_grade_summary` tools never exposed a user id and are
  unaffected.
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
