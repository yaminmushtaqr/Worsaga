"""Read-only Moodle API client.

This module is the ONLY permitted way to call the Moodle API.
Direct HTTP calls to Moodle bypassing this module are forbidden.

ENFORCEMENT: Any wsfunction not on the ALLOWED_FUNCTIONS allowlist will raise
MoodleWriteAttemptError before any network request is made.

Permitted: read-only data fetching only.
Forbidden: submitting assignments, opening quizzes, posting, uploading, or any
           action that creates or modifies data on Moodle.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from worsaga.config import MoodleConfig

# ─────────────────────────────────────────────────────────────────
# ALLOWLIST — only these read-only functions may be called.
# To add a new function, it must be demonstrably read-only.
# ─────────────────────────────────────────────────────────────────
ALLOWED_FUNCTIONS = frozenset([
    "core_webservice_get_site_info",
    "core_enrol_get_users_courses",
    "core_enrol_get_enrolled_users",
    "core_course_get_courses",
    "core_course_get_contents",
    "core_calendar_get_calendar_events",
    "core_calendar_get_action_events_by_timesort",
    "mod_assign_get_assignments",
    "mod_assign_get_submission_status",
    "mod_assign_get_grades",
    "gradereport_user_get_grade_items",
    "core_grades_get_grades",
    "mod_forum_get_forums_by_courses",
    "mod_forum_get_forum_discussions",
    "mod_quiz_get_quizzes_by_courses",
    "message_popup_get_popup_notifications",
    "core_message_get_messages",
])

# Belt-and-suspenders: block any function matching these patterns
# even if someone adds them to ALLOWED_FUNCTIONS accidentally.
BLOCKED_PATTERNS = [
    "submit", "save_submission", "upload", "post", "create", "update",
    "delete", "add_", "lock", "unlock", "grade_submission", "send",
    "attempt", "start_attempt", "process_attempt",
]


class MoodleWriteAttemptError(PermissionError):
    """Raised when code tries to call a non-read-only Moodle function."""


class MoodleClient:
    """Read-only Moodle web-service client with enforced safeguards."""

    def __init__(self, config: MoodleConfig | None = None, **kwargs):
        """Create a client.

        Parameters
        ----------
        config : MoodleConfig, optional
            Pre-built config.  If omitted, ``MoodleConfig.load(**kwargs)``
            is called so you can pass ``url=``, ``token=``, ``creds_path=``
            etc. directly.
        """
        if config is None:
            config = MoodleConfig.load(**kwargs)
        self._config = config

    @property
    def base_url(self) -> str:
        return self._config.url.rstrip("/")

    @property
    def userid(self) -> int:
        return self._config.userid

    def call(self, wsfunction: str, **params) -> dict | list:
        """Call a Moodle web-service function (read-only only).

        Raises MoodleWriteAttemptError if the function is not on the
        allowlist or matches a blocked pattern.
        """
        fn = wsfunction.lower()

        # 1. Blocked patterns first (belt-and-suspenders)
        for pattern in BLOCKED_PATTERNS:
            if pattern in fn:
                raise MoodleWriteAttemptError(
                    f"BLOCKED: '{wsfunction}' matches blocked pattern '{pattern}'. "
                    f"Moodle is read-only. This call has been prevented."
                )

        # 2. Allowlist
        if wsfunction not in ALLOWED_FUNCTIONS:
            raise MoodleWriteAttemptError(
                f"BLOCKED: '{wsfunction}' is not on the Moodle read-only allowlist. "
                f"To add it, verify it is read-only and add it to ALLOWED_FUNCTIONS."
            )

        # 3. Make the request
        params.update({
            "wstoken": self._config.token,
            "moodlewsrestformat": "json",
            "wsfunction": wsfunction,
        })
        data = urllib.parse.urlencode(params).encode()
        url = f"{self.base_url}/webservice/rest/server.php"
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.load(r)

        if isinstance(result, dict) and "exception" in result:
            raise RuntimeError(f"Moodle API error: {result.get('message', result)}")

        return result

    # ── Convenience read-only methods ──────────────────────────────

    def get_courses(self) -> list[dict]:
        """Return all courses the authenticated user is enrolled in."""
        return self.call("core_enrol_get_users_courses", userid=self.userid)

    def get_assignments(self, course_id: int) -> dict:
        """Return assignments for a single course."""
        return self.call("mod_assign_get_assignments", **{"courseids[0]": course_id})

    def get_assignments_by_courses(self, course_ids: list[int]) -> dict:
        """Return assignments for the given courses in one batched call.

        Uses the array form of ``mod_assign_get_assignments`` to avoid the
        N round-trips of calling :meth:`get_assignments` per course.
        """
        if not course_ids:
            return {"courses": []}
        params = {f"courseids[{i}]": cid for i, cid in enumerate(course_ids)}
        return self.call("mod_assign_get_assignments", **params)

    def get_quizzes(self, course_ids: list[int] | None = None) -> dict:
        """Return quizzes for the given courses (or all enrolled courses)."""
        if course_ids is None:
            course_ids = [c["id"] for c in self.get_courses()]
        params = {f"courseids[{i}]": cid for i, cid in enumerate(course_ids)}
        return self.call("mod_quiz_get_quizzes_by_courses", **params)

    def get_course_contents(self, course_id: int) -> list[dict]:
        """Return all sections (with modules) for a course."""
        return self.call("core_course_get_contents", courseid=course_id)

    def download_file(
        self, fileurl: str, *, max_bytes: int | None = None,
    ) -> bytes | None:
        """Download a file from a Moodle file URL (read-only GET).

        Appends the session token and returns raw bytes, or None on failure.
        This is a plain HTTP GET — not a web-service call — so the allowlist
        is not checked (there is no wsfunction involved).

        By default the full response body is read. Callers may pass
        ``max_bytes`` explicitly when they intentionally want a capped read.
        """
        sep = "&" if "?" in fileurl else "?"
        url = f"{fileurl}{sep}token={self._config.token}"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read() if max_bytes is None else r.read(max_bytes)
        except Exception:
            return None
