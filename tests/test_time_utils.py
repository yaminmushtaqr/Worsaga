"""Tests for shared time helpers."""

from datetime import datetime, timezone

import pytest

from worsaga.time_utils import (
    calculate_days_left,
    parse_since,
    timestamp_to_display,
    timestamp_to_iso,
)


def test_timestamp_to_iso_uses_utc():
    assert timestamp_to_iso(0) == ""
    assert timestamp_to_iso(1700000000).endswith("+00:00")


def test_timestamp_to_display_with_utc_tz_is_compact():
    assert timestamp_to_display(1700000000, tz=timezone.utc) == "Nov 14 22:13 UTC"


def test_timestamp_to_display_uses_local_zone_with_offset_label():
    import re

    result = timestamp_to_display(1700000000)
    # Local-zone rendering: always labelled UTC or UTC±HH:MM.
    assert re.fullmatch(r"[A-Z][a-z]{2} \d{2} \d{2}:\d{2} UTC([+-]\d{2}:\d{2})?", result)


def test_timestamp_to_display_empty_for_missing():
    assert timestamp_to_display(None) == ""
    assert timestamp_to_display(0) == ""


def test_calculate_days_left_handles_missing_and_future():
    assert calculate_days_left(None, now=1000) is None
    assert calculate_days_left(1000 + 3 * 86400, now=1000) == 3


def test_parse_since_relative_days_and_hours():
    assert parse_since("7d", now=1000) == 1000 - 7 * 86400
    assert parse_since("24h", now=1000) == 1000 - 24 * 3600


def test_parse_since_absolute_date():
    expected = int(datetime(2026, 4, 29, tzinfo=timezone.utc).timestamp())
    assert parse_since("2026-04-29", now=1000) == expected


def test_parse_since_rejects_bad_value():
    with pytest.raises(ValueError, match="--since"):
        parse_since("last week", now=1000)
