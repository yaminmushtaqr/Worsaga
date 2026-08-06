"""Token redaction at Worsaga's boundaries.

Worsaga holds one secret — the Moodle web-service token — and it travels
in more places than the credentials file. Moodle itself hands back URLs
with a ``token=`` query parameter (a notification ``contexturl``, a
calendar link), a plugin or a reverse proxy can echo a request back in an
error payload, and any of those strings goes on to be printed, logged,
returned to an agent, and pasted into a bug report.

This module is the single implementation of "do not let that value
leave", applied at every boundary where data crosses out of the process:

- the CLI's stdout/stderr (:class:`RedactingStream`),
- the MCP tool result, and any exception a tool call raises,
- the logging handlers both of those install (:class:`RedactingLogFilter`),
- the local SQLite cache (:func:`worsaga.cache.sanitize_payload`),
- the record factories that carry a Moodle-supplied URL through.

Two independent rules, because each catches what the other cannot:

1. **The configured token, wherever it appears.** Every
   :class:`~worsaga.config.MoodleConfig` registers its own token here
   when it is built, so the redactor is armed no matter where the
   credentials came from (argument, environment, file, standard input).
   Raw, percent-encoded, ``+``-encoded, and double-encoded spellings are
   all matched, in both upper- and lower-case escape forms, because the
   value passes through ``urlencode`` on the way out and can come back
   quoted once or twice by anything in between.
2. **Any ``token``-ish query parameter, whatever its value.** ``token``,
   ``wstoken``, ``access_token``, ``sesskey`` — with ``=`` written
   literally, as ``%3D`` (a URL nested in another URL's query), or as
   ``%253D`` (nested twice). This catches credentials Worsaga never
   configured: the ones Moodle mints into the links it returns.

Redaction is deliberately blunt. It can flatten an innocent
``token=value`` in extracted lecture text, which is the right way round:
a printed secret cannot be un-printed.

The secret registry is process-local and holds nothing that is not
already in memory in a :class:`~worsaga.config.MoodleConfig`.
"""

from __future__ import annotations

import logging
import re
import threading
import traceback
import urllib.parse
from typing import Any, Iterable, Sequence

#: What a redacted value is replaced with. Says nothing about the
#: original — not its length, not a prefix — so a captured transcript
#: never narrows the search space for the secret.
REDACTED = "***"

#: Values shorter than this are never registered as secrets. A short
#: string would match ordinary prose and turn every output into noise,
#: and a token that short is not a Moodle token.
MIN_SECRET_LENGTH = 8

#: How much of a character stream is held back when a write does not end
#: at a line boundary, so a secret split across two ``write`` calls is
#: still matched (see :class:`_HoldingRedactor`). Also the longest
#: ``token=`` *value* that can be caught across such a split; a longer one
#: is caught whenever it arrives in one piece, which is every real case.
STREAM_HOLD_CHARS = 512

#: Longest identifier prefix allowed before ``token``/``sesskey``. Bounded
#: on purpose, and not for tidiness: an unbounded ``[A-Za-z0-9_-]*`` makes
#: this pattern quadratic. On a long run of identifier characters — a
#: base64 blob, a minified line of extracted course text — every position
#: expands to the end of the run and backtracks looking for the keyword,
#: which turned a 100 kB write into minutes of CPU. Thirty-two characters
#: is far longer than any real parameter name that ends in "token".
_PARAM_PREFIX_MAX = 32

#: A credential-bearing query parameter and its value. The name is an
#: identifier ending in ``token`` or ``sesskey``; the separator is a
#: literal ``=`` or its once/twice percent-encoded forms; the value runs
#: to the next delimiter, encoded or not. The lookbehind starts the match
#: at a real identifier boundary rather than mid-word, which is both more
#: accurate and less work.
_PARAM_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])"
    rf"([A-Za-z0-9_-]{{0,{_PARAM_PREFIX_MAX}}}(?:token|sesskey))"
    r"(=|%3D|%253D)"
    r"([^\s\"'<>&#]*?)"
    r"(?=$|[\s\"'<>&#]|%26|%2526|%23|%2523)"
)

#: A percent escape, for generating the lower-case spelling of an encoded
#: secret. ``urllib`` emits upper-case hex; PHP, some JavaScript, and
#: hand-written links emit lower-case.
_PCT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")

_lock = threading.Lock()
#: secret -> every spelling of it that could appear in outgoing text.
_secrets: dict[str, tuple[str, ...]] = {}


