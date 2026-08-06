"""Tests for the read-only Moodle client safeguards."""

import ast
import inspect
import threading
import urllib.error
import urllib.parse

import pytest
from unittest.mock import patch

import json

from worsaga.client import (
    ALLOWED_FUNCTION_POLICIES,
    ALLOWED_FUNCTIONS,
    BLOCKED_PATTERNS,
    IDENTITY_PARAMS,
    SELF_SCOPED_PARAMS,
    SERVICE_DISABLED_MESSAGE,
    AssignmentNotFoundError,
    CourseNotFoundError,
    DownloadError,
    MoodleClient,
    MoodleParameterError,
    MoodleRequestError,
    MoodleScopeError,
    MoodleServiceDisabledError,
    MoodleWriteAttemptError,
    _param_base_name,
    is_auth_error,
    is_service_disabled_error,
)
from worsaga.config import MoodleConfig


@pytest.fixture()
def client():
    """Client with dummy credentials — never hits the network."""
    cfg = MoodleConfig(url="https://moodle.example.com", token="fake", userid=1)
    return MoodleClient(config=cfg)


class _FakeResponse:
    def __init__(self, payload: bytes, headers: dict | None = None):
        self.payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size=None):
        if size is None:
            return self.payload
        return self.payload[:size]


def _dispatched_wsfunctions() -> set[str]:
    """Return every wsfunction literal actually dispatched by the client.

    Walks the client module's AST for ``self.call(...)`` / ``self._call(...)``
    and collects the first argument, so a name mentioned only in a docstring
    or a comment does not count as reachable.
    """
    import worsaga.client as client_module

    tree = ast.parse(inspect.getsource(client_module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in {"call", "_call"}:
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "self"):
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            if isinstance(node.args[0].value, str):
                names.add(node.args[0].value)
    return names


class _FakeTransport:
    """A ``urllib.request.urlopen`` stand-in for wire-level client tests.

    Before the call under test the client verifies its user id against
    ``core_webservice_get_site_info`` and, for a course-scoped read, checks
    the enrolment list from ``core_enrol_get_users_courses`` — so a
    wire-level fake has to answer both. Every request is recorded in
    :attr:`calls`, letting a test assert exactly what did (and did not) go
    out.
    """

    def __init__(self, *, userid: int = 1, courses=(), payload: bytes = b"{}"):
        self.userid = userid
        self.courses = list(courses)
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, req, timeout=30):
        params = urllib.parse.parse_qs(req.data.decode())
        wsfunction = (params.get("wsfunction") or [""])[0]
        self.calls.append((wsfunction, params))
        if wsfunction == "core_webservice_get_site_info":
            return _FakeResponse(json.dumps({"userid": self.userid}).encode())
        if wsfunction == "core_enrol_get_users_courses" and self.courses:
            return _FakeResponse(json.dumps(self.courses).encode())
        return _FakeResponse(self.payload)

    @property
    def functions(self) -> list[str]:
        return [name for name, _ in self.calls]

    def params_for(self, wsfunction: str) -> dict:
        """Return the last recorded request parameters for *wsfunction*."""
        for name, params in reversed(self.calls):
            if name == wsfunction:
                return params
        raise AssertionError(f"no {wsfunction} request was made")


# ── Allowlist enforcement ──────────────────────────────────────────


class TestAllowlist:
    def test_allowed_function_is_on_list(self):
        assert "core_enrol_get_users_courses" in ALLOWED_FUNCTIONS

    def test_allowed_functions_have_policy_metadata(self):
        assert set(ALLOWED_FUNCTIONS) == set(ALLOWED_FUNCTION_POLICIES)
        for fn, policy in ALLOWED_FUNCTION_POLICIES.items():
            assert policy["purpose"]
            assert policy["changes_user_state"] is False
            assert isinstance(policy["exposed"], bool), fn
            assert isinstance(policy["params"], frozenset), fn
            assert all(isinstance(name, str) for name in policy["params"]), fn
            # The scope keys have to name parameters the function can carry,
            # or the rule they express is unreachable.
            assert set(policy.get("course_params", ())) <= policy["params"], fn
            injects = policy.get("injects")
            if injects is not None:
                assert injects in policy["params"], fn
                assert injects in IDENTITY_PARAMS, fn

    def test_every_allowed_function_is_reachable_from_a_wrapper(self):
        """No dead entries: each allowlisted name is used by a client method.

        Matched against the module's AST, not its text: a name that only
        appears in a docstring or comment is not reachable, and an entry no
        wrapper calls is exactly the kind of unused capability the allowlist
        exists to keep off the wire.
        """
        dispatched = _dispatched_wsfunctions()
        for fn in ALLOWED_FUNCTIONS:
            assert fn in dispatched, (
                f"{fn} is never passed to self.call()/self._call()"
            )

    def test_reachability_ignores_names_that_only_appear_in_text(self):
        # Guards the guard: the AST scan must not be satisfied by prose.
        assert "core_enrol_get_enrolled_users" not in _dispatched_wsfunctions()

    def test_unknown_function_is_blocked(self, client):
        with pytest.raises(MoodleWriteAttemptError, match="not on the Moodle read-only allowlist"):
            client.call("totally_unknown_function")

    def test_every_allowed_function_is_lowercase(self):
        for fn in ALLOWED_FUNCTIONS:
            assert fn == fn.lower(), f"{fn} should be lowercase"

    def test_enrolled_users_is_not_allowlisted(self, client):
        """core_enrol_get_enrolled_users exposes third-party PII and must
        stay off the allowlist until a real feature needs it."""
        assert "core_enrol_get_enrolled_users" not in ALLOWED_FUNCTIONS
        with pytest.raises(MoodleWriteAttemptError):
            client.call("core_enrol_get_enrolled_users", courseid=1)

    def test_call_sends_user_agent(self, client):
        seen = {}
        transport = _FakeTransport()

        def _fake_urlopen(req, timeout=30):
            seen["ua"] = req.get_header("User-agent")
            return transport(req, timeout)

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            client.call("core_enrol_get_users_courses", userid=1)

        from worsaga import __version__
        assert seen["ua"] == (
            f"worsaga/{__version__} (+https://github.com/yaminmushtaqr/worsaga)"
        )

    def test_user_agent_identifies_version_and_project(self):
        """Bot etiquette: an admin seeing the traffic can identify the
        client and reach its maintainers."""
        from worsaga import __version__
        from worsaga.client import PROJECT_URL, _user_agent

        ua = _user_agent()
        assert ua.startswith(f"worsaga/{__version__} ")
        assert f"(+{PROJECT_URL})" in ua
        assert PROJECT_URL.startswith("https://")


