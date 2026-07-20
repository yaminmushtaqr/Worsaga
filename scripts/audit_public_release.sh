#!/usr/bin/env bash
# Generic public-release audit for Worsaga.
#
# This script is public and must stay generic: it checks for credential
# shapes, private-file names, local machine paths, and non-public
# install/licence language. It must never embed project-specific private
# strings. It scans every tracked file, including itself; pattern
# literals below are written so they do not match their own definitions.
#
# Maintainers additionally run a private deny-list scan from a local,
# git-ignored pattern file; that list is intentionally not part of this
# script or this repository.
#
# Usage: scripts/audit_public_release.sh [--no-build]
#   --no-build   skip building/scanning wheel and sdist artifacts

set -u

# The tracked-file scans are built on git ls-files; outside a work tree
# they would scan nothing and the audit would pass vacuously.
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "AUDIT FAIL: not inside a git work tree; tracked-file scans cannot run." >&2
    exit 1
fi

FAILURES=0

fail() {
    echo "AUDIT FAIL: $1" >&2
    FAILURES=$((FAILURES + 1))
}

# Grep helper over a file list on stdin.
# Args: <label> <extended-regex> [<exclude-regex-for-matching-lines>]
scan_files() {
    label="$1"
    pattern="$2"
    allow="${3:-}"
    matches=$(xargs -r grep -nEI -- "$pattern" 2>/dev/null || true)
    if [ -n "$allow" ] && [ -n "$matches" ]; then
        matches=$(echo "$matches" | grep -vE -- "$allow" || true)
    fi
    if [ -n "$matches" ]; then
        echo "$matches" >&2
        fail "$label"
    fi
}

# ── Patterns (generic only; written to not match themselves) ────────
# Non-public distribution language.
PAT_PRIVATE_DIST='git\+ssh[:]//|deploy[ -]key|private (Git|GitHub|package index|repository over SSH)'
# Closed licence language and non-SPDX licence refs. Lines tagged with an
# explicit "audit-allow" marker (generic filtering heuristics) are skipped.
PAT_CLOSED='[Pp]roprietar[y]|closed[-]source|LicenseRef[-]'
ALLOW_MARKED='audit[-]allow'
# Real-looking Moodle hostnames; example domains are filtered per scan.
PAT_MOODLE_HOST='moodle[.][a-z0-9-]+[.](ac[.][a-z]+|edu[a-z.]*|org|com|net)'
ALLOW_EXAMPLE_HOST='moodle[.]example[.]'
# Local absolute paths that identify a developer machine; the generic
# placeholder username "user" is allowed (used in test fixtures/docs).
PAT_LOCAL_PATH='[A-Z]:\\+Users\\+[A-Za-z]|/home/[a-z]+/|/Users/[a-z]+/'
ALLOW_PLACEHOLDER_PATH='/home/user/|/Users/user/'
# Obvious credential shapes: long hex tokens, key markers, inline secrets.
PAT_CREDENTIALS='\b[a-f0-9]{32,}\b|BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY|(api|access|auth)[_-]?token["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"']?[A-Za-z0-9]{8,}|password[[:space:]]*=[[:space:]]*[A-Za-z0-9]{6,}'
# Files where long hashes are expected and not credentials, plus GitHub
# Actions pinned by full commit SHA ("uses: owner/repo@<40-hex>").
HASH_FILE_ALLOW='(^|/)(uv[.]lock|poetry[.]lock|package-lock[.]json|.*[.]dist-info/RECORD)([:,]|$)|uses:[[:space:]]+[^[:space:]]+@[a-f0-9]{40}'
# Private planning / local-agent / secret files that must not be tracked
# or shipped (checked against file NAMES, not contents).
PRIVATE_FILE_NAMES='newplan[.]md|oldplan[.]md|plan-private[.]md|notes-private|settings[.]local[.]json|(^|/)[.]env($|[.])|[.]token$|[.]secret$|worsaga-creds[.]json'

tracked_files() {
    git ls-files
}