def _lowercase_escapes(text: str) -> str:
    """Return *text* with every ``%XX`` escape lower-cased."""
    return _PCT_ESCAPE_RE.sub(lambda match: match.group(0).lower(), text)


def _secret_forms(secret: str) -> tuple[str, ...]:
    """Return every spelling of *secret* that could reach an output stream.

    The raw value, the two percent-encodings ``urlencode`` produces, each
    of those encoded a second time (a Moodle link carried inside another
    link's query string), and the lower-case-escape spelling of all of
    them. Longest first, so a double-encoded form is replaced before the
    shorter spelling it contains.
    """
    forms = {secret}
    for encoded in (
        urllib.parse.quote(secret, safe=""),
        urllib.parse.quote_plus(secret),
    ):
        for form in (encoded, urllib.parse.quote(encoded, safe="")):
            forms.add(form)
            forms.add(_lowercase_escapes(form))
    return tuple(sorted(forms, key=len, reverse=True))


def remember_secret(value: Any) -> bool:
    """Register *value* as a secret to strip from every boundary.

    Returns whether it was registered. Empty, non-string, and implausibly
    short values are ignored (see :data:`MIN_SECRET_LENGTH`) rather than
    raising: this is called from a constructor and from argument parsing,
    and a configuration problem must surface as a configuration error, not
    as a crash inside the redactor.
    """
    if not isinstance(value, str):
        return False
    secret = value.strip()
    if len(secret) < MIN_SECRET_LENGTH:
        return False
    with _lock:
        if secret not in _secrets:
            _secrets[secret] = _secret_forms(secret)
    return True


def known_secrets() -> tuple[str, ...]:
    """Return the registered secrets (the raw values, longest first)."""
    with _lock:
        return tuple(sorted(_secrets, key=len, reverse=True))


def forget_secrets() -> None:
    """Drop every registered secret. Used by tests and by resets."""
    with _lock:
        _secrets.clear()


def _forms(secrets: Iterable[str] | None) -> tuple[str, ...]:
    """Return the spellings to strip for *secrets* (registry when None)."""
    if secrets is None:
        with _lock:
            ordered = sorted(_secrets, key=len, reverse=True)
            return tuple(
                form for secret in ordered for form in _secrets[secret]
            )
    collected: list[str] = []
    for secret in secrets:
        if isinstance(secret, str) and len(secret.strip()) >= MIN_SECRET_LENGTH:
            collected.extend(_secret_forms(secret.strip()))
    return tuple(sorted(set(collected), key=len, reverse=True))


def redact_text(text: str, *, secrets: Iterable[str] | None = None) -> str:
    """Return *text* with every known secret and token parameter removed.

    Exact secrets go first so that a value which happens to sit in a
    ``token=`` parameter is replaced by its own rule as well as by the
    parameter rule; the result is the same either way.
    """
    if not isinstance(text, str) or not text:
        return text
    cleaned = text
    for form in _forms(secrets):
        if form and form in cleaned:
            cleaned = cleaned.replace(form, REDACTED)
    return _PARAM_RE.sub(rf"\1\2{REDACTED}", cleaned)


