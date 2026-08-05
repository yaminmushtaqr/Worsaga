"""Read-only Moodle API client.

This module is the ONLY permitted way to call the Moodle API.
Direct HTTP calls to Moodle bypassing this module are forbidden.

ENFORCEMENT: Any wsfunction not on the ALLOWED_FUNCTIONS allowlist will raise
MoodleWriteAttemptError before any network request is made. Any call that
names a user other than the authenticated one raises MoodleScopeError,
likewise before any request is made.

Permitted: read-only data fetching only.
Forbidden: submitting assignments, opening quizzes, posting, uploading, or any
           action that creates or modifies data on Moodle.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from worsaga.config import MoodleConfig

# ─────────────────────────────────────────────────────────────────
# ALLOWLIST — only these read-only functions may be called.
# To add a new function, it must be demonstrably read-only and documented here.
# ─────────────────────────────────────────────────────────────────
ALLOWED_FUNCTION_POLICIES = {
    "core_webservice_get_site_info": {
        "purpose": "connection check and authenticated user metadata",
        "changes_user_state": False,
        "exposed": False,
    },
    "core_enrol_get_users_courses": {
        "purpose": "list courses visible to the authenticated user",
        "changes_user_state": False,
        "exposed": True,
    },
    # core_enrol_get_enrolled_users is deliberately NOT allowlisted: it
    # exposes third-party PII (other students' names and profiles) and no
    # current feature needs it. Re-add only alongside a real feature and
    # a privacy review.
    "core_course_get_courses": {
        "purpose": "read course metadata",
        "changes_user_state": False,
        "exposed": False,
    },
    "core_course_get_contents": {
        "purpose": "read course sections, modules, and material metadata",
        "changes_user_state": False,
        "exposed": True,
    },
    "core_calendar_get_calendar_events": {
        "purpose": "read calendar events",
        "changes_user_state": False,
        "exposed": True,
    },
    "core_calendar_get_action_events_by_timesort": {
        "purpose": "read action-oriented calendar events by time window",
        "changes_user_state": False,
        "exposed": True,
    },
    "mod_assign_get_assignments": {
        "purpose": "read assignment definitions and due dates",
        "changes_user_state": False,
        "exposed": True,
    },
    "mod_assign_get_submission_status": {
        "purpose": "read authenticated user's assignment submission status",
        "changes_user_state": False,
        "exposed": True,
    },
    # mod_assign_get_grades is deliberately NOT allowlisted: its response
    # can include other students' grades for teacher-capable tokens, and
    # the authenticated user's own grades/feedback already come from
    # mod_assign_get_submission_status and gradereport_user_get_grade_items.
    # Re-add only alongside a real feature and a privacy review.
    "gradereport_user_get_grade_items": {
        "purpose": "read gradebook items for a user/course",
        "changes_user_state": False,
        "exposed": True,
    },
    # core_grades_get_grades is deliberately NOT allowlisted: it takes a
    # userids list, so a teacher-capable token could read other students'
    # grades through it. The authenticated user's own gradebook already
    # comes from gradereport_user_get_grade_items. Re-add only alongside a
    # real feature and a privacy review.
    "mod_forum_get_forums_by_courses": {
        "purpose": "read forum containers in courses",
        "changes_user_state": False,
        "exposed": True,
    },
    "mod_forum_get_forum_discussions": {
        "purpose": "read forum discussion metadata without marking views",
        "changes_user_state": False,
        "exposed": True,
    },
    "mod_quiz_get_quizzes_by_courses": {
        "purpose": "read quiz due dates for deadline aggregation",
        "changes_user_state": False,
        "exposed": True,
    },
    "message_popup_get_popup_notifications": {
        "purpose": "read popup notifications without marking them read",
        "changes_user_state": False,
        "exposed": True,
    },
    "core_message_get_messages": {
        "purpose": "read messages visible to the authenticated user",
        "changes_user_state": False,
        "exposed": True,
    },
}

ALLOWED_FUNCTIONS = frozenset(ALLOWED_FUNCTION_POLICIES)

# Belt-and-suspenders: block any function matching these patterns
# even if someone adds them to ALLOWED_FUNCTIONS accidentally.
BLOCKED_PATTERNS = [
    "submit", "save_submission", "upload", "post", "create", "update",
    "delete", "add_", "lock", "unlock", "grade_submission", "send",
    "attempt", "start_attempt", "process_attempt", "view_",
]

# ─────────────────────────────────────────────────────────────────
# SELF-SCOPE — an allowlisted function that names a user may only
# ever name the authenticated one.
# ─────────────────────────────────────────────────────────────────
#: Maps an allowlisted wsfunction to the request parameter naming the user
#: whose data comes back. The parameter is injected when a caller omits it,
#: so no route through :meth:`MoodleClient.call` — wrapper method or raw
#: call — can read another person's grades, courses, or messages, even with
#: a token carrying teacher or admin capabilities.
#:
#: Only functions whose wrappers already send the parameter are listed.
#: Moodle rejects unexpected keys outright, so injecting one that a given
#: site's version does not accept would break an otherwise working call.
#: Phase 0A formalises this into a full per-function parameter policy that
#: also covers the optional user-identity arguments on the remaining
#: allowlisted functions (for example mod_assign_get_submission_status).
SELF_SCOPED_PARAMS = {
    "core_enrol_get_users_courses": "userid",
    "gradereport_user_get_grade_items": "userid",
    "core_message_get_messages": "useridto",
    "message_popup_get_popup_notifications": "useridto",
}

#: Parameter names that identify whose data is being read. Checked on
#: *every* allowlisted call, so naming someone else is refused even on a
#: function that is not in the injection map above. Deliberately excludes
#: ``useridfrom``, which is a sender filter (0 means "anyone"), not a claim
#: about whose mailbox is being read.
IDENTITY_PARAMS = frozenset(SELF_SCOPED_PARAMS.values())


class MoodleWriteAttemptError(PermissionError):
    """Raised when code tries to call a non-read-only Moodle function."""


class MoodleScopeError(MoodleWriteAttemptError):
    """Raised when a call would request a user other than the authenticated one.

    Subclasses :class:`MoodleWriteAttemptError` deliberately. Both are
    pre-network refusals of a call the safety model forbids, and every
    orchestrator already re-raises ``MoodleWriteAttemptError`` instead of
    degrading it into a per-course "no access" warning. A scope violation
    is a bug or an attack, never a course this account cannot see, so it
    needs exactly that non-swallowable treatment — inherited here rather
    than taught to each orchestrator separately.
    """


class MoodleRequestError(RuntimeError):
    """A Moodle web-service call returned an ``exception`` payload.

    Carries Moodle's stable ``errorcode`` (localisation-independent)
    alongside the human message so callers can classify failures without
    string-matching a translated message. The ``str()`` form keeps the
    historical ``"Moodle API error: ..."`` wording. Messages never contain
    tokens (Moodle does not echo the ``wstoken`` in exception payloads).
    """

    def __init__(self, message: str, *, errorcode: str = ""):
        super().__init__(message)
        self.errorcode = errorcode


class CourseNotFoundError(RuntimeError):
    """Raised when a course id is not enrolled or does not exist.

    Subclasses :class:`RuntimeError` so the CLI's top-level handler turns
    it into a clean ``Error: ...`` exit rather than a traceback, replacing
    the raw Moodle DB wording ("Can't find data record in database table
    course."). MCP tools catch it and return a structured
    ``{"error", "error_code": "course_not_found"}`` dict.
    """

    def __init__(self, course_id: int | str, message: str | None = None):
        self.course_id = course_id
        super().__init__(
            message
            or f"Course {course_id} not found (not enrolled or does not exist)."
        )


class AssignmentNotFoundError(ValueError):
    """Raised when an assignment id does not exist or is not accessible.

    Subclasses :class:`ValueError` (as the historical
    ``get_assignment_status`` failure did) so existing callers and the
    CLI's top-level handler are unaffected. MCP tools catch it and return
    a structured ``{"error", "error_code": "assignment_not_found"}`` dict.
    """

    def __init__(
        self,
        assignment_id: int | str,
        *,
        course_id: int | str | None = None,
        message: str | None = None,
    ):
        self.assignment_id = assignment_id
        self.course_id = course_id
        if message is None:
            if course_id is not None:
                message = (
                    f"No assignment {assignment_id} found in course {course_id}."
                )
            else:
                message = (
                    f"Assignment {assignment_id} not found "
                    "(does not exist or not accessible)."
                )
        super().__init__(message)


# Moodle "missing record" / invalid-id error signatures. Moodle localises
# the human message but keeps the errorcode stable, so match on both. These
# indicate the requested course/assignment/module simply does not exist for
# this user — distinct from auth failures, which must keep raising.
_NOT_FOUND_ERRORCODES = frozenset({
    "invalidrecord", "invalidrecordunknown", "invalidcourseid",
    "coursedoesnotexist", "invalidcourse", "invalidcoursemodule",
    "invalidassignment", "notenrolled",
})


def _is_missing_record_error(exc: BaseException) -> bool:
    """Return True when *exc* is a Moodle "record not found" style failure."""
    code = str(getattr(exc, "errorcode", "") or "").lower()
    if code in _NOT_FOUND_ERRORCODES:
        return True
    message = str(exc).lower()
    return (
        "can't find data record" in message
        or "cannot find data record" in message
        or "invalid course id" in message
    )


# Conservative cap on any single file download. Files above this size are
# skipped with a structured error — never silently truncated.
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


class DownloadError(RuntimeError):
    """A categorized, token-free download failure.

    ``code`` is one of:

    - ``"auth"`` — Moodle rejected the credentials (HTTP 401/403).
    - ``"not_found"`` — the file does not exist (HTTP 404).
    - ``"network"`` — connection, timeout, or other HTTP failure.
    - ``"oversize"`` — the file exceeds the download size limit.
    - ``"invalid_url"`` — the URL failed Moodle-origin validation.
    - ``"empty"`` — the server returned no data.

    Messages never contain tokens or authenticated URLs.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


#: Advertised in the User-Agent so a Moodle administrator who sees the
#: traffic can identify the client and reach its maintainers. Must stay in
#: sync with the Repository URL in pyproject.toml.
PROJECT_URL = "https://github.com/yaminmushtaqr/worsaga"


def _user_agent() -> str:
    from worsaga import __version__

    return f"worsaga/{__version__} (+{PROJECT_URL})"


def _download_display_name(fileurl: str) -> str:
    """Return a token-free display name (URL path tail) for error text."""
    path = urllib.parse.urlparse(str(fileurl or "")).path
    tail = urllib.parse.unquote(path.rsplit("/", 1)[-1]) if path else ""
    return tail or "(file)"


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
        allowlist or matches a blocked pattern, and MoodleScopeError if it
        names a user other than the authenticated one. Both checks run
        before any request is built.
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

        # 3. Self-scope. Checked against whatever the caller passed, then
        # filled in when it was omitted, so the wire parameters always
        # name the authenticated user.
        for key, value in params.items():
            if key in IDENTITY_PARAMS and str(value) != str(self.userid):
                raise MoodleScopeError(
                    f"BLOCKED: '{wsfunction}' was called with {key}={value}, "
                    f"which is not the authenticated user ({self.userid}). "
                    "Worsaga only ever reads the authenticated user's own "
                    "data. This call has been prevented."
                )
        scoped_param = SELF_SCOPED_PARAMS.get(wsfunction)
        if scoped_param is not None:
            params.setdefault(scoped_param, self.userid)

        # 4. Make the request
        params.update({
            "wstoken": self._config.token,
            "moodlewsrestformat": "json",
            "wsfunction": wsfunction,
        })
        data = urllib.parse.urlencode(params).encode()
        url = f"{self.base_url}/webservice/rest/server.php"
        req = urllib.request.Request(
            url, data=data, headers={"User-Agent": _user_agent()},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.load(r)

        if isinstance(result, dict) and "exception" in result:
            raise MoodleRequestError(
                f"Moodle API error: {result.get('message', result)}",
                errorcode=str(result.get("errorcode") or ""),
            )

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

    def get_assignment_submission_status(self, assignment_id: int) -> dict:
        """Return submission status for one assignment.

        Raises :class:`AssignmentNotFoundError` when Moodle reports the
        assignment id does not exist, so callers get a friendly,
        classifiable failure instead of raw DB wording.
        """
        try:
            return self.call(
                "mod_assign_get_submission_status", assignid=assignment_id,
            )
        except MoodleRequestError as exc:
            if _is_missing_record_error(exc):
                raise AssignmentNotFoundError(assignment_id) from None
            raise

    def get_user_grade_items(self, course_id: int) -> dict:
        """Return the authenticated user's gradebook items for a course.

        Self-only by construction: there is no user-id parameter, so no
        caller can aim this at another student even with a token that
        carries teacher capabilities.

        Raises :class:`CourseNotFoundError` when Moodle reports the course
        id does not exist for this user.
        """
        try:
            return self.call(
                "gradereport_user_get_grade_items",
                courseid=course_id,
                userid=self.userid,
            )
        except MoodleRequestError as exc:
            if _is_missing_record_error(exc):
                raise CourseNotFoundError(course_id) from None
            raise

    def get_quizzes(self, course_ids: list[int] | None = None) -> dict:
        """Return quizzes for the given courses (or all enrolled courses)."""
        if course_ids is None:
            course_ids = [c["id"] for c in self.get_courses()]
        params = {f"courseids[{i}]": cid for i, cid in enumerate(course_ids)}
        return self.call("mod_quiz_get_quizzes_by_courses", **params)

    def get_course_contents(self, course_id: int) -> list[dict]:
        """Return all sections (with modules) for a course.

        Raises :class:`CourseNotFoundError` when Moodle reports the course
        id does not exist or the user is not enrolled, so callers get a
        friendly, classifiable failure instead of raw DB wording.
        """
        try:
            return self.call("core_course_get_contents", courseid=course_id)
        except MoodleRequestError as exc:
            if _is_missing_record_error(exc):
                raise CourseNotFoundError(course_id) from None
            raise

    def get_forums_by_courses(self, course_ids: list[int]) -> dict:
        """Return forums for the given courses."""
        if not course_ids:
            return {"forums": []}
        params = {f"courseids[{i}]": cid for i, cid in enumerate(course_ids)}
        result = self.call("mod_forum_get_forums_by_courses", **params)
        if isinstance(result, list):
            return {"forums": result}
        if isinstance(result, dict):
            return result
        return {"forums": []}

    def get_forum_discussions(self, forum_id: int) -> dict:
        """Return discussions for a forum without marking it viewed."""
        return self.call("mod_forum_get_forum_discussions", forumid=forum_id)

    def get_popup_notifications(self, unread_only: bool = False) -> dict:
        """Return popup notifications without marking them read."""
        return self.call(
            "message_popup_get_popup_notifications",
            useridto=self.userid,
            newestfirst=1,
            limit=100,
            offset=0,
        )

    def get_messages(self, since_time: int | None = None) -> dict:
        """Return messages visible to the authenticated user."""
        params = {
            "useridto": self.userid,
            "useridfrom": 0,
            "type": "conversations",
            "read": 2,
            "newestfirst": 1,
            "limitfrom": 0,
            "limitnum": 100,
        }
        return self.call("core_message_get_messages", **params)

    def get_calendar_events(
        self,
        course_ids: list[int] | None = None,
        timestart: int | None = None,
        timeend: int | None = None,
    ) -> dict:
        """Return calendar events for courses and an optional time window."""
        params: dict[str, int] = {}
        if course_ids:
            params.update({
                f"events[courseids][{i}]": cid
                for i, cid in enumerate(course_ids)
            })
        if timestart is not None:
            params["options[timestart]"] = timestart
        if timeend is not None:
            params["options[timeend]"] = timeend
        return self.call("core_calendar_get_calendar_events", **params)

    def get_action_events_by_timesort(
        self,
        timesort_from: int,
        timesort_to: int,
        limit: int = 100,
    ) -> dict:
        """Return action calendar events by timesort window."""
        return self.call(
            "core_calendar_get_action_events_by_timesort",
            timesortfrom=timesort_from,
            timesortto=timesort_to,
            limitnum=max(1, min(50, limit)),
        )

    def download_file(
        self, fileurl: str, *, max_bytes: int | None = MAX_DOWNLOAD_BYTES,
    ) -> bytes:
        """Download a file from a Moodle file URL (read-only GET).

        Appends the session token and returns raw bytes. This is a plain
        HTTP GET — not a web-service call — so the allowlist is not
        checked (there is no wsfunction involved).

        Downloads are capped at *max_bytes* (default
        :data:`MAX_DOWNLOAD_BYTES`); an oversize file is a structured
        failure, never a silently truncated result. Pass
        ``max_bytes=None`` only when an uncapped read is intended.

        Raises
        ------
        DownloadError
            With a stable ``code`` (``auth``, ``not_found``, ``network``,
            ``oversize``, ``invalid_url``, ``empty``). Error messages
            never contain tokens or authenticated URLs.
        """
        name = _download_display_name(fileurl)
        try:
            url = self._authenticated_file_url(fileurl)
        except ValueError as exc:
            raise DownloadError("invalid_url", f"'{name}': {exc}") from None

        req = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
        # Chained exceptions are suppressed (``from None``) throughout:
        # urllib errors carry the authenticated URL, which must never
        # surface in messages or tracebacks.
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                if max_bytes is None:
                    data = r.read()
                else:
                    declared = r.headers.get("Content-Length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        raise DownloadError(
                            "oversize",
                            f"'{name}' is {int(declared)} bytes, over the "
                            f"{max_bytes} byte limit; skipped.",
                        )
                    data = r.read(max_bytes + 1)
                    if len(data) > max_bytes:
                        raise DownloadError(
                            "oversize",
                            f"'{name}' exceeds the {max_bytes} byte limit; "
                            "skipped (no partial file was written).",
                        )
        except DownloadError:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise DownloadError(
                    "auth",
                    f"Moodle refused the download of '{name}' "
                    f"(HTTP {exc.code}). The token may be invalid or expired.",
                ) from None
            if exc.code == 404:
                raise DownloadError(
                    "not_found", f"'{name}' was not found on Moodle (HTTP 404).",
                ) from None
            raise DownloadError(
                "network", f"download of '{name}' failed (HTTP {exc.code}).",
            ) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise DownloadError(
                "network", f"network request failed for '{name}' — {reason}",
            ) from None
        except (TimeoutError, OSError):
            raise DownloadError(
                "network", f"network request failed for '{name}'.",
            ) from None

        if not data:
            raise DownloadError("empty", f"server returned no data for '{name}'.")
        return data

    def _authenticated_file_url(self, fileurl: str) -> str:
        """Return a Moodle-local pluginfile URL with this client's token.

        Moodle may expose arbitrary URL/page resources in course contents.
        Only pluginfile-style URLs from the configured Moodle origin should
        receive the token used for authenticated file downloads.
        """
        raw = str(fileurl or "").strip()
        if not raw:
            raise ValueError("file URL is empty")

        base = urllib.parse.urlparse(self.base_url)
        target = urllib.parse.urlparse(raw)
        if not target.scheme and not target.netloc:
            target = urllib.parse.urlparse(urllib.parse.urljoin(f"{self.base_url}/", raw))

        if target.scheme.lower() not in {"http", "https"}:
            raise ValueError("file URL must be HTTP(S)")
        if target.scheme.lower() != base.scheme.lower():
            raise ValueError("file URL scheme does not match Moodle base URL")
        if target.netloc.lower() != base.netloc.lower():
            raise ValueError("file URL host does not match Moodle base URL")

        base_path = base.path.rstrip("/")
        if base_path and not (
            target.path == base_path or target.path.startswith(f"{base_path}/")
        ):
            raise ValueError("file URL path is outside Moodle base path")

        path_parts = {part.lower() for part in target.path.split("/") if part}
        if not ({"pluginfile.php", "draftfile.php"} & path_parts):
            raise ValueError("file URL is not a Moodle file endpoint")

        query = [
            (key, value)
            for key, value in urllib.parse.parse_qsl(
                target.query, keep_blank_values=True,
            )
            if key.lower() not in {"token", "wstoken"}
        ]
        query.append(("token", self._config.token))
        return urllib.parse.urlunparse(
            target._replace(query=urllib.parse.urlencode(query))
        )