# ── Blocked-pattern enforcement ────────────────────────────────────


class TestBlockedPatterns:
    """Verify that write-like function names are rejected
    even if somehow present on the allowlist."""

    WRITE_FUNCTIONS = [
        "mod_assign_submit_grading_form",
        "mod_assign_save_submission",
        "core_files_upload",
        "mod_forum_add_discussion_post",
        "core_message_send_instant_messages",
        "mod_quiz_start_attempt",
        "mod_quiz_process_attempt",
        "core_calendar_create_calendar_events",
        "core_calendar_delete_calendar_events",
        "core_course_update_courses",
        "core_user_update_users",
        "mod_assign_lock_submissions",
        "mod_assign_unlock_submissions",
        "mod_assign_grade_submission",
        "mod_forum_view_forum",
        "mod_forum_view_forum_discussion",
    ]

    @pytest.mark.parametrize("fn", WRITE_FUNCTIONS)
    def test_write_function_blocked(self, client, fn):
        with pytest.raises(MoodleWriteAttemptError, match="BLOCKED"):
            client.call(fn)

    def test_blocked_patterns_are_nonempty(self):
        assert len(BLOCKED_PATTERNS) > 0

    def test_no_allowed_function_matches_blocked_pattern(self):
        """Sanity check: none of the allowlisted functions should
        match any blocked pattern."""
        for fn in ALLOWED_FUNCTIONS:
            fn_lower = fn.lower()
            for pattern in BLOCKED_PATTERNS:
                assert pattern not in fn_lower, (
                    f"Allowed function '{fn}' matches blocked pattern '{pattern}'"
                )


# ── Self-scope enforcement ─────────────────────────────────────────


_SCOPED = sorted(SELF_SCOPED_PARAMS.items())


class TestSelfScope:
    """A call may only ever name the authenticated user.

    The convenience wrappers no longer accept a user id, but ``call()`` is
    public and forwards arbitrary parameters, so the guarantee has to hold
    at the dispatcher itself.
    """

    @pytest.mark.parametrize("wsfunction,param", _SCOPED)
    def test_other_user_refused_before_any_request(self, client, wsfunction, param):
        transport = _FakeTransport()
        with patch("urllib.request.urlopen", side_effect=transport):
            with pytest.raises(MoodleScopeError, match="not the authenticated user"):
                client.call(wsfunction, **{param: 99})
        # Only the client's own identity check went out; the named-user
        # request never did.
        assert transport.functions == ["core_webservice_get_site_info"]

    @pytest.mark.parametrize("wsfunction,param", _SCOPED)
    def test_omitted_identity_param_is_injected(self, client, wsfunction, param):
        transport = _FakeTransport()
        with patch("urllib.request.urlopen", side_effect=transport):
            client.call(wsfunction)
        assert transport.params_for(wsfunction)[param] == [str(transport.userid)]

    @pytest.mark.parametrize("wsfunction,param", _SCOPED)
    def test_own_userid_is_accepted(self, client, wsfunction, param):
        transport = _FakeTransport()
        with patch("urllib.request.urlopen", side_effect=transport):
            client.call(wsfunction, **{param: transport.userid})
        assert transport.params_for(wsfunction)[param] == [str(transport.userid)]

    def test_own_userid_accepted_as_string(self, client):
        # Moodle params travel as strings; the comparison must not reject
        # the authenticated user just because the caller passed "1".
        transport = _FakeTransport(courses=[{"id": 10}])
        with patch("urllib.request.urlopen", side_effect=transport):
            client.call(
                "gradereport_user_get_grade_items",
                courseid=10,
                userid=str(transport.userid),
            )
        params = transport.params_for("gradereport_user_get_grade_items")
        assert params["userid"] == [str(transport.userid)]

    def test_identity_param_refused_on_unmapped_function_too(self, client):
        # mod_assign_get_submission_status takes an optional userid on
        # current Moodle. Worsaga never sends one, so naming *anyone* there
        # is refused. This used to raise MoodleScopeError after verifying
        # the caller's identity; the parameter policy now settles it first,
        # as MoodleParameterError and with no request at all. Both are
        # MoodleWriteAttemptError, so no orchestrator swallows either.
        transport = _FakeTransport()
        with patch("urllib.request.urlopen", side_effect=transport):
            with pytest.raises(MoodleWriteAttemptError, match="BLOCKED"):
                client.call(
                    "mod_assign_get_submission_status", assignid=1, userid=99,
                )
        assert transport.functions == []

    def test_useridfrom_filter_is_not_an_identity_claim(self, client):
        # useridfrom=0 means "any sender" and must stay untouched; only
        # useridto says whose mailbox is being read.
        transport = _FakeTransport()
        with patch("urllib.request.urlopen", side_effect=transport):
            client.call("core_message_get_messages", useridfrom=0)
        params = transport.params_for("core_message_get_messages")
        assert params["useridfrom"] == ["0"]
        assert params["useridto"] == [str(transport.userid)]

    def test_scope_error_is_not_swallowed_as_a_permission_warning(self):
        # Orchestrators re-raise MoodleWriteAttemptError rather than turning
        # it into a per-course "no access" warning; a scope violation needs
        # the same non-swallowable treatment.
        assert issubclass(MoodleScopeError, MoodleWriteAttemptError)

    def test_every_scoped_function_is_allowlisted(self):
        assert set(SELF_SCOPED_PARAMS) <= set(ALLOWED_FUNCTIONS)
        assert IDENTITY_PARAMS == {"userid", "useridto"}


