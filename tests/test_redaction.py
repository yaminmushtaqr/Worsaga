"""The token never leaves: redaction at every Worsaga boundary.

Three kinds of test here.

The *unit* tests pin the primitive: which spellings of a secret are
matched, what a credential-bearing query parameter looks like, and how the
stream wrapper behaves when a value is split across two writes.

The *property sweep* drives every CLI command and every MCP tool against a
client that has woven the sentinel token into its responses — into URL
fields, into prose fields, and into the error messages a failure carries.
Injecting it is the point: a sweep that merely configures a token the demo
dataset never mentions proves nothing, because the assertion passes
whether or not any redaction happens at all.

The *regression* tests cover the specific paths that were found leaking:
a token read from standard input, a value split across writes, the binary
``.buffer`` beneath a text stream, a mapping *key*, an argument-validation
failure inside FastMCP, a logged exception, and the storage boundary.
"""

import asyncio
import io
import json
import logging
import re
import urllib.parse

import pytest

from worsaga import redact
from worsaga.redact import (
    REDACTED,
    RedactingStream,
    install_log_redaction,
    known_secrets,
    redact_payload,
    redact_text,
    redact_url,
    remember_secret,
    remove_log_redaction,
)

#: Distinctive enough that a substring match is meaningful, and long
#: enough to clear MIN_SECRET_LENGTH.
SENTINEL = "SENTINELtok3n-nEVER-print-me-42"

#: A second sentinel containing characters ``urlencode`` actually escapes,
#: so the encoded-spelling rules have something to bite on.
ESCAPED_SENTINEL = "abc+def/ghi=jkl mno~SENTINEL2"

_PCT = re.compile(r"%[0-9A-Fa-f]{2}")


def _lower_escapes(text: str) -> str:
    return _PCT.sub(lambda match: match.group(0).lower(), text)


@pytest.fixture
def armed():
    """Arm the redactor the way MoodleConfig does when a client is built."""
    remember_secret(SENTINEL)
    assert SENTINEL in known_secrets()
    return SENTINEL


# ── The primitive ─────────────────────────────────────────────────


class TestRedactText:
    def test_replaces_the_raw_secret(self, armed):
        assert redact_text(f"token is {SENTINEL}!") == f"token is {REDACTED}!"

    @pytest.mark.parametrize(
        "spelling",
        ["raw", "quote", "quote_lower", "quote_plus", "quote_plus_lower",
         "double", "double_lower"],
    )
    def test_replaces_every_encoded_spelling(self, spelling):
        remember_secret(ESCAPED_SENTINEL)
        quoted = urllib.parse.quote(ESCAPED_SENTINEL, safe="")
        forms = {
            "raw": ESCAPED_SENTINEL,
            "quote": quoted,
            "quote_lower": _lower_escapes(quoted),
            "quote_plus": urllib.parse.quote_plus(ESCAPED_SENTINEL),
            "quote_plus_lower": _lower_escapes(
                urllib.parse.quote_plus(ESCAPED_SENTINEL)
            ),
            "double": urllib.parse.quote(quoted, safe=""),
            "double_lower": _lower_escapes(
                urllib.parse.quote(quoted, safe="")
            ),
        }
        value = forms[spelling]
        cleaned = redact_text(f"leak {value} end")
        assert value not in cleaned
        assert REDACTED in cleaned

    @pytest.mark.parametrize("name", ["token", "wstoken", "access_token", "sesskey"])
    def test_strips_a_credential_parameter_whatever_its_value(self, name):
        url = f"https://moodle.example.edu/x.php?{name}=SOMEONEELSESVALUE&a=1"
        cleaned = redact_text(url)
        assert "SOMEONEELSESVALUE" not in cleaned
        assert f"{name}={REDACTED}" in cleaned
        assert "a=1" in cleaned

    def test_strips_an_encoded_parameter_nested_in_another_url(self):
        inner = "https%3A%2F%2Fmoodle.example.edu%2Ff.php%3Ftoken%3DHIDEME%26p%3D2"
        cleaned = redact_text(f"https://moodle.example.edu/go?next={inner}")
        assert "HIDEME" not in cleaned
        assert "%26p%3D2" in cleaned

    def test_strips_a_double_encoded_parameter(self):
        cleaned = redact_text("go?u=x%253Ftoken%253DHIDEME%2526p%253D2")
        assert "HIDEME" not in cleaned

    def test_strips_a_lowercase_encoded_parameter(self):
        cleaned = redact_text("go?u=x%3ftoken%3dHIDEME%26p%3d2")
        assert "HIDEME" not in cleaned
        assert "%26p%3d2" in cleaned

    def test_leaves_ordinary_text_alone(self, armed):
        text = "Week 3: elasticity and consumer surplus. See page 12."
        assert redact_text(text) == text

    def test_a_suffix_that_is_not_a_parameter_name_is_left_alone(self):
        assert redact_text("notatokenbutasuffix=keepme") == (
            "notatokenbutasuffix=keepme"
        )

    def test_a_long_identifier_run_does_not_take_quadratic_time(self):
        """An unbounded name prefix made this pattern explode.

        Every position in a long run of identifier characters expanded to
        the end of the run and backtracked hunting for the keyword, so a
        100 kB write — one page of extracted course text, or a base64
        blob — cost minutes of CPU at an output boundary that runs on
        every print.
        """
        import time

        blob = "x" * 200_000
        start = time.perf_counter()
        assert redact_text(blob) == blob
        assert time.perf_counter() - start < 2.0

    def test_a_long_prefix_before_a_real_parameter_still_matches(self):
        cleaned = redact_text("a" * 50_000 + "?wstoken=HIDEME&x=1")
        assert "HIDEME" not in cleaned
        assert cleaned.endswith(f"?wstoken={REDACTED}&x=1")

    def test_a_short_value_is_never_registered(self):
        assert remember_secret("abc") is False
        assert remember_secret("") is False
        assert remember_secret(None) is False

    def test_a_value_with_an_internal_line_break_is_never_registered(self):
        """No token spans a line, and the stream wrapper relies on that.

        Its newline shortcut — emit a completed line at once — is only
        sound while no registered secret can contain one, so the registry
        refuses such a value rather than leaving it to be noticed later.
        """
        assert remember_secret("first line\nsecond line here") is False
        assert remember_secret("carriage\rreturn value") is False
        assert known_secrets() == ()

    def test_surrounding_whitespace_is_stripped_not_refused(self):
        """A trailing newline is how a token normally arrives.

        Piped through ``--token-stdin``, read from a file, pasted into an
        environment variable: refusing those would leave a real credential
        unregistered for no security gain. The invariant the stream
        wrapper needs is only that no *registered* secret contains a line
        break, and stripping first gives exactly that.
        """
        assert remember_secret("\n  TOKENVALUE123 \n") is True
        assert known_secrets() == ("TOKENVALUE123",)
        assert redact_text("using TOKENVALUE123 now") == f"using {REDACTED} now"

    def test_an_empty_configured_token_redacts_nothing(self):
        redact.forget_secrets()
        assert redact_text("plain text") == "plain text"


