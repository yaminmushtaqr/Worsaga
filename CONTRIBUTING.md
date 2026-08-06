# Contributing

Worsaga is currently a personal public developer project.

External code contributions, pull requests, patches, translations, design
contributions, and substantial implementation proposals are not being accepted
at this stage.

Bug reports, reproducible issues, documentation corrections, and general user
feedback are welcome through GitHub Issues.

Please do not include implementation code in issues beyond the minimal snippet
needed to reproduce a bug. Unsolicited pull requests may be closed without
review.

This policy keeps copyright ownership, licensing, and project direction clear
while Worsaga is early-stage.

## Safety rules for any change

Worsaga is read-only against Moodle. It does write locally — config, cache,
search index, downloads, study packs, scheduler registration — so "read-only"
is a claim about the LMS, never about the machine.

- Do not add LMS write actions.
- Do not submit assignments, post replies, upload files, create events, delete
  content, or mark resources viewed.
- Do not bypass `MoodleClient` for Moodle API calls.
- Do not add functions to `ALLOWED_FUNCTIONS` unless they have been reviewed as
  read-only.
- Do not expose tokens, authenticated URLs, cookies, or personal data.