# ── Config-based construction ──────────────────────────────────────


class TestClientConstruction:
    def test_client_from_config(self):
        cfg = MoodleConfig(url="https://example.com/moodle/", token="t", userid=42)
        c = MoodleClient(config=cfg)
        assert c.base_url == "https://example.com/moodle"  # trailing slash stripped
        assert c._config.userid == 42  # the configured hint


class TestVerifiedPrincipal:
    """The authenticated user id comes from the site, not from config.

    A configured ``WORSAGA_USERID`` is a hint. An elevated token plus a
    foreign id used to widen every self-scoped read; the site's own answer
    is now the only value that reaches the wire.
    """

    def _client(self, configured: int) -> MoodleClient:
        return MoodleClient(config=MoodleConfig(
            url="https://moodle.example.com", token="fake", userid=configured,
        ))

    def test_site_info_userid_wins_over_configured_hint(self, caplog):
        client = self._client(999)
        transport = _FakeTransport(userid=7, courses=[{"id": 10}])
        with patch("urllib.request.urlopen", side_effect=transport):
            with caplog.at_level("WARNING"):
                client.call("gradereport_user_get_grade_items", courseid=10)

        params = transport.params_for("gradereport_user_get_grade_items")
        assert params["userid"] == ["7"]
        # The mismatch is reported once, naming both values.
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "999" in warnings[0] and "7" in warnings[0]

    def test_configured_id_never_reaches_the_wire(self):
        client = self._client(999)
        transport = _FakeTransport(userid=7)
        with patch("urllib.request.urlopen", side_effect=transport):
            client.call("core_message_get_messages")
        assert transport.params_for("core_message_get_messages")["useridto"] == ["7"]

    def test_site_info_is_fetched_once_per_client(self):
        client = self._client(7)
        transport = _FakeTransport(userid=7)
        with patch("urllib.request.urlopen", side_effect=transport):
            client.call("core_message_get_messages")
            client.call("message_popup_get_popup_notifications")
            client.call("core_message_get_messages")
        assert transport.functions.count("core_webservice_get_site_info") == 1

    def test_site_info_memo_is_shared_with_the_connection_check(self):
        from worsaga.doctor import fetch_site_info

        client = self._client(7)
        transport = _FakeTransport(userid=7)
        with patch("urllib.request.urlopen", side_effect=transport):
            client.call("core_message_get_messages")
            assert fetch_site_info(client)["userid"] == 7
        assert transport.functions.count("core_webservice_get_site_info") == 1

    def test_matching_hint_warns_nothing(self, caplog):
        client = self._client(7)
        transport = _FakeTransport(userid=7)
        with patch("urllib.request.urlopen", side_effect=transport):
            with caplog.at_level("WARNING"):
                client.call("core_message_get_messages")
        assert [r for r in caplog.records if r.levelname == "WARNING"] == []

    def test_failed_verification_never_falls_back_to_the_hint(self):
        client = self._client(999)
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with pytest.raises(urllib.error.URLError):
                client.call("core_message_get_messages")

    def test_site_without_a_user_id_is_refused_not_assumed(self):
        client = self._client(999)
        with patch(
            "urllib.request.urlopen", return_value=_FakeResponse(b"{}"),
        ):
            with pytest.raises(MoodleScopeError, match="did not report"):
                client.call("core_message_get_messages")

    def test_site_info_is_not_callable_through_the_public_call(self, client):
        # exposed: False -- reachable only through MoodleClient.site_info().
        with patch("urllib.request.urlopen") as mock_urlopen:
            with pytest.raises(MoodleWriteAttemptError, match="internal to Worsaga"):
                client.call("core_webservice_get_site_info")
        mock_urlopen.assert_not_called()

    def test_site_info_wrapper_still_works(self, client):
        transport = _FakeTransport(userid=7)
        with patch("urllib.request.urlopen", side_effect=transport):
            assert client.site_info() == {"userid": 7}

    def test_mutating_the_returned_site_info_cannot_poison_identity(self):
        # site_info() used to hand out the memo itself, so a caller could
        # rewrite the id every later request was scoped to.
        client = self._client(7)
        transport = _FakeTransport(userid=7)
        with patch("urllib.request.urlopen", side_effect=transport):
            info = client.site_info()
            info["userid"] = 999
            assert client.userid == 7
            client.get_messages()
            assert client.site_info()["userid"] == 7
        assert transport.params_for("core_message_get_messages")["useridto"] == ["7"]


class TestServerErrorTokenRedaction:
    """Moodle's own error text becomes a Worsaga error string, and that
    string is printed, logged, and pasted into bug reports."""

    # 32 chars like a real wstoken, but deliberately not hex-shaped, so
    # the release audit's credential scanner never mistakes it for one.
    TOKEN = "faketesttokenfaketesttoken000001"

    def _client(self) -> MoodleClient:
        return MoodleClient(config=MoodleConfig(
            url="https://moodle.example.com", token=self.TOKEN, userid=1,
        ))

    def _transport(self, message: str, errorcode: str = "invalidtoken"):
        payload = json.dumps({
            "exception": "moodle_exception",
            "errorcode": errorcode,
            "message": message,
        }).encode()

        def transport(req, timeout=30):
            params = urllib.parse.parse_qs(req.data.decode())
            if (params.get("wsfunction") or [""])[0] == \
                    "core_webservice_get_site_info":
                return _FakeResponse(json.dumps({"userid": 1}).encode())
            return _FakeResponse(payload)

        return transport

    def test_echoed_token_is_redacted_from_the_message(self):
        transport = self._transport(
            f"Invalid parameter: wstoken={self.TOKEN} was rejected"
        )
        with patch("urllib.request.urlopen", side_effect=transport):
            with pytest.raises(MoodleRequestError) as exc:
                self._client().get_courses()
        text = str(exc.value)
        assert self.TOKEN not in text
        assert "***" in text
        assert "was rejected" in text

    def test_urlencoded_token_is_redacted_too(self):
        # The token goes out through urlencode, so a server quoting the
        # request back can echo the percent-encoded form.
        token = "tok en+with/specials"
        encoded = urllib.parse.quote_plus(token)
        client = MoodleClient(config=MoodleConfig(
            url="https://moodle.example.com", token=token, userid=1,
        ))
        transport = self._transport(f"bad request: wstoken={encoded}")
        with patch("urllib.request.urlopen", side_effect=transport):
            with pytest.raises(MoodleRequestError) as exc:
                client.get_courses()
        assert encoded not in str(exc.value)
        assert "***" in str(exc.value)

    def test_errorcode_is_redacted_as_well(self):
        transport = self._transport("nope", errorcode=f"bad-{self.TOKEN}")
        with patch("urllib.request.urlopen", side_effect=transport):
            with pytest.raises(MoodleRequestError) as exc:
                self._client().get_courses()
        assert self.TOKEN not in exc.value.errorcode
        assert "***" in exc.value.errorcode

    def test_ordinary_errors_are_untouched(self):
        transport = self._transport("Invalid token - token not found")
        with patch("urllib.request.urlopen", side_effect=transport):
            with pytest.raises(MoodleRequestError) as exc:
                self._client().get_courses()
        assert str(exc.value) == (
            "Moodle API error: Invalid token - token not found"
        )

    def test_empty_token_does_not_redact_everything(self):
        client = MoodleClient(config=MoodleConfig(
            url="https://moodle.example.com", token="", userid=1,
        ))
        assert client._redact_token("plain message") == "plain message"


