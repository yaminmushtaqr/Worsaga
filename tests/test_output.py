"""Tests for structured output helpers."""

import importlib
import json
from unittest.mock import patch

import pytest

from worsaga.output import (
    format_table,
    render_json,
    render_structured,
    render_yaml,
    truncate_cell,
)


def test_render_json_is_pretty_json():
    rendered = render_json({"a": 1})
    assert json.loads(rendered) == {"a": 1}
    assert "\n" in rendered


def test_render_yaml_requires_pyyaml_when_missing():
    real_import = importlib.import_module

    def _fake_import(name):
        if name == "yaml":
            raise ImportError("missing")
        return real_import(name)

    with patch("importlib.import_module", side_effect=_fake_import):
        with pytest.raises(RuntimeError, match="worsaga\\[yaml\\]"):
            render_yaml({"a": 1})


def test_render_structured_rejects_both_json_and_yaml():
    with pytest.raises(ValueError, match="cannot be used together"):
        render_structured({}, json_mode=True, yaml_mode=True)


def test_format_table_uses_declared_columns():
    rendered = format_table(
        [{"name": "Essay", "status": "graded"}],
        [("name", "Item", 8), ("status", "Status", 8)],
    )
    assert "Item" in rendered
    assert "Essay" in rendered
    assert "graded" in rendered


def test_truncate_cell_short_value_unchanged():
    assert truncate_cell("Essay", 10) == "Essay"
    assert truncate_cell("exactly-ten", 11) == "exactly-ten"


def test_truncate_cell_marks_loss_with_ascii_ellipsis():
    result = truncate_cell("Week 3 - Elasticity and Market Responses", 30)
    assert result == "Week 3 - Elasticity and Mar..."
    assert len(result) == 30
    # ASCII '...' only — never the Unicode ellipsis (cp1252-safe).
    assert "…" not in result
    assert result.isascii()


def test_truncate_cell_narrow_column_hard_cuts():
    # No room for the 3-char marker: fall back to a hard cut.
    assert truncate_cell("abcdef", 3) == "abc"
    assert truncate_cell("abcdef", 2) == "ab"


def test_format_table_truncates_wide_data_cells_with_indicator():
    rendered = format_table(
        [{"name": "A really quite long assignment name here"}],
        [("name", "Item", 12)],
    )
    body = rendered.splitlines()[-1]
    assert body.startswith("A really ...")
    assert "..." in body


def test_render_yaml_emits_non_ascii_literally():
    pytest.importorskip("yaml")
    # allow_unicode=True: an en-dash stays an en-dash instead of an
    # escaped – sequence (Issue 5).
    rendered = render_yaml({"label": "weeks 3–4 revision"})
    assert "–" in rendered
    assert "\\u2013" not in rendered


def test_render_yaml_non_ascii_survives_cp1252_roundtrip_with_replace():
    pytest.importorskip("yaml")
    # A char cp1252 cannot encode (snowman) must not crash a legacy console:
    # _reconfigure_streams sets errors="replace", so encoding degrades to a
    # replacement byte instead of raising.
    rendered = render_yaml({"label": "snow ☃ man"})
    assert "☃" in rendered
    encoded = rendered.encode("cp1252", errors="replace")
    assert b"?" in encoded  # the snowman degraded, nothing crashed