class TestRedactPayload:
    def test_walks_nested_containers(self, armed):
        payload = {
            "a": [{"b": ("x", f"?token={SENTINEL}")}],
            "n": 3,
            "none": None,
            "flag": True,
        }
        cleaned = redact_payload(payload)
        assert SENTINEL not in json.dumps(cleaned, default=str)
        assert cleaned["n"] == 3
        assert cleaned["none"] is None
        assert cleaned["flag"] is True

    def test_keeps_container_types(self, armed):
        cleaned = redact_payload({"t": ("a",), "s": {"b"}, "l": ["c"]})
        assert isinstance(cleaned["t"], tuple)
        assert isinstance(cleaned["s"], set)
        assert isinstance(cleaned["l"], list)

    def test_keys_are_left_alone_by_default(self, armed):
        """The storage boundary drops such a key instead of rewriting it."""
        cleaned = redact_payload({SENTINEL: "v"})
        assert SENTINEL in cleaned

    def test_keys_are_redacted_when_asked(self, armed):
        cleaned = redact_payload({SENTINEL: "v"}, redact_keys=True)
        assert SENTINEL not in json.dumps(cleaned)
        assert cleaned == {REDACTED: "v"}

    def test_nested_keys_are_redacted_too(self, armed):
        payload = {"outer": [{f"?wstoken={SENTINEL}": {"deep": "x"}}]}
        cleaned = redact_payload(payload, redact_keys=True)
        assert SENTINEL not in json.dumps(cleaned)