class TestParameterPolicy:
    """Only the parameters Worsaga sends for a function may go on the wire."""

    def test_array_index_suffix_is_stripped_for_matching(self):
        assert _param_base_name("courseids[0]") == "courseids"
        assert _param_base_name("events[courseids][3]") == "events[courseids]"
        assert _param_base_name("options[timestart]") == "options[timestart]"

    @pytest.mark.parametrize("wsfunction,params", [
        ("core_course_get_contents", {"courseid": 1, "options[0][name]": "x"}),
        ("mod_forum_get_forum_discussions", {"forumid": 1, "sortby": "id"}),
        ("mod_assign_get_submission_status", {"assignid": 1, "groupid": 3}),
        ("core_message_get_messages", {"limitnum": 5, "currentuserid": 9}),
    ])
    def test_unknown_parameter_refused_before_any_request(
        self, client, wsfunction, params,
    ):
        with patch("urllib.request.urlopen") as mock_urlopen:
            with pytest.raises(MoodleParameterError, match="not part of what Worsaga"):
                client.call(wsfunction, **params)
        mock_urlopen.assert_not_called()

    @pytest.mark.parametrize("value", [7, 99, "99"])
    def test_unlisted_identity_param_refused_with_no_network_at_all(
        self, client, value,
    ):
        """An identity param the policy omits needs no identity to judge.

        Worsaga never sends a userid to ``mod_assign_get_submission_status``,
        so the refusal is structural and must not cost even the site-info
        request that verifying an id would need.
        """
        with patch("urllib.request.urlopen") as mock_urlopen:
            with pytest.raises(MoodleParameterError, match="BLOCKED"):
                client.call(
                    "mod_assign_get_submission_status", assignid=1, userid=value,
                )
        mock_urlopen.assert_not_called()

    def test_mapped_identity_param_mismatch_still_verifies_first(self, client):
        # Where the policy *does* list the identity param, judging it needs
        # the verified id -- that one site-info call is the identity
        # mechanism, not the widened request.
        transport = _FakeTransport(userid=7)
        with patch("urllib.request.urlopen", side_effect=transport):
            with pytest.raises(MoodleScopeError):
                client.call(
                    "gradereport_user_get_grade_items", courseid=10, userid=99,
                )
        assert transport.functions == ["core_webservice_get_site_info"]

    def test_array_parameters_are_accepted(self, client):
        transport = _FakeTransport(courses=[{"id": 10}, {"id": 11}])
        with patch("urllib.request.urlopen", side_effect=transport):
            client.get_forums_by_courses([10, 11])
        params = transport.params_for("mod_forum_get_forums_by_courses")
        assert params["courseids[0]"] == ["10"]
        assert params["courseids[1]"] == ["11"]


