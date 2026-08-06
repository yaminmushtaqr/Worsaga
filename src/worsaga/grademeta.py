"""What the cache keeps about a grade instead of the instructor's words.

Worsaga stores ``feedback_present`` and a truncated ``feedback_hash`` in
place of feedback text, so a feedback-only edit is still a detected grade
change while the words themselves never reach the local database, the
recorded change events, or an agent replaying them.

Two very different callers have to agree on exactly what that means:

- :mod:`worsaga.sync`, which prepares and fingerprints every grade it
  collects, and
- :mod:`worsaga.cache`, which scrubs the rows an *older* Worsaga wrote
  before the swap — the one-time migration that runs when a cache built
  by 0.8.1 or earlier is first opened.

If those two computed the hash or the fingerprint even slightly
differently, the migration would rewrite every row into a shape the next
sync does not recognise and report the whole gradebook as changed. So
both derive from this module and neither owns a copy.

Nothing here imports the rest of Worsaga: ``sync`` imports ``cache``
already, and a shared definition that either of them owned would be a
cycle waiting to be written.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: Hex characters kept from the feedback digest. Sixty-four bits is far
#: more than change detection over one person's gradebook needs, and a
#: short digest is a worse oracle for confirming a guess at the text than
#: a full one would be.
FEEDBACK_HASH_CHARS = 16

#: The grade fields a stored fingerprint covers. ``feedback_hash`` stands
#: in for the instructor's words: a feedback-only edit still changes the
#: fingerprint, but neither the items table nor a change event ever holds
#: the text.
GRADE_FINGERPRINT_FIELDS = (
    "grade_display", "percentage", "status", "feedback_hash",
)

#: Version of the grade fingerprint shape. Bumped when the swap from
#: feedback text to a hash changed which fields are covered, which is what
#: stops an existing cache from reporting every grade as updated once.
GRADE_FINGERPRINT_VERSION = 2

#: The fields the fingerprint covered *before* that swap: the same three
#: stable fields and the feedback text itself. Kept here beside the
#: current list rather than in the migration that reads it, so the two
#: shapes are described in one place and neither can quietly drift.
LEGACY_GRADE_FINGERPRINT_FIELDS = (
    "grade_display", "percentage", "status", "feedback",
)


def fingerprint_digest(payload: dict[str, Any], fields: tuple[str, ...]) -> str:
    """Return a stable hash over the change-relevant fields of *payload*."""
    data = {field: payload.get(field) for field in fields}
    text = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tag_fingerprint(digest: str, version: int) -> str:
    """Return *digest* tagged with its shape *version*.

    Version 1 is the original bare hex digest, written exactly as it was
    so the categories that never changed shape do not all look modified.
    Anything later carries a ``v<n>:`` prefix, which is what lets a stored
    value be recognised as older rather than as different.
    """
    return digest if version <= 1 else f"v{version}:{digest}"


def feedback_hash(feedback: Any) -> str:
    """Return the stored stand-in for one grade item's feedback text.

    Empty for no feedback, otherwise the leading
    :data:`FEEDBACK_HASH_CHARS` of its SHA-256. Two runs that see the same
    words produce the same value, so a feedback-only edit is still a
    detected grade change — without the words themselves ever reaching the
    cache, the change log, or a change event replayed to an agent.
    """
    text = str(feedback or "")
    if not text:
        return ""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return digest[:FEEDBACK_HASH_CHARS]


def feedback_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the two derived fields describing *payload*'s feedback."""
    text = str(payload.get("feedback") or "")
    return {"feedback_present": bool(text), "feedback_hash": feedback_hash(text)}


def scrub_feedback(payload: dict[str, Any]) -> dict[str, Any]:
    """Return *payload* with feedback text replaced by presence and a hash.

    Used for stored rows and stored change details — anything already on
    disk, where the text is not wanted whatever the caller asked for. The
    sync's own preparation is a shade more permissive: it adds the same
    two fields but keeps the text when ``--store-feedback`` was given.
    """
    scrubbed = dict(payload)
    scrubbed.update(feedback_fields(payload))
    scrubbed.pop("feedback", None)
    return scrubbed


def grade_fingerprint(payload: dict[str, Any]) -> str:
    """Return the current-shape fingerprint for one prepared grade payload.

    The single definition of "what a grade row's fingerprint is", so the
    migration cannot write one the next sync would disagree with.
    """
    return tag_fingerprint(
        fingerprint_digest(payload, GRADE_FINGERPRINT_FIELDS),
        GRADE_FINGERPRINT_VERSION,
    )


def legacy_grade_fingerprint(payload: dict[str, Any]) -> str:
    """Return the pre-v2 fingerprint of *payload*: a bare digest, old fields.

    The migration uses this to ask one exact question of a stored row —
    *is the feedback text in this payload the text its fingerprint was
    computed from?* An older Worsaga fingerprinted the record it had
    fetched and then stored a **sanitized** copy of it, so for a row whose
    text the sanitizer edited the two no longer correspond, and hashing
    the stored words would produce a value the next sync can never match.
    Recomputing over the stored payload answers that in one comparison,
    with no guessing about what edited text looks like.
    """
    return tag_fingerprint(
        fingerprint_digest(payload, LEGACY_GRADE_FINGERPRINT_FIELDS), 1,
    )


__all__ = [
    "FEEDBACK_HASH_CHARS",
    "GRADE_FINGERPRINT_FIELDS",
    "GRADE_FINGERPRINT_VERSION",
    "LEGACY_GRADE_FINGERPRINT_FIELDS",
    "feedback_fields",
    "feedback_hash",
    "fingerprint_digest",
    "grade_fingerprint",
    "legacy_grade_fingerprint",
    "scrub_feedback",
    "tag_fingerprint",
]