class TestRedactingStream:
    def _sink(self):
        return io.StringIO()

    def test_redacts_on_write(self, armed):
        sink = self._sink()
        stream = RedactingStream(sink)
        stream.write(f"the token is {SENTINEL}\n")
        assert SENTINEL not in sink.getvalue()
        assert REDACTED in sink.getvalue()

    @pytest.mark.parametrize("split", [1, 5, 10, 20, 30])
    def test_a_secret_split_across_writes_is_still_caught(self, armed, split):
        """print('a', token) is three writes, not one."""
        sink = self._sink()
        stream = RedactingStream(sink)
        stream.write("prefix ")
        stream.write(SENTINEL[:split])
        stream.write(SENTINEL[split:])
        stream.write("\n")
        stream.flush()
        assert SENTINEL not in sink.getvalue()
        assert sink.getvalue() == f"prefix {REDACTED}\n"

    def test_a_secret_split_one_character_at_a_time(self, armed):
        sink = self._sink()
        stream = RedactingStream(sink)
        for char in SENTINEL:
            stream.write(char)
        stream.flush()
        assert SENTINEL not in sink.getvalue()

    def test_held_text_is_not_lost(self, armed):
        """Whatever is held back must still arrive, on flush at the latest."""
        sink = self._sink()
        stream = RedactingStream(sink)
        stream.write("no newline here")
        stream.flush()
        assert sink.getvalue() == "no newline here"

    def test_output_is_not_delayed_past_a_line(self, armed):
        """A completed line goes out immediately; watch streams NDJSON."""
        sink = self._sink()
        stream = RedactingStream(sink)
        stream.write('{"cycle": 1}\n')
        assert sink.getvalue() == '{"cycle": 1}\n'

    def test_the_held_tail_stays_bounded(self, armed):
        sink = self._sink()
        stream = RedactingStream(sink)
        stream.write("x" * 100_000)
        assert len(sink.getvalue()) >= 100_000 - redact.STREAM_HOLD_CHARS - 64

    def test_a_value_longer_than_the_hold_does_not_survive_the_cut(self, armed):
        """The bounded hold used to be a hole, not just a bound.

        One write of a ``token=`` parameter whose value is longer than
        :data:`redact.STREAM_HOLD_CHARS` was cut before it was scanned:
        the head matched and printed ``access_token=***``, and the tail —
        the last 512 characters of the raw value — went out on the next
        flush as ordinary text.
        """
        value = "A" * 600
        sink = self._sink()
        stream = RedactingStream(sink)
        stream.write(f"?access_token={value}")
        stream.flush()
        output = sink.getvalue()
        assert "A" * 10 not in output
        assert output == f"?access_token={REDACTED}"

    def test_a_secret_is_not_split_by_the_overflow_cut(self, armed):
        """The same hole, reassembling a *registered* secret in the output.

        A secret followed by enough characters to overflow the hold put
        the front of it in the emitted head and the rest in the tail. The
        two fragments arrive contiguously, so the complete token appears
        in the reader's terminal even though neither half matched.
        """
        sink = self._sink()
        stream = RedactingStream(sink)
        stream.write(SENTINEL + "z" * 500)
        stream.flush()
        output = sink.getvalue()
        assert SENTINEL not in output
        for start in range(len(SENTINEL) - 10 + 1):
            assert SENTINEL[start:start + 10] not in output
        assert output == REDACTED + "z" * 500

    def test_a_parameter_name_split_across_writes_is_still_caught(self, armed):
        sink = self._sink()
        stream = RedactingStream(sink)
        stream.write("?access_tok")
        stream.write("en=SECRETVALUE12345&x=1\n")
        assert "SECRETVALUE" not in sink.getvalue()
        assert sink.getvalue() == f"?access_token={REDACTED}&x=1\n"

    def test_streamed_output_equals_one_shot_for_a_real_token(self):
        """The guarantee, stated as an equality and checked exhaustively.

        A Moodle web-service token is 32 hexadecimal characters: one line,
        no parameter delimiter. For such a secret the streamed result is
        the one-shot result, however the writes happen to be chopped up.
        """
        token = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        remember_secret(token)
        text = (
            "GET https://moodle.example.edu/webservice/rest/server.php"
            f"?wstoken={token}&wsfunction=core_webservice_get_site_info\n"
            f"retrying with {token}\n"
        )
        expected = redact_text(text)
        assert token not in expected
        for size in range(1, len(text) + 1):
            sink = self._sink()
            stream = RedactingStream(sink)
            for start in range(0, len(text), size):
                stream.write(text[start:start + size])
            stream.flush()
            assert sink.getvalue() == expected, f"chunk size {size}"

    def test_rescanning_already_redacted_text_changes_nothing(self, armed):
        """The held tail is scanned again on the next write.

        That is only safe because redaction is idempotent on its own
        output: ``***`` matches nothing, and a re-scanned ``name=***``
        collapses to itself rather than growing.
        """
        text = f"a {REDACTED} b ?wstoken={REDACTED} c\n"
        assert redact_text(text) == text
        sink = self._sink()
        stream = RedactingStream(sink)
        for char in text:
            stream.write(char)
        stream.flush()
        assert sink.getvalue() == text

    def test_reports_the_length_the_caller_wrote(self, armed):
        text = f"x{SENTINEL}"
        assert RedactingStream(self._sink()).write(text) == len(text)

    def test_delegates_unknown_attributes(self):
        class _Sink:
            encoding = "utf-8"

            def isatty(self):
                return False

        stream = RedactingStream(_Sink())
        assert stream.encoding == "utf-8"
        assert stream.isatty() is False

    def test_the_binary_buffer_is_wrapped_not_handed_over(self, armed):
        """.buffer used to be a documented way straight past the wrapper."""
        raw = io.BytesIO()

        class _Text:
            buffer = raw

            def write(self, text):
                return len(text)

            def flush(self):
                pass

        stream = RedactingStream(_Text())
        assert stream.buffer is not raw
        stream.buffer.write(f"leaking {SENTINEL}\n".encode())
        stream.buffer.flush()
        assert SENTINEL.encode() not in raw.getvalue()
        assert REDACTED.encode() in raw.getvalue()

    def test_the_binary_buffer_survives_a_split(self, armed):
        raw = io.BytesIO()

        class _Text:
            buffer = raw

            def flush(self):
                pass

        stream = RedactingStream(_Text())
        stream.buffer.write(SENTINEL[:8].encode())
        stream.buffer.write(SENTINEL[8:].encode())
        stream.buffer.flush()
        assert SENTINEL.encode() not in raw.getvalue()

    def test_the_binary_buffer_passes_arbitrary_bytes_through(self, armed):
        raw = io.BytesIO()

        class _Text:
            buffer = raw

            def flush(self):
                pass

        payload = b"\xff\xfe binary \x00 bytes\n"
        stream = RedactingStream(_Text())
        stream.buffer.write(payload)
        stream.buffer.flush()
        assert raw.getvalue() == payload