class TestEnrolmentScope:
    """Course-scoped reads never leave the enrolment set."""

    COURSES = [{"id": 10, "shortname": "ECON101"}]

    @pytest.mark.parametrize("method,args", [
        ("get_course_contents", (999999,)),
        ("get_user_grade_items", (999999,)),
        ("get_assignments", (999999,)),
        ("get_assignments_by_courses", ([999999],)),
        ("get_forums_by_courses", ([999999],)),
        ("get_quizzes", ([999999],)),
    ])
    def test_non_enrolled_course_never_reaches_moodle(self, client, method, args):
        transport = _FakeTransport(courses=self.COURSES)
        with patch("urllib.request.urlopen", side_effect=transport):
            with pytest.raises(CourseNotFoundError):
                getattr(client, method)(*args)
        # Only identity + enrolment discovery went out.
        assert set(transport.functions) <= {
            "core_webservice_get_site_info", "core_enrol_get_users_courses",
        }

    def test_calendar_by_course_is_scoped_too(self, client):
        transport = _FakeTransport(courses=self.COURSES)
        with patch("urllib.request.urlopen", side_effect=transport):
            with pytest.raises(CourseNotFoundError):
                client.get_calendar_events(course_ids=[999999])
        assert "core_calendar_get_calendar_events" not in transport.functions

    def test_enrolled_course_is_allowed_through(self, client):
        transport = _FakeTransport(courses=self.COURSES)
        with patch("urllib.request.urlopen", side_effect=transport):
            client.get_course_contents(10)
        assert transport.params_for("core_course_get_contents")["courseid"] == ["10"]

    def test_enrolment_set_is_fetched_once_for_many_reads(self, client):
        transport = _FakeTransport(courses=self.COURSES)
        with patch("urllib.request.urlopen", side_effect=transport):
            client.get_course_contents(10)
            client.get_user_grade_items(10)
            client.get_forums_by_courses([10])
        assert transport.functions.count("core_enrol_get_users_courses") == 1

    def test_listing_courses_refreshes_the_enrolment_memo(self, client):
        transport = _FakeTransport(courses=self.COURSES)
        with patch("urllib.request.urlopen", side_effect=transport):
            assert client.enrolled_course_ids() == frozenset({10})
            transport.courses = [{"id": 10}, {"id": 11}]
            client.get_courses()
            assert client.enrolled_course_ids() == frozenset({10, 11})
            client.get_course_contents(11)
        assert "core_course_get_contents" in transport.functions

    # Raw call() must be bound by the same rule as the wrappers: the
    # dispatcher, not the convenience layer, is where the guarantee lives.
    RAW_COURSE_CALLS = [
        ("core_course_get_contents", {"courseid": 999999}),
        ("gradereport_user_get_grade_items", {"courseid": 999999}),
        ("mod_assign_get_assignments", {"courseids[0]": 999999}),
        ("mod_forum_get_forums_by_courses", {"courseids[0]": 999999}),
        ("mod_quiz_get_quizzes_by_courses", {"courseids[0]": 999999}),
        ("core_calendar_get_calendar_events", {"events[courseids][0]": 999999}),
    ]

    @pytest.mark.parametrize("wsfunction,params", RAW_COURSE_CALLS)
    def test_raw_call_cannot_escape_the_enrolment_set(
        self, client, wsfunction, params,
    ):
        transport = _FakeTransport(courses=self.COURSES)
        with patch("urllib.request.urlopen", side_effect=transport):
            with pytest.raises(CourseNotFoundError):
                client.call(wsfunction, **params)
        assert wsfunction not in transport.functions

    @pytest.mark.parametrize("wsfunction,params", RAW_COURSE_CALLS)
    def test_raw_call_allows_an_enrolled_course(self, client, wsfunction, params):
        enrolled = {key: 10 for key in params}
        transport = _FakeTransport(courses=self.COURSES)
        with patch("urllib.request.urlopen", side_effect=transport):
            client.call(wsfunction, **enrolled)
        assert wsfunction in transport.functions

    def test_raw_call_checks_every_id_in_an_array_param(self, client):
        transport = _FakeTransport(courses=self.COURSES)
        with patch("urllib.request.urlopen", side_effect=transport):
            with pytest.raises(CourseNotFoundError):
                client.call(
                    "mod_forum_get_forums_by_courses",
                    **{"courseids[0]": 10, "courseids[1]": 999999},
                )
        assert "mod_forum_get_forums_by_courses" not in transport.functions

    def test_course_list_discovery_is_not_itself_course_scoped(self, client):
        # The function the enrolment set comes from must carry no course
        # parameter, or the check would recurse into itself.
        from worsaga.client import ALLOWED_FUNCTION_POLICIES

        policy = ALLOWED_FUNCTION_POLICIES["core_enrol_get_users_courses"]
        assert not policy.get("course_params")

    def test_concurrent_refresh_keeps_the_newer_enrolment_set(self, client):
        """A slow older refresh must never overwrite a newer one.

        Committing outside the lock let two refreshes land out of order, so
        a course revoked between them could be re-authorised by the stale
        answer arriving last.
        """
        started = threading.Event()
        release = threading.Event()
        guard = threading.Lock()
        state = {"active": 0, "peak": 0, "seen": 0}

        def transport(req, timeout=30):
            params = urllib.parse.parse_qs(req.data.decode())
            if params["wsfunction"] == ["core_webservice_get_site_info"]:
                return _FakeResponse(json.dumps({"userid": 1}).encode())
            with guard:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                first = state["seen"] == 0
                state["seen"] += 1
            if first:
                started.set()
                release.wait(5.0)
                body = json.dumps([{"id": 10}]).encode()  # older: 10 enrolled
            else:
                body = json.dumps([]).encode()            # newer: 10 revoked
            with guard:
                state["active"] -= 1
            return _FakeResponse(body)

        with patch("urllib.request.urlopen", side_effect=transport):
            slow = threading.Thread(target=client.get_courses)
            slow.start()
            assert started.wait(5.0)
            fast = threading.Thread(target=client.get_courses)
            fast.start()
            fast.join(0.2)
            assert fast.is_alive(), "the second refresh must wait for the first"
            release.set()
            slow.join(5.0)
            fast.join(5.0)

        assert state["peak"] == 1, "course-list refreshes must not overlap"
        assert client.enrolled_course_ids() == frozenset()


