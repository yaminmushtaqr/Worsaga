# CLAUDE.md — Worsaga

## Never commit private or planning documents

Never commit, stage, or push, in this repository or any repository derived
from it:

- Planning documents of any kind (`*plan*.md`, working roadmap drafts, release
  plans, milestone breakdowns kept as working files)
- Private notes, strategy, outreach, idea, brainstorm, or feature-draft docs
- Deny-lists or private audit pattern files
- Anything under `notes-private/`
- Local agent settings (`.claude/`, `*.local.json`)
- Any document containing personal identifiers or private context

The published `ROADMAP.md` is the deliberate public roadmap and is exempt.

The only permitted identifying content in tracked files is the maintainer's
name and public GitHub/repository URLs, and only where deliberately chosen
(LICENSE, TRADEMARKS.md, package metadata).

Working and planning docs live untracked at the repo root or inside
`notes-private/` (git-ignored). They must never appear in any commit, public
export, or published artifact — including commit messages, branch names, and
PR/issue text.

Enforcement:

- `.gitignore` already covers these patterns. Never use `git add -f` to bypass
  it — if git refuses a file, that is the system working.
- Before committing, check `git status` for any plan/notes/idea file; if one
  appears as tracked or staged, stop and fix the ignore rules instead of
  committing it.
