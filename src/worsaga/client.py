"""Read-only Moodle API client.

This module is the ONLY permitted way to call the Moodle API.
Direct HTTP calls to Moodle bypassing this module are forbidden.

ENFORCEMENT: Any wsfunction not on the ALLOWED_FUNCTIONS allowlist will raise
MoodleWriteAttemptError before any network request is made. Any call that
names a user other than the authenticated one raises MoodleScopeError, any
call carrying a request parameter the function's policy does not list raises
MoodleParameterError, and any read aimed at a course the authenticated user
is not enrolled in raises CourseNotFoundError — all before any request is
made, and all in the dispatcher itself rather than in the wrappers, so a raw
``call()`` is bound by them too.

Child-object ids (an assignment id, a forum id) are the one scope rule the
dispatcher cannot enforce: it would have to fetch the parent course's object
list to judge them. They are checked by the orchestrators that already hold
that list (:mod:`worsaga.assignments`, :mod:`worsaga.forums`), which is the
path every CLI command and MCP tool takes.

Permitted: read-only data fetching only.
Forbidden: submitting assignments, opening quizzes, posting, uploading, or any
           action that creates or modifies data on Moodle.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

from worsaga.config import MoodleConfig

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# ALLOWLIST — only these read-only functions may be called.
# To add a new function, it must be demonstrably read-only and documented here.
#
# Every entry carries the same four keys:
#
# - ``purpose`` — why Worsaga needs it, in one line.
# - ``changes_user_state`` — always False; a True entry does not belong here.
# - ``exposed`` — False marks a function that is an internal mechanism of
#   :class:`MoodleClient` rather than part of its surface. Only this
#   module's own wrapper method may reach it; the public :meth:`
#   MoodleClient.call` refuses it like any un-allowlisted name.
# - ``params`` — the complete set of request parameter names Worsaga may
#   send for that function, derived from the wrapper that calls it. Anything
#   else is refused before the request is built, so no route through
#   ``call()`` can widen a request beyond what the feature needs. Moodle's
#   array arguments (``courseids[0]``, ``events[courseids][0]``) are listed
#   once under their base name; see :func:`_param_base_name`.
#
# Two optional keys carry the scope rules:
#
# - ``injects`` — the identity parameter filled in when a caller omits it
#   (see SELF_SCOPED_PARAMS below).
# - ``course_params`` — the parameters whose values are course ids. Every
#   such value is checked against the enrolment set before the request, so
#   course scope holds at the dispatcher and not only in the wrappers.
#
# Both are always a subset of ``params``; functions with neither omit them.
# ─────────────────────────────────────────────────────────────────
ALLOWED_FUNCTION_POLICIES = {
    "core_webservice_get_site_info": {
        "purpose": "connection check and authenticated user metadata",
        "changes_user_state": False,
        "exposed": False,
        "params": frozenset(),
    },
    "core_enrol_get_users_courses": {
        "purpose": "list courses visible to the authenticated user",
        "changes_user_state": False,
        "exposed": True,
        "params": frozenset({"userid"}),
        "injects": "userid",
    },
    # core_enrol_get_enrolled_users is deliberately NOT allowlisted: it
    # exposes third-party PII (other students' names and profiles) and no
    # current feature needs it. Re-add only alongside a real feature and
    # a privacy review.
    #
    # core_course_get_courses is deliberately NOT allowlisted: it reads
    # course metadata by arbitrary id, so it can describe courses this
    # account is not enrolled in. Enrolment-scoped discovery through
    # core_enrol_get_users_courses is the sanctioned path and is what
    # every feature already uses. Re-add only alongside a real feature and
    # a privacy review.
    "core_course_get_contents": {
        "purpose": "read course sections, modules, and material metadata",
        "changes_user_state": False,
        "exposed": True,
        "params": frozenset({"courseid"}),
        "course_params": frozenset({"courseid"}),
    },
    "core_calendar_get_calendar_events": {
        "purpose": "read calendar events",
        "changes_user_state": False,
        "exposed": True,
        "params": frozenset({
            "events[courseids]", "options[timestart]", "options[timeend]",
        }),
        "course_params": frozenset({"events[courseids]"}),
    },
    "core_calendar_get_action_events_by_timesort": {
        "purpose": "read action-oriented calendar events by time window",
        "changes_user_state": False,
        "exposed": True,
        "params": frozenset({"timesortfrom", "timesortto", "limitnum"}),
    },
    "mod_assign_get_assignments": {
        "purpose": "read assignment definitions and due dates",
        "changes_user_state": False,
        "exposed": True,
        "params": frozenset({"courseids"}),
        "course_params": frozenset({"courseids"}),
    },
    "mod_assign_get_submission_status": {
        "purpose": "read authenticated user's assignment submission status",
        "changes_user_state": False,
        "exposed": True,
        # Current Moodle also accepts an optional userid here. Worsaga never
        # sends one — omitting it means "the token's own user" — so it is
        # not in the policy and a caller cannot add it.
        "params": frozenset({"assignid"}),
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
        "params": frozenset({"courseid", "userid"}),
        "injects": "userid",
        "course_params": frozenset({"courseid"}),
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
        "params": frozenset({"courseids"}),
        "course_params": frozenset({"courseids"}),
    },
    "mod_forum_get_forum_discussions": {
        "purpose": "read forum discussion metadata without marking views",
        "changes_user_state": False,
        "exposed": True,
        "params": frozenset({"forumid"}),
    },
    "mod_quiz_get_quizzes_by_courses": {
        "purpose": "read quiz due dates for deadline aggregation",
        "changes_user_state": False,
        "exposed": True,
        "params": frozenset({"courseids"}),
        "course_params": frozenset({"courseids"}),
    },
    "message_popup_get_popup_notifications": {
        "purpose": "read popup notifications without marking them read",
        "changes_user_state": False,
        "exposed": True,
        "params": frozenset({"useridto", "newestfirst", "limit", "offset"}),
        "injects": "useridto",
    },
    "core_message_get_messages": {
        "purpose": "read messages visible to the authenticated user",
        "changes_user_state": False,
        "exposed": True,
        "params": frozenset({
            "useridto", "useridfrom", "type", "read", "newestfirst",
            "limitfrom", "limitnum",
        }),
        "injects": "useridto",
    },
}

ALLOWED_FUNCTIONS = frozenset(ALLOWED_FUNCTION_POLICIES)

#: Moodle takes list arguments as ``courseids[0]``, ``courseids[1]``, ...
#: (and nested, as ``events[courseids][0]``). The parameter policy lists
#: each such argument once under its base name.
_ARRAY_INDEX = re.compile(r"\[\d+\]$")


def _param_base_name(name: str) -> str:
    """Return a request parameter's policy name, dropping an array index."""
    return _ARRAY_INDEX.sub("", name)