class TestClientConvenienceMethods:
    def test_get_assignment_submission_status(self, client):
        with patch.object(client, "call", return_value={}) as mock_call:
            client.get_assignment_submission_status(10)
        mock_call.assert_called_once_with(
            "mod_assign_get_submission_status",
            assignid=10,
        )

    def test_mod_assign_get_grades_is_not_allowlisted(self, client):
        # Removed in 0.6.0: the response can include other students'
        # grades for teacher-capable tokens and no feature needs it.
        from worsaga.client import ALLOWED_FUNCTIONS, MoodleWriteAttemptError

        assert "mod_assign_get_grades" not in ALLOWED_FUNCTIONS
        with pytest.raises(MoodleWriteAttemptError):
            client.call("mod_assign_get_grades", **{"assignmentids[0]": 10})

    def test_get_user_grade_items_leaves_identity_to_the_dispatcher(self, client):
        # The wrapper names only the course; call() fills in the verified
        # user id, so identity is resolved in exactly one place.
        with patch.object(client, "_require_enrolled"), \
             patch.object(client, "call", return_value={}) as mock_call:
            client.get_user_grade_items(10)
        mock_call.assert_called_once_with(
            "gradereport_user_get_grade_items",
            courseid=10,
        )

    def test_get_user_grade_items_is_self_only(self, client):
        # Removed in 0.8.2: the method takes no user-id parameter at all, so
        # it cannot be aimed at another student even with a token that
        # carries teacher capabilities.
        params = inspect.signature(MoodleClient.get_user_grade_items).parameters
        assert list(params) == ["self", "course_id"]
        with pytest.raises(TypeError):
            client.get_user_grade_items(10, 99)

    def test_get_user_grade_items_wire_params_carry_own_userid(self, client):
        # Guarded at the wire, not just the call() seam: the request body
        # always carries the site-verified userid.
        transport = _FakeTransport(userid=7, courses=[{"id": 10}])
        with patch("urllib.request.urlopen", side_effect=transport):
            client.get_user_grade_items(10)

        params = transport.params_for("gradereport_user_get_grade_items")
        assert params["courseid"] == ["10"]
        assert params["userid"] == ["7"]

    def test_core_course_get_courses_is_not_allowlisted(self, client):
        # Removed in 0.8.2: it reads course metadata by arbitrary id, so it
        # can describe courses this account is not enrolled in. Enrolment-
        # scoped discovery through core_enrol_get_users_courses is the
        # sanctioned path, and nothing called this.
        assert "core_course_get_courses" not in ALLOWED_FUNCTIONS
        with patch("urllib.request.urlopen") as mock_urlopen:
            with pytest.raises(MoodleWriteAttemptError):
                client.call("core_course_get_courses", **{"options[ids][0]": 5})
        mock_urlopen.assert_not_called()

    def test_core_grades_get_grades_is_not_allowlisted(self, client):
        # Removed in 0.8.2: it takes a userids list, so a teacher-capable
        # token could read other students' grades through it. The
        # authenticated user's own gradebook comes from
        # gradereport_user_get_grade_items.
        assert "core_grades_get_grades" not in ALLOWED_FUNCTIONS
        with patch("urllib.request.urlopen") as mock_urlopen:
            with pytest.raises(MoodleWriteAttemptError):
                client.call(
                    "core_grades_get_grades", courseid=10, **{"userids[0]": 99},
                )
        mock_urlopen.assert_not_called()

    def test_get_course_grades_is_gone(self, client):
        # The multi-user grade reader was deleted, not merely unused.
        assert not hasattr(client, "get_course_grades")

    def test_get_forums_by_courses(self, client):
        with patch.object(client, "_require_enrolled"), \
             patch.object(client, "call", return_value={}) as mock_call:
            assert client.get_forums_by_courses([10, 11]) == {}
        mock_call.assert_called_once_with(
            "mod_forum_get_forums_by_courses",
            **{"courseids[0]": 10, "courseids[1]": 11},
        )

    def test_get_forums_by_courses_wraps_moodle_list_payload(self, client):
        forum = {"id": 5, "course": 10, "name": "Announcements"}
        with patch.object(client, "_require_enrolled"), \
             patch.object(client, "call", return_value=[forum]):
            assert client.get_forums_by_courses([10]) == {"forums": [forum]}

    def test_get_forum_discussions(self, client):
        with patch.object(client, "call", return_value={}) as mock_call:
            client.get_forum_discussions(5)
        mock_call.assert_called_once_with("mod_forum_get_forum_discussions", forumid=5)

    def test_get_popup_notifications(self, client):
        # The mailbox owner is injected by call(), from the verified id.
        with patch.object(client, "call", return_value={}) as mock_call:
            client.get_popup_notifications(unread_only=True)
        mock_call.assert_called_once_with(
            "message_popup_get_popup_notifications",
            newestfirst=1,
            limit=100,
            offset=0,
        )

    def test_get_messages(self, client):
        with patch.object(client, "call", return_value={}) as mock_call:
            client.get_messages(since_time=123)
        assert mock_call.call_args.args == ("core_message_get_messages",)
        assert "currentuserid" not in mock_call.call_args.kwargs
        assert "timefrom" not in mock_call.call_args.kwargs
        assert mock_call.call_args.kwargs["useridfrom"] == 0
        assert mock_call.call_args.kwargs["read"] == 2

    def test_get_messages_wire_params_carry_verified_userid(self, client):
        transport = _FakeTransport(userid=7)
        with patch("urllib.request.urlopen", side_effect=transport):
            client.get_messages()
        assert transport.params_for("core_message_get_messages")["useridto"] == ["7"]

    def test_get_calendar_events(self, client):
        with patch.object(client, "_require_enrolled"), \
             patch.object(client, "call", return_value={}) as mock_call:
            client.get_calendar_events(course_ids=[10], timestart=1, timeend=2)
        mock_call.assert_called_once_with(
            "core_calendar_get_calendar_events",
            **{
                "events[courseids][0]": 10,
                "options[timestart]": 1,
                "options[timeend]": 2,
            },
        )

    def test_get_action_events_by_timesort(self, client):
        with patch.object(client, "call", return_value={}) as mock_call:
            client.get_action_events_by_timesort(1, 2, limit=50)
        mock_call.assert_called_once_with(
            "core_calendar_get_action_events_by_timesort",
            timesortfrom=1,
            timesortto=2,
            limitnum=50,
        )