class TestLogRedaction:
    def _capture(self):
        """Attach a StringIO handler to the root logger, filtered."""
        sink = io.StringIO()
        handler = logging.StreamHandler(sink)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        install_log_redaction()
        return sink, handler

    def _release(self, handler):
        remove_log_redaction()
        logging.getLogger().removeHandler(handler)

    def test_a_logged_message_is_redacted(self, armed):
        sink, handler = self._capture()
        try:
            logging.getLogger("worsaga.sync").warning(
                "sync fetch failed for %s: %s", "grades",
                f"wstoken={SENTINEL} rejected",
            )
        finally:
            self._release(handler)
        assert SENTINEL not in sink.getvalue()
        assert REDACTED in sink.getvalue()

    def test_a_logged_traceback_is_redacted(self, armed):
        sink, handler = self._capture()
        try:
            try:
                raise RuntimeError(f"upstream said wstoken={SENTINEL}")
            except RuntimeError:
                logging.getLogger("worsaga.sync").exception("fetch failed")
        finally:
            self._release(handler)
        assert SENTINEL not in sink.getvalue()

    def test_an_ordinary_message_is_untouched(self):
        sink, handler = self._capture()
        try:
            logging.getLogger("worsaga.sync").warning("nothing secret here")
        finally:
            self._release(handler)
        assert "nothing secret here" in sink.getvalue()

    def test_installing_twice_does_not_double_filter(self, armed):
        sink, handler = self._capture()
        try:
            install_log_redaction()
            filters = [
                f for f in handler.filters
                if isinstance(f, redact.RedactingLogFilter)
            ]
            assert len(filters) <= 1
            logging.getLogger("worsaga").warning("plain")
        finally:
            self._release(handler)
        assert sink.getvalue().count("plain") == 1


# ── The record-layer gaps this closed ─────────────────────────────


class TestRecordUrlFields:
    def test_notification_context_url_is_stripped(self):
        from worsaga.messages import normalize_notifications

        records = normalize_notifications({"notifications": [{
            "id": 1,
            "subject": "Assignment graded",
            "contexturl": (
                "https://moodle.example.edu/mod/assign/view.php"
                "?id=9&token=LEAKEDNOTIFTOKEN"
            ),
        }]})
        assert records[0]["view_url"].endswith(f"token={REDACTED}")
        assert "LEAKEDNOTIFTOKEN" not in records[0]["view_url"]

    def test_message_context_url_is_stripped(self):
        from worsaga.messages import normalize_messages

        records = normalize_messages({"messages": [{
            "id": 2, "smallmessage": "hi",
            "contexturl": "https://moodle.example.edu/m.php?wstoken=LEAKEDMSG1",
        }]})
        assert "LEAKEDMSG1" not in records[0]["view_url"]

    def test_calendar_event_url_is_stripped(self):
        from worsaga.calendar import normalize_calendar_events

        events = normalize_calendar_events({"events": [{
            "id": 3, "name": "Lecture", "timestart": 1_800_000_000,
            "url": "https://moodle.example.edu/cal.php?token=LEAKEDCALTOKEN",
        }]})
        assert "LEAKEDCALTOKEN" not in events[0]["view_url"]

    def test_forum_discussion_url_is_stripped(self):
        from worsaga.models import forum_discussion_record

        record = forum_discussion_record(
            course_id=1, forum_id=2, forum_name="Announcements",
            discussion_id=3, name="Welcome",
            view_url="https://moodle.example.edu/d.php?d=3&token=LEAKEDFORUM1",
        )
        assert "LEAKEDFORUM1" not in record["view_url"]

    def test_redact_url_handles_none(self):
        assert redact_url(None) == ""


class TestClientErrorRedaction:
    def test_a_server_echo_of_the_token_is_stripped(self):
        from worsaga.client import MoodleClient
        from worsaga.config import MoodleConfig

        client = MoodleClient(
            MoodleConfig(url="https://moodle.example.edu", token=SENTINEL),
        )
        message = (
            f"Invalid request: wstoken={SENTINEL}&wsfunction=core_x "
            f"(also {urllib.parse.quote(SENTINEL, safe='')})"
        )
        cleaned = client._redact_token(message)
        assert SENTINEL not in cleaned
        assert urllib.parse.quote(SENTINEL, safe="") not in cleaned

    def test_building_a_config_arms_the_redactor(self):
        from worsaga.config import MoodleConfig

        redact.forget_secrets()
        MoodleConfig(url="https://moodle.example.edu", token=SENTINEL)
        assert SENTINEL not in redact_text(f"leak {SENTINEL}")


# ── The storage boundary shares the definition ────────────────────


class TestCacheParity:
    """Storage and output must agree on what a secret looks like."""

    @pytest.mark.parametrize(
        "value",
        [
            "https://m.example.edu/f.php?token=LEAKEDVALUE1",
            "https://m.example.edu/go?u=x%3Ftoken%3DLEAKEDVALUE1%26p%3D2",
            "https://m.example.edu/go?u=x%253Ftoken%253DLEAKEDVALUE1",
            "https://m.example.edu/f.php?wstoken=LEAKEDVALUE1&a=1",
        ],
        ids=["plain", "encoded", "double-encoded", "wstoken"],
    )
    def test_encoded_parameters_are_sanitized_too(self, value):
        from worsaga.cache import sanitize_payload

        assert "LEAKEDVALUE1" not in json.dumps(
            sanitize_payload({"url": value})
        )

    def test_the_configured_token_alone_is_sanitized(self, armed):
        from worsaga.cache import sanitize_payload

        cleaned = sanitize_payload({"note": f"see {SENTINEL} for details"})
        assert SENTINEL not in json.dumps(cleaned)

    def test_token_named_keys_are_still_dropped_not_rewritten(self, armed):
        from worsaga.cache import sanitize_payload

        cleaned = sanitize_payload({"wstoken": "v", "keep": 1})
        assert cleaned == {"keep": 1}

    def test_a_sync_writes_none_of_it_to_disk(self, tmp_path, armed):
        """End to end: through run_sync, into the cache file's bytes."""
        from worsaga.sync import run_sync

        cache_path = tmp_path / "cache.db"
        client = _LeakyDemoClient(SENTINEL)
        run_sync(client, cache_path=cache_path, categories="all")
        raw = cache_path.read_bytes()
        assert SENTINEL.encode() not in raw
        assert urllib.parse.quote(SENTINEL, safe="").encode() not in raw