echo "== Scanning tracked files =="

# scan_files reads the file list from stdin via process substitution, NOT
# a pipeline: a pipeline would run the function in a subshell, where its
# FAILURES increment is silently lost and findings could never fail the
# audit.
scan_files "non-public distribution language in tracked files" "$PAT_PRIVATE_DIST" < <(tracked_files)
scan_files "closed licence language in tracked files" "$PAT_CLOSED" "$ALLOW_MARKED" < <(tracked_files)
scan_files "non-example Moodle hostname in tracked files" "$PAT_MOODLE_HOST" "$ALLOW_EXAMPLE_HOST" < <(tracked_files)
scan_files "local absolute path in tracked files" "$PAT_LOCAL_PATH" "$ALLOW_PLACEHOLDER_PATH" < <(tracked_files)
scan_files "credential-shaped string in tracked files" "$PAT_CREDENTIALS" "$HASH_FILE_ALLOW" < <(tracked_files)

bad_tracked=$(git ls-files | grep -E "$PRIVATE_FILE_NAMES" || true)
if [ -n "$bad_tracked" ]; then
    echo "$bad_tracked" >&2
    fail "private/local file tracked in git"
fi

# ── Artifact scan ───────────────────────────────────────────────────
if [ "${1:-}" != "--no-build" ]; then
    echo "== Building artifacts =="
    rm -rf dist
    if ! python -m build > /dev/null 2>&1; then
        fail "python -m build failed"
    else
        AUDIT_TMP=$(mktemp -d)
        trap 'rm -rf "$AUDIT_TMP"' EXIT

        echo "== Scanning sdist and wheel contents =="
        found_sdist=0
        for sdist in dist/*.tar.gz; do
            [ -e "$sdist" ] || continue
            found_sdist=1
            mkdir -p "$AUDIT_TMP/sdist"
            tar -xzf "$sdist" -C "$AUDIT_TMP/sdist"
        done
        [ "$found_sdist" -eq 1 ] || fail "no sdist produced"
        found_wheel=0
        for wheel in dist/*.whl; do
            [ -e "$wheel" ] || continue
            found_wheel=1
            mkdir -p "$AUDIT_TMP/wheel"
            python -m zipfile -e "$wheel" "$AUDIT_TMP/wheel" > /dev/null
        done
        [ "$found_wheel" -eq 1 ] || fail "no wheel produced"

        bad_names=$(find "$AUDIT_TMP" -type f | grep -E "$PRIVATE_FILE_NAMES" || true)
        if [ -n "$bad_names" ]; then
            echo "$bad_names" >&2
            fail "private/local file shipped inside built artifact"
        fi
        # Shell scripts (like this audit) must not ship in artifacts.
        shipped_scripts=$(find "$AUDIT_TMP" -type f -name '*.sh' || true)
        if [ -n "$shipped_scripts" ]; then
            echo "$shipped_scripts" >&2
            fail "shell script shipped inside built artifact"
        fi

        scan_files "non-public distribution language in built artifact" "$PAT_PRIVATE_DIST" < <(find "$AUDIT_TMP" -type f)
        scan_files "closed licence language in built artifact" "$PAT_CLOSED" "$ALLOW_MARKED" < <(find "$AUDIT_TMP" -type f)
        scan_files "non-example Moodle hostname in built artifact" "$PAT_MOODLE_HOST" "$ALLOW_EXAMPLE_HOST" < <(find "$AUDIT_TMP" -type f)
        scan_files "local absolute path in built artifact" "$PAT_LOCAL_PATH" "$ALLOW_PLACEHOLDER_PATH" < <(find "$AUDIT_TMP" -type f)
        scan_files "credential-shaped string in built artifact" "$PAT_CREDENTIALS" "$HASH_FILE_ALLOW" < <(find "$AUDIT_TMP" -type f)
    fi
fi

if [ "$FAILURES" -gt 0 ]; then
    echo "Public release audit FAILED with $FAILURES finding(s)." >&2
    exit 1
fi
echo "Public release audit passed."