class TestDownloadFile:
    def test_download_file_reads_files_within_default_cap(self, client):
        payload = b"x" * (11 * 1024 * 1024)
        with patch("urllib.request.urlopen", return_value=_FakeResponse(payload)):
            data = client.download_file("https://moodle.example.com/pluginfile.php/123/file.pptx")
        assert data == payload
        assert len(data) == len(payload)

    def test_download_file_oversize_body_is_skipped_not_truncated(self, client):
        payload = b"abcdef"
        with patch("urllib.request.urlopen", return_value=_FakeResponse(payload)):
            with pytest.raises(DownloadError) as exc_info:
                client.download_file(
                    "https://moodle.example.com/pluginfile.php/123/file.txt",
                    max_bytes=3,
                )
        assert exc_info.value.code == "oversize"

    def test_download_file_oversize_content_length_precheck(self, client):
        response = _FakeResponse(b"", headers={"Content-Length": str(60 * 1024 * 1024)})
        with patch("urllib.request.urlopen", return_value=response):
            with pytest.raises(DownloadError) as exc_info:
                client.download_file(
                    "https://moodle.example.com/pluginfile.php/123/big.pdf",
                )
        assert exc_info.value.code == "oversize"

    def test_download_file_uncapped_when_explicitly_requested(self, client):
        payload = b"abcdef"
        with patch("urllib.request.urlopen", return_value=_FakeResponse(payload)):
            data = client.download_file(
                "https://moodle.example.com/pluginfile.php/123/file.txt",
                max_bytes=None,
            )
        assert data == payload

    def test_download_file_adds_token_only_to_moodle_file_url(self, client):
        seen = {}

        def _fake_urlopen(req, timeout=30):
            seen["url"] = req.full_url
            return _FakeResponse(b"ok")

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            data = client.download_file(
                "https://moodle.example.com/pluginfile.php/123/file.txt?forcedownload=1",
            )

        assert data == b"ok"
        assert "forcedownload=1" in seen["url"]
        assert "token=fake" in seen["url"]

    def test_download_file_sends_user_agent(self, client):
        seen = {}

        def _fake_urlopen(req, timeout=30):
            seen["ua"] = req.get_header("User-agent")
            return _FakeResponse(b"ok")

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            client.download_file(
                "https://moodle.example.com/pluginfile.php/123/file.txt",
            )

        from worsaga.client import _user_agent
        assert seen["ua"] == _user_agent()

    def test_download_file_rejects_external_host_before_tokenizing(self, client):
        with patch("urllib.request.urlopen") as mock_urlopen:
            with pytest.raises(DownloadError) as exc_info:
                client.download_file(
                    "https://evil.example.com/pluginfile.php/123/file.txt",
                )

        assert exc_info.value.code == "invalid_url"
        mock_urlopen.assert_not_called()

    def test_download_file_accepts_explicit_default_port(self, client):
        # The configured base URL is stored canonically (no :443), but a
        # Moodle instance may still emit file URLs carrying one; comparing
        # raw netlocs read that as a different origin.
        with patch("urllib.request.urlopen", return_value=_FakeResponse(b"ok")):
            data = client.download_file(
                "https://moodle.example.com:443/pluginfile.php/123/file.txt",
            )
        assert data == b"ok"

    def test_download_file_accepts_default_port_on_the_base_side(self):
        # The mirror case, for an http loopback instance.
        local = MoodleClient(config=MoodleConfig(
            url="http://localhost", token="fake", userid=1,
        ))
        with patch("urllib.request.urlopen", return_value=_FakeResponse(b"ok")):
            data = local.download_file(
                "http://localhost:80/pluginfile.php/123/file.txt",
            )
        assert data == b"ok"

    def test_download_file_still_rejects_a_genuinely_different_port(self, client):
        with patch("urllib.request.urlopen") as mock_urlopen:
            with pytest.raises(DownloadError) as exc_info:
                client.download_file(
                    "https://moodle.example.com:8443/pluginfile.php/123/file.txt",
                )
        assert exc_info.value.code == "invalid_url"
        mock_urlopen.assert_not_called()

    def test_download_file_rejects_non_file_moodle_url(self, client):
        with patch("urllib.request.urlopen") as mock_urlopen:
            with pytest.raises(DownloadError) as exc_info:
                client.download_file(
                    "https://moodle.example.com/mod/url/view.php?id=123",
                )

        assert exc_info.value.code == "invalid_url"
        mock_urlopen.assert_not_called()

    @pytest.mark.parametrize("status,code", [(401, "auth"), (403, "auth"), (404, "not_found"), (500, "network")])
    def test_download_file_categorizes_http_errors(self, client, status, code):
        error = urllib.error.HTTPError(
            "https://moodle.example.com/pluginfile.php/123/file.txt?token=fake",
            status, "boom", hdrs=None, fp=None,
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with pytest.raises(DownloadError) as exc_info:
                client.download_file(
                    "https://moodle.example.com/pluginfile.php/123/file.txt",
                )
        assert exc_info.value.code == code
        # Redaction: no token, no URL in the message or the chained cause.
        assert "token=" not in str(exc_info.value)
        assert "pluginfile" not in str(exc_info.value)
        assert exc_info.value.__cause__ is None

    def test_download_file_categorizes_network_errors(self, client):
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with pytest.raises(DownloadError) as exc_info:
                client.download_file(
                    "https://moodle.example.com/pluginfile.php/123/file.txt",
                )
        assert exc_info.value.code == "network"

    def test_download_file_empty_body_is_structured_error(self, client):
        with patch("urllib.request.urlopen", return_value=_FakeResponse(b"")):
            with pytest.raises(DownloadError) as exc_info:
                client.download_file(
                    "https://moodle.example.com/pluginfile.php/123/file.txt",
                )
        assert exc_info.value.code == "empty"


def _exception_response(*, errorcode: str, message: str) -> "_FakeResponse":
    """A _FakeResponse carrying a Moodle web-service exception payload."""
    payload = json.dumps({
        "exception": "dml_missing_record_exception",
        "errorcode": errorcode,
        "message": message,
    }).encode()
    return _FakeResponse(payload)


def _missing_record_transport(course_id: int, *, errorcode="invalidrecord",
                              message="Can't find data record in database "
                                      "table course.") -> _FakeTransport:
    """Transport where *course_id* is enrolled but Moodle reports it missing.

    The pre-network enrolment check now stops an id this account is not in,
    so the Moodle-error mapping below is only reachable for an enrolled
    course whose record the server nonetheless refuses.
    """
    payload = json.dumps({
        "exception": "dml_missing_record_exception",
        "errorcode": errorcode,
        "message": message,
    }).encode()
    return _FakeTransport(courses=[{"id": course_id}], payload=payload)


class TestDomainNotFoundErrors:
    """Moodle "record not found" failures become friendly, classifiable
    domain exceptions instead of raw DB wording."""

    def test_course_contents_missing_record_raises_course_not_found(self, client):
        transport = _missing_record_transport(999999)
        with patch("urllib.request.urlopen", side_effect=transport):
            with pytest.raises(CourseNotFoundError) as exc_info:
                client.get_course_contents(999999)
        assert exc_info.value.course_id == 999999
        assert "999999" in str(exc_info.value)
        assert "not enrolled or does not exist" in str(exc_info.value)
        # The raw Moodle DB wording is replaced, not surfaced.
        assert "data record" not in str(exc_info.value)

    def test_course_not_found_detected_by_errorcode_only(self, client):
        # A localised (non-English) message must still be classified via
        # the stable errorcode.
        transport = _missing_record_transport(
            42,
            errorcode="invalidcourseid",
            message="No se puede encontrar el registro",
        )
        with patch("urllib.request.urlopen", side_effect=transport):
            with pytest.raises(CourseNotFoundError):
                client.get_course_contents(42)

    def test_grade_items_missing_record_raises_course_not_found(self, client):
        transport = _missing_record_transport(555)
        with patch("urllib.request.urlopen", side_effect=transport):
            with pytest.raises(CourseNotFoundError) as exc_info:
                client.get_user_grade_items(555)
        assert exc_info.value.course_id == 555

    def test_submission_status_missing_record_raises_assignment_not_found(self, client):
        response = _exception_response(
            errorcode="invalidrecord",
            message="Can't find data record in database table assign.",
        )
        with patch("urllib.request.urlopen", return_value=response):
            with pytest.raises(AssignmentNotFoundError) as exc_info:
                client.get_assignment_submission_status(777)
        assert exc_info.value.assignment_id == 777
        # AssignmentNotFoundError stays a ValueError for backward compat.
        assert isinstance(exc_info.value, ValueError)

    def test_non_missing_record_moodle_error_stays_request_error(self, client):
        # An auth/permission failure must keep raising, not be swallowed as
        # a not-found domain error.
        response = _exception_response(
            errorcode="accessexception",
            message="Access control exception",
        )
        with patch("urllib.request.urlopen", return_value=response):
            with pytest.raises(MoodleRequestError) as exc_info:
                client.get_course_contents(1)
        assert not isinstance(exc_info.value, CourseNotFoundError)
        assert "Moodle API error" in str(exc_info.value)
        assert exc_info.value.errorcode == "accessexception"


class TestServiceDisabled:
    """A site that has switched web services off is its own answer.

    Not a rejected token — nothing the user does with their credentials
    changes it — and not an unreachable site. The message says so,
    attributes the decision to the institution, and stops there: Worsaga
    documents no way around it.
    """

    #: The two errorcodes Moodle raises: web services off site-wide, and
    #: the external service a token belongs to being disabled.
    PAYLOADS = ("enablewsdescription", "servicenotavailable")

    def _transport(self, errorcode: str):
        """Every call answers with the disabled-service exception payload."""
        payload = json.dumps({
            "exception": "moodle_exception",
            "errorcode": errorcode,
            "message": "Web services are not enabled on this site.",
        }).encode()

        def transport(req, timeout=30):
            return _FakeResponse(payload)

        return transport

    @pytest.mark.parametrize("errorcode", PAYLOADS)
    def test_client_raises_the_dedicated_error(self, client, errorcode):
        with patch("urllib.request.urlopen", side_effect=self._transport(errorcode)):
            with pytest.raises(MoodleServiceDisabledError) as exc_info:
                client.site_info()
        assert str(exc_info.value) == SERVICE_DISABLED_MESSAGE
        assert exc_info.value.errorcode == errorcode

    def test_message_is_respectful_and_offers_no_workaround(self):
        text = SERVICE_DISABLED_MESSAGE.lower()
        assert "institution's decision" in text
        assert "cannot be used" in text
        # ASCII only: this reaches Windows consoles.
        SERVICE_DISABLED_MESSAGE.encode("ascii")
        # Nothing in it may read as a route around the decision.
        for forbidden in (
            "instead", "workaround", "work around", "bypass", "scrap",
            "another way", "alternative", "try ", "however",
        ):
            assert forbidden not in text, forbidden

    def test_not_classified_as_an_auth_failure(self):
        from worsaga.syncstate import classify_failure

        for errorcode in self.PAYLOADS:
            exc = MoodleRequestError("off", errorcode=errorcode)
            assert is_service_disabled_error(exc)
            assert not is_auth_error(exc)
            assert classify_failure(exc) == "service_disabled"

    def test_a_real_auth_failure_is_still_auth(self):
        from worsaga.syncstate import classify_failure

        exc = MoodleRequestError("nope", errorcode="invalidtoken")
        assert not is_service_disabled_error(exc)
        assert is_auth_error(exc)
        assert classify_failure(exc) == "auth"

    def test_message_fallback_when_the_errorcode_is_missing(self):
        # A proxy that drops the errorcode still leaves English text.
        exc = MoodleRequestError("Web services are not enabled on this site.")
        assert is_service_disabled_error(exc)
        assert not is_auth_error(exc)

    def test_connection_check_reports_service_disabled(self, client):
        from worsaga.doctor import ConnectionCheckError, fetch_site_info

        with patch("urllib.request.urlopen",
                   side_effect=self._transport("enablewsdescription")):
            with pytest.raises(ConnectionCheckError) as exc_info:
                fetch_site_info(client)
        assert exc_info.value.code == "service_disabled"
        assert str(exc_info.value) == SERVICE_DISABLED_MESSAGE

    def test_mcp_get_connection_info_returns_the_structured_code(self, client):
        from worsaga import mcp_server

        with patch.object(mcp_server, "_get_client", return_value=client), \
             patch("urllib.request.urlopen",
                   side_effect=self._transport("servicenotavailable")):
            result = mcp_server.get_connection_info()
        assert result["error_code"] == "service_disabled"
        assert result["error"] == SERVICE_DISABLED_MESSAGE
        assert result["error_code"] in mcp_server.ERROR_CODES

    def test_cli_doctor_prints_the_message(self, capsys):
        from worsaga.cli import main

        with patch("urllib.request.urlopen",
                   side_effect=self._transport("enablewsdescription")):
            with pytest.raises(SystemExit) as exc_info:
                main(["doctor"])
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert SERVICE_DISABLED_MESSAGE in out
        assert "token" not in out.lower().replace("web-service", "")