# ── A client that leaks the sentinel everywhere it can ────────────

#: Response fields that carry free-form prose, where a leaked credential
#: would realistically be pasted by a person or echoed by a plugin.
_PROSE_KEYS = frozenset({
    "intro", "summary", "description", "subject", "smallmessage",
    "fullmessage", "fullmessagehtml", "feedback", "text", "message",
    "fullname", "name", "contexturlname",
})

#: Never injected into: Worsaga matches on these, and corrupting them
#: would make the sweep fail for a reason that is not redaction.
_STRUCTURAL_KEYS = frozenset({"shortname", "filename", "filepath"})


def _inject(value, sentinel, key=None):
    """Return *value* with the sentinel woven into every plausible field."""
    if isinstance(value, dict):
        return {k: _inject(v, sentinel, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [_inject(item, sentinel, key=key) for item in value]
    if not isinstance(value, str) or key in _STRUCTURAL_KEYS:
        return value
    if value.startswith("http"):
        separator = "&" if "?" in value else "?"
        return f"{value}{separator}token={sentinel}"
    if key in _PROSE_KEYS and value:
        return f"{value} (ref {sentinel})"
    return value


#: Every read method whose response is woven with the sentinel.
_LEAKY_METHODS = (
    "get_courses", "get_course_contents", "get_user_grade_items",
    "get_assignments_by_courses", "get_assignment_submission_status",
    "get_quizzes", "get_forums_by_courses", "get_forum_discussions",
    "get_popup_notifications", "get_messages", "get_calendar_events",
    "get_action_events_by_timesort", "site_info",
)


def _LeakyDemoClient(sentinel):  # noqa: N802 - reads as a constructor
    """A demo client whose every response carries the sentinel token.

    Without this the sweep asserts nothing: the demo dataset has no
    credentials in it, so "the token never appears" is true before any
    redaction runs. Here it appears in every URL, in every prose field,
    and therefore in anything built from them.
    """
    from worsaga.demo import DemoMoodleClient

    client = DemoMoodleClient()

    def _wrap(original):
        def _leaky(*args, **kwargs):
            return _inject(original(*args, **kwargs), sentinel)
        return _leaky

    wrapped = 0
    for name in _LEAKY_METHODS:
        original = getattr(client, name, None)
        if original is None:
            # The demo client mirrors the read subset the surfaces use, not
            # every wrapper on MoodleClient. Skipping is fine; the guard
            # test below asserts the injection actually landed somewhere.
            continue
        setattr(client, name, _wrap(original))
        wrapped += 1
    assert wrapped >= 10, "the leaky client wrapped almost nothing"
    return client


def test_the_leaky_client_really_leaks():
    """Guard the guard: a sweep against a clean client proves nothing."""
    redact.forget_secrets()
    client = _LeakyDemoClient(SENTINEL)
    assert SENTINEL in json.dumps(client.get_courses(), default=str)
    assert SENTINEL in json.dumps(client.get_course_contents(101), default=str)
    assert SENTINEL in json.dumps(client.get_popup_notifications(), default=str)


# ── Property sweep: every CLI command ─────────────────────────────

#: Every subcommand the parser defines. ``setup`` appears twice because
#: its two token sources are different code paths, and one of them was
#: the path that leaked.
CLI_INVOCATIONS = [
    ["courses"],
    ["courses", "--json"],
    ["deadlines"],
    ["contents", "ECON101"],
    ["materials", "ECON101", "--week", "1"],
    ["download", "ECON101", "--week", "1", "--index", "0"],
    ["extract", "ECON101", "--week", "1", "--index", "0"],
    ["grades"],
    ["grades", "--json"],
    ["grades", "--summary"],
    ["assignments"],
    ["forums", "ECON101"],
    ["forum", "latest", "ECON101"],
    ["updates"],
    ["updates", "--json"],
    ["notifications"],
    ["notifications", "--json"],
    ["inbox"],
    ["inbox", "--json"],
    ["digest"],
    ["digest", "--json"],
    ["calendar"],
    ["calendar", "--json"],
    ["summary", "ECON101", "--week", "1"],
    ["search", "ECON101", "week"],
    ["index", "ECON101", "--week", "1"],
    ["search-text", "week"],
    ["study-pack", "ECON101", "--week", "1", "--stdout"],
    ["sync"],
    ["sync", "--json"],
    ["sync", "--categories", "grades"],
    ["sync", "--unattended"],
    ["changes"],
    ["watch", "--cycles", "1", "--no-notify"],
    ["auto-sync", "status"],
    ["doctor"],
    ["config", "path"],
    ["update"],
]


@pytest.fixture
def leaky_cli(monkeypatch, tmp_path):
    """Point the CLI at the leaking client, with the token configured."""
    from worsaga import cli

    monkeypatch.setenv("WORSAGA_TOKEN", SENTINEL)
    monkeypatch.setenv("WORSAGA_CACHE_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setenv("WORSAGA_INDEX_PATH", str(tmp_path / "search.db"))
    monkeypatch.setenv("WORSAGA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    client = _LeakyDemoClient(SENTINEL)
    monkeypatch.setattr(cli, "_client", lambda args: client)
    return cli


@pytest.mark.parametrize(
    "argv", CLI_INVOCATIONS, ids=lambda a: "_".join(a).replace("--", ""),
)
def test_no_cli_command_prints_the_token(argv, capsys, leaky_cli):
    try:
        leaky_cli.main(["--demo", *argv])
    except SystemExit as exc:  # a non-zero exit is still output to inspect
        assert exc.code in (0, 1, None)

    captured = capsys.readouterr()
    assert SENTINEL not in captured.out
    assert SENTINEL not in captured.err


def test_setup_does_not_print_a_token_given_as_an_argument(
    capsys, monkeypatch, tmp_path,
):
    from worsaga import cli

    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        cli.main([
            "setup", "--url", "https://moodle.example.edu",
            "--token", SENTINEL,
        ])
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out
    assert SENTINEL not in captured.err


def test_setup_does_not_print_a_token_read_from_stdin(
    capsys, monkeypatch, tmp_path,
):
    """The stdin path used to return before the redactor was armed."""
    from worsaga import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{SENTINEL}\n"))
    with pytest.raises(SystemExit):
        cli.main([
            "setup", "--url", "https://moodle.example.edu", "--token-stdin",
        ])
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out
    assert SENTINEL not in captured.err


def test_a_token_read_from_stdin_arms_the_redactor(monkeypatch):
    """The precise regression: --token-stdin must register the value."""
    import argparse

    from worsaga import cli

    redact.forget_secrets()
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{SENTINEL}\n"))
    args = argparse.Namespace(token=None, token_stdin=True)
    cli._apply_token_source(args, argparse.ArgumentParser())
    assert args.token == SENTINEL
    assert SENTINEL in known_secrets()


def test_a_stdin_token_is_redacted_from_a_command_that_prints_it(
    capsys, monkeypatch,
):
    """End to end for the same gap, through a command body that leaks."""
    from worsaga import cli

    redact.forget_secrets()
    monkeypatch.delenv("WORSAGA_TOKEN", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{SENTINEL}\n"))

    def _leaky(args):
        print(f"stdout leak {args.token}")

    monkeypatch.setattr(cli, "cmd_courses", _leaky)
    cli.main(["--demo", "--token-stdin", "courses"])
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out
    assert REDACTED in captured.out


def test_the_cli_redacts_a_token_a_command_manages_to_print(
    capsys, monkeypatch,
):
    from worsaga import cli

    monkeypatch.setenv("WORSAGA_TOKEN", SENTINEL)

    def _leaky(args):
        import sys as _sys

        print(f"stdout leak {SENTINEL}")
        print(f"stderr leak ?wstoken={SENTINEL}", file=_sys.stderr)

    monkeypatch.setattr(cli, "cmd_courses", _leaky)
    cli.main(["--demo", "courses"])
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out
    assert SENTINEL not in captured.err
    assert REDACTED in captured.out


def test_the_cli_redacts_a_logged_token(capsys, monkeypatch):
    """A warning logged by an orchestrator goes through a handler, not print."""
    from worsaga import cli

    monkeypatch.setenv("WORSAGA_TOKEN", SENTINEL)

    def _leaky(args):
        logging.getLogger("worsaga.sync").warning(
            "sync fetch failed for %s: %s", "grades",
            f"wstoken={SENTINEL} rejected",
        )

    monkeypatch.setattr(cli, "cmd_courses", _leaky)
    cli.main(["--demo", "courses"])
    captured = capsys.readouterr()
    assert SENTINEL not in captured.err


def test_the_cli_restores_the_streams_afterwards(monkeypatch):
    import sys

    from worsaga import cli

    before = (sys.stdout, sys.stderr)
    cli.main(["--demo", "config", "path"])
    assert (sys.stdout, sys.stderr) == before


def test_a_failing_command_still_restores_the_streams(monkeypatch):
    import sys

    from worsaga import cli

    def _boom(args):
        raise RuntimeError("nope")

    monkeypatch.setattr(cli, "cmd_courses", _boom)
    before = (sys.stdout, sys.stderr)
    with pytest.raises(SystemExit):
        cli.main(["--demo", "courses"])
    assert (sys.stdout, sys.stderr) == before


@pytest.mark.parametrize(
    "argv",
    [
        ["sync", "--token", SENTINEL],
        ["--token", SENTINEL, "sync", "--days", SENTINEL],
        [f"--token={SENTINEL}", "sync", "--days", "not-a-number"],
        [f"--token-stdin={SENTINEL}", "sync"],
        [f"--setup-token={SENTINEL}", "sync"],
        ["--wstoken", SENTINEL, "sync"],
    ],
    ids=["unrecognized-argument", "invalid-int", "joined-form",
         "value-less-flag-given-a-value", "unknown-joined-flag",
         "invented-token-flag"],
)
def test_a_token_in_a_failing_command_line_is_not_echoed(
    capsys, monkeypatch, argv,
):
    """Argparse quotes what it could not parse — including the token.

    ``_apply_token_source`` registers ``--token`` only after a successful
    parse, so every *failing* invocation printed the value: "unrecognized
    arguments: --token SECRET", "invalid int value: 'SECRET'". argv is
    now scanned before the parser runs.

    ``--wstoken`` is in the list deliberately: a flag Worsaga does not
    define, whose value the user plainly meant as a credential. The scan
    covers it, and the price is one starred word in the error message of a
    command that was never going to run.
    """
    from worsaga import cli

    redact.forget_secrets()
    monkeypatch.delenv("WORSAGA_TOKEN", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    assert excinfo.value.code != 0
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out
    assert SENTINEL not in captured.err


def test_the_argv_scan_ignores_flags_that_take_no_value(monkeypatch):
    """``--token-stdin`` names no secret; the item after it is a command.

    Registering it would strike the *subcommand* out of everything this
    run printed — which is why the separated form is restricted to flags
    that genuinely take a value, and the joined form is not.
    """
    from worsaga import cli

    redact.forget_secrets()
    cli._remember_argv_tokens(["--token-stdin", "notifications"])
    assert known_secrets() == ()


def test_a_stdin_run_redacts_the_token_and_nothing_else(capsys, monkeypatch):
    """The other half of that trade, end to end through a real command."""
    from worsaga import cli

    redact.forget_secrets()
    monkeypatch.delenv("WORSAGA_TOKEN", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{SENTINEL}\n"))
    cli.main(["--demo", "--token-stdin", "notifications"])
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out
    assert REDACTED not in captured.out
    assert "notifications" not in known_secrets()


def test_output_held_by_the_wrapper_is_not_lost_at_exit(capsys, monkeypatch):
    """A command whose last line has no newline must still be printed."""
    from worsaga import cli

    def _partial(args):
        print("tail with no newline", end="")

    monkeypatch.setattr(cli, "cmd_courses", _partial)
    cli.main(["--demo", "courses"])
    assert "tail with no newline" in capsys.readouterr().out


# ── Property sweep: every MCP tool ────────────────────────────────


@pytest.fixture
def leaky_mcp(monkeypatch, tmp_path):
    """The MCP module served by the leaking client, writes in tmp_path."""
    pytest.importorskip("mcp")
    from worsaga import mcp_server

    remember_secret(SENTINEL)
    monkeypatch.setenv("WORSAGA_CACHE_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setenv("WORSAGA_INDEX_PATH", str(tmp_path / "search.db"))
    monkeypatch.setenv("WORSAGA_STATE_DIR", str(tmp_path / "state"))
    client = _LeakyDemoClient(SENTINEL)
    monkeypatch.setattr(mcp_server, "_get_client", lambda: client)
    monkeypatch.setattr(
        mcp_server, "default_downloads_dir", lambda: tmp_path / "downloads",
    )
    monkeypatch.setattr(
        mcp_server, "announce_third_party_collection", lambda *a, **k: False,
    )
    return mcp_server


def _mcp_calls(server):
    """Every tool, gated or not, with arguments the demo dataset satisfies."""
    course = next(
        c["id"] for c in server.list_courses() if c["shortname"] == "ECON101"
    )
    assignments = server.get_assignments(course)
    assignment_id = assignments[0]["id"] if assignments else 0
    return {
        "list_courses": lambda: server.list_courses(),
        "get_deadlines": lambda: server.get_deadlines(),
        "get_grades": lambda: server.get_grades(),
        "get_grade_summary": lambda: server.get_grade_summary(),
        "get_assignments": lambda: server.get_assignments(),
        "get_assignment_status": lambda: server.get_assignment_status(
            course, assignment_id,
        ),
        "get_course_forums": lambda: server.get_course_forums(course),
        "get_forum_discussions": lambda: server.get_forum_discussions(course),
        "get_latest_updates": lambda: server.get_latest_updates(),
        "get_notifications": lambda: server.get_notifications(),
        "get_messages": lambda: server.get_messages(),
        "get_digest": lambda: server.get_digest(),
        "get_calendar_events": lambda: server.get_calendar_events(),
        "get_course_contents": lambda: server.get_course_contents(course),
        "get_week_materials": lambda: server.get_week_materials(course, "1"),
        "search_course_content": lambda: server.search_course_content(
            course, "week",
        ),
        "get_weekly_summary": lambda: server.get_weekly_summary(course, "1"),
        "download_material": lambda: server.download_material(
            course, "1", index=0,
        ),
        "extract_material": lambda: server.extract_material(
            course, "1", index=0,
        ),
        "sync_now": lambda: server.sync_now(categories="all"),
        "get_changes": lambda: server.get_changes(),
        "build_search_index": lambda: server.build_search_index(course, "1"),
        "search_text": lambda: server.search_text("week"),
        "export_study_pack": lambda: server.export_study_pack(course, "1"),
        "get_autosync_status": lambda: server.get_autosync_status(),
        "get_connection_info": lambda: server.get_connection_info(),
    }


def test_the_sweep_covers_every_mcp_tool(leaky_mcp):
    assert set(_mcp_calls(leaky_mcp)) == set(leaky_mcp.ALL_TOOLS)


def test_no_mcp_tool_returns_the_token(leaky_mcp):
    for name, call in _mcp_calls(leaky_mcp).items():
        rendered = json.dumps(call(), default=str)
        assert SENTINEL not in rendered, name


def test_an_mcp_result_is_redacted_however_deeply_nested(leaky_mcp):
    from unittest.mock import patch

    leaky = {"a": [{"b": {"c": f"https://x/f.php?token={SENTINEL}"}}]}
    with patch.object(leaky_mcp, "_autosync_status", return_value=leaky):
        result = leaky_mcp.get_autosync_status()
    assert SENTINEL not in json.dumps(result)
    assert result["a"][0]["b"]["c"].endswith(REDACTED)


def test_an_mcp_result_key_is_redacted(leaky_mcp):
    """A mapping key used to walk straight out."""
    from unittest.mock import patch

    with patch.object(
        leaky_mcp, "_autosync_status", return_value={SENTINEL: "value"},
    ):
        result = leaky_mcp.get_autosync_status()
    assert SENTINEL not in json.dumps(result)
    assert result == {REDACTED: "value"}


def test_an_mcp_exception_message_is_redacted(leaky_mcp):
    from unittest.mock import patch

    def _boom():
        raise RuntimeError(f"upstream rejected wstoken={SENTINEL}")

    with patch.object(leaky_mcp, "_autosync_status", side_effect=_boom):
        with pytest.raises(RuntimeError) as excinfo:
            leaky_mcp.get_autosync_status()
    assert SENTINEL not in str(excinfo.value)
    assert REDACTED in str(excinfo.value)


def test_an_ordinary_exception_keeps_its_type(leaky_mcp):
    from unittest.mock import patch

    with patch.object(
        leaky_mcp, "_autosync_status", side_effect=KeyError("nope"),
    ):
        with pytest.raises(KeyError):
            leaky_mcp.get_autosync_status()


def test_an_argument_validation_failure_is_redacted(leaky_mcp):
    """FastMCP validates *before* the tool body, so the wrapper never sees it.

    A token passed where an int belongs comes back as a ToolError quoting
    the offending ``input_value`` — which is the token.
    """
    with pytest.raises(Exception) as excinfo:
        asyncio.run(
            leaky_mcp.mcp.call_tool(
                "get_deadlines", {"lookahead_days": SENTINEL},
            )
        )
    assert SENTINEL not in str(excinfo.value)
    assert REDACTED in str(excinfo.value)


def test_a_fresh_server_is_armed_before_the_first_call(monkeypatch):
    """The registry used to be armed only by the first tool body.

    Argument validation runs *before* any tool body, so on a server that
    had not answered a call yet there was nothing registered to strip: a
    token pasted into a mistyped argument came back verbatim in the
    ToolError. Start-up arms it instead.
    """
    pytest.importorskip("mcp")
    from worsaga import mcp_server

    redact.forget_secrets()
    monkeypatch.setenv("WORSAGA_DEMO", "1")
    monkeypatch.setenv("WORSAGA_TOKEN", SENTINEL)
    assert known_secrets() == ()

    mcp_server._arm_redaction()
    assert SENTINEL in known_secrets()

    with pytest.raises(Exception) as excinfo:
        asyncio.run(
            mcp_server.mcp.call_tool(
                "get_deadlines", {"lookahead_days": SENTINEL},
            )
        )
    assert SENTINEL not in str(excinfo.value)
    assert REDACTED in str(excinfo.value)


def test_arming_an_unconfigured_server_does_not_raise(monkeypatch):
    """No credentials is a normal way to start; it must not stop the server."""
    pytest.importorskip("mcp")
    from worsaga import mcp_server

    redact.forget_secrets()
    monkeypatch.delenv("WORSAGA_TOKEN", raising=False)
    monkeypatch.delenv("WORSAGA_URL", raising=False)
    mcp_server._arm_redaction()
    assert known_secrets() == ()


@pytest.mark.parametrize(
    "creds",
    [
        {"url": "http://not-https.example.edu", "token": SENTINEL, "userid": 7},
        {"url": "https://moodle.example.edu", "token": SENTINEL,
         "userid": "not-a-number"},
        {"url": "", "token": SENTINEL},
    ],
    ids=["invalid-url", "invalid-userid", "missing-url"],
)
def test_a_malformed_config_still_arms_the_token(monkeypatch, tmp_path, creds):
    """The token is real even when the rest of the file is not.

    ``MoodleConfig.load`` validates as it goes, so a bad URL or user id
    raises before anything registers — and that file's owner is exactly
    who then sees a startup error quoting their settings back. The raw
    JSON is read for the token first, so a configuration mistake cannot
    cost the redactor its secret.
    """
    pytest.importorskip("mcp")
    from worsaga import mcp_server

    redact.forget_secrets()
    for name in ("WORSAGA_TOKEN", "WORSAGA_URL", "WORSAGA_USERID"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "creds.json"
    path.write_text(json.dumps(creds), encoding="utf-8")
    monkeypatch.setenv("WORSAGA_CREDS_PATH", str(path))

    mcp_server._arm_redaction()

    assert SENTINEL in known_secrets()


@pytest.mark.parametrize(
    "contents", ["not json at all", "[]", ""], ids=["garbage", "list", "empty"],
)
def test_an_unusable_config_file_arms_nothing_and_raises_nothing(
    monkeypatch, tmp_path, contents,
):
    pytest.importorskip("mcp")
    from worsaga import mcp_server

    redact.forget_secrets()
    for name in ("WORSAGA_TOKEN", "WORSAGA_URL", "WORSAGA_USERID"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "creds.json"
    path.write_text(contents, encoding="utf-8")
    monkeypatch.setenv("WORSAGA_CREDS_PATH", str(path))

    mcp_server._arm_redaction()

    assert known_secrets() == ()


def test_a_valid_call_through_the_server_still_works(leaky_mcp):
    """The override must not change the success path."""
    result = asyncio.run(
        leaky_mcp.mcp.call_tool("get_deadlines", {"lookahead_days": 7})
    )
    assert SENTINEL not in json.dumps(result, default=str)


def test_a_rate_limit_error_stays_token_free(leaky_mcp):
    from unittest.mock import patch

    from worsaga.client import MoodleRateLimitedError

    def _limited():
        raise MoodleRateLimitedError(
            f"slow down (wstoken={SENTINEL})", status=429,
        )

    with patch.object(leaky_mcp, "_autosync_status", side_effect=_limited):
        result = leaky_mcp.get_autosync_status()
    assert result["error_code"] == "rate_limited"
    assert SENTINEL not in result["error"]