def redact_payload(
    payload: Any,
    *,
    secrets: Iterable[str] | None = None,
    redact_keys: bool = False,
) -> Any:
    """Return *payload* with every string inside it redacted, recursively.

    Dicts, lists, tuples, and sets are rebuilt; scalars other than strings
    are returned unchanged.

    ``redact_keys`` also rewrites mapping *keys*. It is off by default,
    because at the storage boundary a key that names a secret is *dropped*
    rather than rewritten (:func:`worsaga.cache.sanitize_payload`) and
    rewriting one there would resurrect it. It is on at the MCP boundary,
    where the payload can be assembled from server-supplied text and
    ``{"<token>": "..."}`` would otherwise walk straight out. Two keys that
    redact to the same string collapse, last one winning — a mapping keyed
    by a credential is already pathological, and losing one of its entries
    is the recoverable direction.
    """
    if isinstance(payload, str):
        return redact_text(payload, secrets=secrets)
    if isinstance(payload, dict):
        return {
            (
                redact_text(key, secrets=secrets)
                if redact_keys and isinstance(key, str) else key
            ): redact_payload(
                value, secrets=secrets, redact_keys=redact_keys,
            )
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [
            redact_payload(item, secrets=secrets, redact_keys=redact_keys)
            for item in payload
        ]
    if isinstance(payload, tuple):
        return tuple(
            redact_payload(item, secrets=secrets, redact_keys=redact_keys)
            for item in payload
        )
    if isinstance(payload, set):
        return {
            redact_payload(item, secrets=secrets, redact_keys=redact_keys)
            for item in payload
        }
    return payload


def redact_url(value: Any) -> str:
    """Return a Moodle-supplied URL with any credential parameter stripped.

    Used by the record factories for the ``view_url`` fields Moodle fills
    in itself (a notification ``contexturl``, a calendar event link),
    which are copied through verbatim and can carry a ``token=``.
    """
    return redact_text(str(value or ""))


class _HoldingRedactor:
    """Redacts a character stream without ever emitting half a secret.

    A single ``print(f"...{token}")`` is one ``write``, but plenty of
    output is not: ``print("a", token)`` writes three times, and any
    formatter that streams its parts can split a value anywhere. Redacting
    each write in isolation would let ``SENTINEL`` written as ``SENT`` +
    ``INEL`` through untouched.

    So a tail is held back:

    - everything up to and including the last newline is safe to emit,
      because no match can span one (the parameter pattern excludes
      whitespace, and a Moodle token is a single line);
    - failing that, all but the last :data:`STREAM_HOLD_CHARS` characters
      go out and the rest waits for the next write.

    The held tail is bounded, so a caller streaming megabytes without a
    newline does not accumulate them here. :meth:`drain` returns whatever
    is left, and the stream wrappers call it on flush and close.
    """

    def __init__(self, secrets: Sequence[str] | None = None):
        self._secrets = None if secrets is None else tuple(secrets)
        self._pending = ""

    def _hold_size(self) -> int:
        longest = max(
            (len(form) for form in _forms(self._secrets)), default=0,
        )
        return max(STREAM_HOLD_CHARS, longest + 1)

    def _newline_is_a_boundary(self) -> bool:
        """Whether no registered secret can contain a line break.

        True in every real case — a web-service token is one line — and
        checked rather than assumed, because a secret that did contain one
        would make the newline shortcut unsound.
        """
        return not any(
            "\n" in form or "\r" in form for form in _forms(self._secrets)
        )

    def feed(self, text: str) -> str:
        """Return the redacted text that is safe to emit now."""
        buffered = self._pending + text
        cut = 0
        if self._newline_is_a_boundary():
            cut = buffered.rfind("\n") + 1
        hold = self._hold_size()
        if len(buffered) - cut > hold:
            cut = len(buffered) - hold
        self._pending = buffered[cut:]
        head = buffered[:cut]
        return redact_text(head, secrets=self._secrets) if head else ""

    def drain(self) -> str:
        """Return everything still held, redacted, and forget it."""
        pending, self._pending = self._pending, ""
        return redact_text(pending, secrets=self._secrets) if pending else ""


class RedactingStream:
    """A text stream that redacts everything written through it.

    Wraps ``sys.stdout``/``sys.stderr`` for the life of a CLI command so
    the guarantee is "the boundary redacts", not "every print site
    remembered to". Attribute access falls through to the wrapped stream,
    so ``isatty``, ``encoding``, and ``fileno`` behave as they did — which
    is what the banner, the progress reporter, and pytest's capture
    fixtures rely on.

    Two attributes are deliberately *not* passed through. ``buffer`` and
    ``raw`` are the underlying binary stream, and handing them out would
    offer a documented way to write bytes straight past this wrapper; they
    return a :class:`RedactingBytesStream` instead.

    ``write`` reports the length of what the caller asked to write, not of
    what was written: a caller counting characters is tracking its own
    output, and neither redaction nor buffering is its business.
    """

    def __init__(self, stream: Any, *, secrets: Sequence[str] | None = None):
        self._stream = stream
        self._secrets = None if secrets is None else tuple(secrets)
        self._redactor = _HoldingRedactor(secrets)
        self._buffer_wrapper: RedactingBytesStream | None = None

    @property
    def raw_stream(self) -> Any:
        """The wrapped stream, for callers that need to unwrap it."""
        return self._stream

    @property
    def buffer(self) -> Any:
        return self._binary("buffer")

    @property
    def raw(self) -> Any:
        return self._binary("raw")

    def _binary(self, name: str) -> Any:
        underlying = getattr(self._stream, name, None)
        if underlying is None:
            raise AttributeError(name)
        if self._buffer_wrapper is None:
            self._buffer_wrapper = RedactingBytesStream(
                underlying, secrets=self._secrets,
            )
        return self._buffer_wrapper

    def write(self, text: Any) -> int:
        if not isinstance(text, str):
            text = str(text)
        emit = self._redactor.feed(text)
        if emit:
            self._stream.write(emit)
        return len(text)

    def writelines(self, lines: Iterable[Any]) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        self._drain()
        self._stream.flush()

    def close(self) -> None:
        self._drain()
        self._stream.close()

    def _drain(self) -> None:
        """Push out anything the redactor is still holding."""
        if self._buffer_wrapper is not None:
            self._buffer_wrapper.flush()
        rest = self._redactor.drain()
        if rest:
            self._stream.write(rest)

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes this class does not define, and
        # never for ``buffer``/``raw`` (properties above take precedence).
        return getattr(self.__dict__["_stream"], name)


class RedactingBytesStream:
    """The binary half of :class:`RedactingStream`.

    Bytes are decoded with ``surrogateescape``, redacted, and re-encoded
    the same way, so anything that is not a match round-trips unchanged
    even when it is not valid UTF-8 at all. Holding works exactly as it
    does for text, so a secret split across two ``write`` calls is caught
    here too.
    """

    def __init__(self, stream: Any, *, secrets: Sequence[str] | None = None):
        self._stream = stream
        self._redactor = _HoldingRedactor(secrets)

    def write(self, data: Any) -> int:
        if isinstance(data, str):
            raw = data.encode("utf-8", "surrogateescape")
        else:
            raw = bytes(data)
        emit = self._redactor.feed(raw.decode("utf-8", "surrogateescape"))
        if emit:
            self._stream.write(emit.encode("utf-8", "surrogateescape"))
        return len(raw)

    def writelines(self, lines: Iterable[Any]) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        rest = self._redactor.drain()
        if rest:
            self._stream.write(rest.encode("utf-8", "surrogateescape"))
        self._stream.flush()

    def close(self) -> None:
        self.flush()
        self._stream.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_stream"], name)


class RedactingLogFilter(logging.Filter):
    """Redacts every log record, message and traceback alike.

    The stream wrappers cannot cover logging on their own. A handler that
    captured ``sys.stderr`` when it was constructed keeps writing to the
    real stream no matter what is installed afterwards — which is exactly
    what the MCP server's own ``RichHandler`` does, since FastMCP
    configures logging at import. Meanwhile the orchestrators log raw
    exception text (``sync fetch failed for %s: %s``), and a Moodle error
    payload quoting the request back is precisely how a token gets into
    one.

    Attached to a *handler* rather than a logger, so it applies to
    everything that handler emits, including records that propagated up
    from a child logger.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - a broken record formats later
            return True
        cleaned = redact_text(message)
        if cleaned != message:
            record.msg = cleaned
            # The message is already interpolated; leaving the arguments
            # would interpolate it a second time.
            record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = "".join(
                traceback.format_exception(*record.exc_info)
            )
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if getattr(record, "stack_info", None):
            record.stack_info = redact_text(record.stack_info)
        return True


def install_log_redaction() -> RedactingLogFilter:
    """Attach :class:`RedactingLogFilter` to every handler in this process.

    Covers the root logger's handlers (FastMCP installs one at import),
    the root logger itself, and ``logging.lastResort`` — the handler that
    prints a warning when nothing else is configured, which is the CLI's
    normal case. Idempotent: a handler already carrying the filter is left
    alone, so calling this twice does not double-redact.
    """
    log_filter = RedactingLogFilter()
    targets: list[Any] = [logging.getLogger()]
    targets.extend(logging.getLogger().handlers)
    if logging.lastResort is not None:
        targets.append(logging.lastResort)
    for target in targets:
        if not any(
            isinstance(existing, RedactingLogFilter)
            for existing in getattr(target, "filters", [])
        ):
            target.addFilter(log_filter)
    return log_filter


def remove_log_redaction() -> None:
    """Detach every :class:`RedactingLogFilter` again (tests and resets)."""
    targets: list[Any] = [logging.getLogger()]
    targets.extend(logging.getLogger().handlers)
    if logging.lastResort is not None:
        targets.append(logging.lastResort)
    for target in targets:
        for existing in list(getattr(target, "filters", [])):
            if isinstance(existing, RedactingLogFilter):
                target.removeFilter(existing)


__all__ = [
    "MIN_SECRET_LENGTH",
    "REDACTED",
    "STREAM_HOLD_CHARS",
    "RedactingBytesStream",
    "RedactingLogFilter",
    "RedactingStream",
    "forget_secrets",
    "install_log_redaction",
    "known_secrets",
    "redact_payload",
    "redact_text",
    "redact_url",
    "remember_secret",
    "remove_log_redaction",
]