def _refuse_unknown_params(wsfunction: str, unknown: set[str]) -> None:
    """Raise :class:`MoodleParameterError` for any *unknown* parameter."""
    if not unknown:
        return
    raise MoodleParameterError(
        f"BLOCKED: '{wsfunction}' was called with parameter(s) "
        f"{', '.join(sorted(unknown))}, which are not part of what Worsaga "
        "sends for this function. This call has been prevented."
    )

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
#: whose data comes back, read from each policy's ``injects`` key. The
#: parameter is injected when a caller omits it, so no route through
#: :meth:`MoodleClient.call` — wrapper method or raw call — can read another
#: person's grades, courses, or messages, even with a token carrying teacher
#: or admin capabilities. The value injected is the *verified* user id (see
#: :attr:`MoodleClient.userid`), never the configured hint.
#:
#: Only functions that accept the parameter on every supported Moodle
#: version are marked. Moodle rejects unexpected keys outright, so injecting
#: one that a given site's version does not accept would break an otherwise
#: working call; those functions are still covered by IDENTITY_PARAMS
#: validation below and by their own ``params`` policy.
SELF_SCOPED_PARAMS = {
    wsfunction: policy["injects"]
    for wsfunction, policy in ALLOWED_FUNCTION_POLICIES.items()
    if policy.get("injects")
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


class MoodleParameterError(MoodleWriteAttemptError):
    """Raised when a call carries a parameter the function's policy omits.

    Subclasses :class:`MoodleWriteAttemptError` for the same reason
    :class:`MoodleScopeError` does: it is a pre-network refusal of a call
    the safety model forbids, so it must reach the caller intact rather than
    being degraded into a per-course "no access" warning. An unlisted
    parameter is a bug or an attempt to widen a request, never a course
    this account cannot see.
    """


class MoodleRequestError(RuntimeError):
    """A Moodle web-service call returned an ``exception`` payload.

    Carries Moodle's stable ``errorcode`` (localisation-independent)
    alongside the human message so callers can classify failures without
    string-matching a translated message. The ``str()`` form keeps the
    historical ``"Moodle API error: ..."`` wording.

    Both fields are built from text the *server* chose, so the client
    passes them through :meth:`MoodleClient._redact_token` first. Stock
    Moodle does not echo the ``wstoken`` in an exception payload, but a
    plugin, a reverse proxy, or a WAF that quotes the offending request
    can, and this message goes on to be printed, logged, and pasted into
    bug reports.
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


class ForumNotFoundError(ValueError):
    """Raised when a forum id is not one of the course's own forums.

    Subclasses :class:`ValueError` exactly as
    :class:`AssignmentNotFoundError` does, so the CLI's top-level handler
    turns it into a clean ``Error:`` exit; MCP tools catch it and return
    ``{"error", "error_code": "forum_not_found"}``. The course's forum list
    is already fetched on this path, so the membership check costs no extra
    request and an unknown forum id is never probed against the server.
    """

    def __init__(
        self,
        forum_id: int | str,
        *,
        course_id: int | str | None = None,
        message: str | None = None,
    ):
        self.forum_id = forum_id
        self.course_id = course_id
        if message is None:
            if course_id is not None:
                message = f"No forum {forum_id} found in course {course_id}."
            else:
                message = (
                    f"Forum {forum_id} not found "
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


def _as_course_id(value: object) -> int:
    """Return *value* as a course id, or 0 when it is not one."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _course_id_set(courses: list[dict]) -> frozenset[int]:
    """Return the non-zero course ids from a raw Moodle course list."""
    return frozenset(
        course_id
        for course_id in (_as_course_id(c.get("id")) for c in courses)
        if course_id
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


def _effective_origin(parsed: urllib.parse.ParseResult) -> tuple[str, str, int]:
    """Return ``(scheme, host, port)`` with a default port made explicit.

    The configured base URL is stored canonically, which drops an explicit
    ``:443``/``:80``, while a Moodle instance may still emit file URLs that
    carry one. Comparing raw netlocs would read that as a different origin
    and refuse the site's own downloads.
    """
    scheme = parsed.scheme.lower()
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, (parsed.hostname or "").lower(), port


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
        # The metadata fan-outs share one client across threads, so both
        # memos below are single-flight behind their own lock. The locks are
        # never held nested: identity resolution only ever calls site-info
        # (which carries no identity or course parameter), and the enrolment
        # memo only ever calls the course list.
        self._identity_lock = threading.Lock()
        self._site_info: dict | None = None
        self._userid: int | None = None
        self._enrolment_lock = threading.Lock()
        self._enrolled_ids: frozenset[int] | None = None

    @property
    def base_url(self) -> str:
        return self._config.url.rstrip("/")

    @property
    def userid(self) -> int:
        """The authenticated user's id, as the Moodle site itself reports it.

        Read from ``core_webservice_get_site_info`` the first time an
        identity-bearing value is needed, then memoised for the life of the
        client — at most one extra request per client instance (in practice
        one per command), the same call the official Moodle app makes at
        startup.

        A configured ``userid`` (constructor, ``WORSAGA_USERID``, or the
        credentials file) is only a hint. If it disagrees with the site, the
        site wins and one warning names both values; the configured value is
        never sent on the wire. A token whose real owner differs from the
        configured id therefore still reads only its own data. If site-info
        cannot be fetched the triggering call fails — there is no fallback
        to the unverified hint.
        """
        with self._identity_lock:
            if self._userid is None:
                # Captured at fetch time inside _site_info_locked, so the
                # value never depends on the payload staying unmodified.
                self._site_info_locked()
            if self._userid is None:
                raise MoodleScopeError(
                    "BLOCKED: Moodle did not report an authenticated user id "
                    "for this token, so Worsaga cannot establish whose data a "
                    "request would read. This call has been prevented."
                )
            return self._userid

    def _redact_token(self, text: str) -> str:
        """Replace this client's token with ``***`` in server-supplied text.

        Applied where a Moodle error payload becomes an exception
        message. The token travels through ``urlencode`` on the way out,
        so a server quoting the request back can echo either the raw
        value or a percent-encoded one; both forms are matched.

        Deliberately narrow: this covers the one place server text
        crosses into Worsaga's own error strings. Redaction at every
        output boundary is separate, later work — this is not a claim
        that no other path could ever carry the value.
        """
        token = self._config.token
        if not token:
            return text
        cleaned = text.replace(token, "***")
        for encoded in (
            urllib.parse.quote_plus(token), urllib.parse.quote(token),
        ):
            if encoded != token:
                cleaned = cleaned.replace(encoded, "***")
        return cleaned

    @property
    def verified_userid(self) -> int | None:
        """The verified user id if it is already known, else ``None``.

        Never makes a request — unlike :attr:`userid`, which fetches on
        first use. Read paths that promise not to contact Moodle (the
        local text search) use this to apply the account-binding check
        when the identity happens to be known already, and to skip it
        silently when it is not.
        """
        with self._identity_lock:
            return self._userid

    def site_info(self) -> dict:
        """Return a copy of this client's ``core_webservice_get_site_info``.

        Fetched at most once per client instance and shared between the
        identity check and the connection-info/doctor surfaces, so a client
        never makes the call twice. Failures are not memoised.

        The return value is a deep copy: the memoised payload is the client's
        own record of who it is authenticated as, and handing out a mutable
        reference to it would let a caller rewrite the identity used for
        every later request.
        """
        with self._identity_lock:
            return copy.deepcopy(self._site_info_locked())

    def _site_info_locked(self) -> dict:
        """Fetch-and-memoise site info. Caller must hold ``_identity_lock``."""
        if self._site_info is None:
            result = self._call("core_webservice_get_site_info", {}, internal=True)
            self._site_info = result if isinstance(result, dict) else {}
            self._capture_userid(self._site_info)
        return self._site_info

    def _capture_userid(self, info: dict) -> None:
        """Record the site's user id at fetch time, before it is exposed.

        Deliberately silent when the payload carries no usable id: the
        connection check reports a site fine without one, while any call
        that actually needs an identity raises from :attr:`userid`.
        """
        try:
            verified = int(info.get("userid") or 0)
        except (TypeError, ValueError):
            return
        if verified <= 0:
            return
        configured = self._config.userid
        if configured and configured != verified:
            logger.warning(
                "Configured Moodle user id %s does not match the "
                "authenticated user id %s reported by the site; using "
                "the site's value (%s).",
                configured, verified, verified,
            )
        self._userid = verified

    def enrolled_course_ids(self) -> frozenset[int]:
        """Return the ids of the courses the authenticated user is enrolled in.

        Memoised per client instance, and refreshed by every explicit
        :meth:`get_courses` call — so a long-running ``watch`` picks up a new
        enrolment on its next cycle, while the many course-scoped reads
        inside one command pay nothing for the check.
        """
        with self._enrolment_lock:
            if self._enrolled_ids is None:
                self._enrolled_ids = _course_id_set(self._fetch_courses())
            return self._enrolled_ids

    def _require_enrolled(self, *course_ids: int | str) -> None:
        """Refuse a course-scoped read aimed outside the enrolment set.

        Runs before the request is built, so a course id this account is not
        enrolled in never reaches Moodle — not as a speculative probe, and
        not as a fabricated local record either.
        """
        enrolled = self.enrolled_course_ids()
        for course_id in course_ids:
            if _as_course_id(course_id) not in enrolled:
                raise CourseNotFoundError(course_id)

    def call(self, wsfunction: str, **params) -> dict | list:
        """Call a Moodle web-service function (read-only only).

        Raises MoodleWriteAttemptError if the function is not on the
        allowlist, matches a blocked pattern, or is internal to this client;
        MoodleParameterError if it carries a request parameter the function's
        policy does not list; MoodleScopeError if it names a user other than
        the authenticated one; and CourseNotFoundError if it names a course
        this user is not enrolled in. Every check runs before any request is
        built.
        """
        return self._call(wsfunction, params)

    def _call(
        self, wsfunction: str, params: dict, *, internal: bool = False,
    ) -> dict | list:
        """Run every safety check, then issue the request.

        The checks are ordered so a call that is structurally wrong costs no
        network at all: the parameter policy is settled first, and only then
        do the scope rules resolve the values they need. The one call that
        does go out ahead of a refusal is site-info, when a function's *own*
        identity parameter has to be compared against the authenticated user
        — that request is the identity mechanism itself, not the widened
        request being judged.
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
        policy = ALLOWED_FUNCTION_POLICIES.get(wsfunction)
        if policy is None:
            raise MoodleWriteAttemptError(
                f"BLOCKED: '{wsfunction}' is not on the Moodle read-only allowlist. "
                f"To add it, verify it is read-only and add it to ALLOWED_FUNCTIONS."
            )

        # 3. Exposure. A policy marked ``exposed: False`` is an internal
        # mechanism of this client, not part of the surface a caller drives.
        if not internal and not policy["exposed"]:
            raise MoodleWriteAttemptError(
                f"BLOCKED: '{wsfunction}' is internal to Worsaga's Moodle "
                "client and is not callable through call(). Use the "
                "MoodleClient method that wraps it."
            )

        # 4. Parameter policy. Only the arguments the wrapper for this
        # function actually needs may go on the wire — including identity
        # arguments the function accepts but Worsaga never sends, which are
        # refused here whatever value they carry, with no network touched.
        _refuse_unknown_params(wsfunction, {
            key for key in params
            if _param_base_name(key) not in policy["params"]
        })

        # 5. Self-scope. Checked against whatever the caller passed, then
        # filled in below when it was omitted, so the wire parameters always
        # name the authenticated user. The verified id is only resolved for
        # a function that carries an identity parameter — site-info carries
        # none, which is what keeps verification from re-entering itself.
        identity_keys = [key for key in params if key in IDENTITY_PARAMS]
        scoped_param = SELF_SCOPED_PARAMS.get(wsfunction)
        userid = self.userid if (identity_keys or scoped_param) else 0
        for key in identity_keys:
            if str(params[key]) != str(userid):
                raise MoodleScopeError(
                    f"BLOCKED: '{wsfunction}' was called with "
                    f"{key}={params[key]}, which is not the authenticated "
                    f"user ({userid}). Worsaga only ever reads the "
                    "authenticated user's own data. This call has been "
                    "prevented."
                )

        # 6. Course scope, at the dispatcher rather than only in the
        # wrappers, so a raw call() cannot aim a course-bearing function at a
        # course this user is not enrolled in. No recursion: the enrolment
        # list itself comes from a function with no course parameter.
        if not internal:
            course_params = policy.get("course_params")
            if course_params:
                self._require_enrolled(*(
                    value for key, value in params.items()
                    if _param_base_name(key) in course_params
                ))

        if scoped_param is not None:
            params.setdefault(scoped_param, userid)

        # 7. Make the request
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
                "Moodle API error: "
                + self._redact_token(str(result.get("message", result))),
                errorcode=self._redact_token(
                    str(result.get("errorcode") or "")
                ),
            )

        return result

    # ── Convenience read-only methods ──────────────────────────────

    def get_courses(self) -> list[dict]:
        """Return all courses the authenticated user is enrolled in.

        Also refreshes the enrolment memo every course-scoped read is
        checked against, so a long-running loop that re-lists courses each
        cycle keeps its scope boundary current.

        The fetch and the commit happen under one lock. Committing outside it
        would let two concurrent refreshes land out of order and an older
        answer overwrite a newer one — which, for a course that had just been
        revoked, would re-authorise it.
        """
        with self._enrolment_lock:
            courses = self._fetch_courses()
            self._enrolled_ids = _course_id_set(courses)
            return courses

    def _fetch_courses(self) -> list[dict]:
        """Fetch the enrolled-course list, normalised to a list."""
        result = self.call("core_enrol_get_users_courses")
        return result if isinstance(result, list) else []

    def get_assignments(self, course_id: int) -> dict:
        """Return assignments for a single course."""
        self._require_enrolled(course_id)
        return self.call("mod_assign_get_assignments", **{"courseids[0]": course_id})

    def get_assignments_by_courses(self, course_ids: list[int]) -> dict:
        """Return assignments for the given courses in one batched call.

        Uses the array form of ``mod_assign_get_assignments`` to avoid the
        N round-trips of calling :meth:`get_assignments` per course.
        """
        if not course_ids:
            return {"courses": []}
        self._require_enrolled(*course_ids)
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

        Raises :class:`CourseNotFoundError` when the course is not one the
        authenticated user is enrolled in (checked before the request), or
        when Moodle reports the id does not exist for this user.
        """
        self._require_enrolled(course_id)
        try:
            return self.call(
                "gradereport_user_get_grade_items",
                courseid=course_id,
            )
        except MoodleRequestError as exc:
            if _is_missing_record_error(exc):
                raise CourseNotFoundError(course_id) from None
            raise

    def get_quizzes(self, course_ids: list[int] | None = None) -> dict:
        """Return quizzes for the given courses (or all enrolled courses)."""
        if course_ids is None:
            course_ids = [c["id"] for c in self.get_courses()]
        else:
            self._require_enrolled(*course_ids)
        params = {f"courseids[{i}]": cid for i, cid in enumerate(course_ids)}
        return self.call("mod_quiz_get_quizzes_by_courses", **params)

    def get_course_contents(self, course_id: int) -> list[dict]:
        """Return all sections (with modules) for a course.

        Raises :class:`CourseNotFoundError` when the course is not one the
        authenticated user is enrolled in (checked before the request), or
        when Moodle reports the id does not exist, so callers get a
        friendly, classifiable failure instead of raw DB wording.
        """
        self._require_enrolled(course_id)
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
        self._require_enrolled(*course_ids)
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
            newestfirst=1,
            limit=100,
            offset=0,
        )

    def get_messages(self, since_time: int | None = None) -> dict:
        """Return messages visible to the authenticated user."""
        params = {
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
            self._require_enrolled(*course_ids)
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
        try:
            target_origin = _effective_origin(target)
        except ValueError:
            raise ValueError("file URL has an invalid port") from None
        base_origin = _effective_origin(base)
        if target_origin[0] != base_origin[0]:
            raise ValueError("file URL scheme does not match Moodle base URL")
        if target_origin[1:] != base_origin[1:]:
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
