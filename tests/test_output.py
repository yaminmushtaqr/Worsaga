"""Tests for structured output helpers."""

import importlib
import json
from unittest.mock import patch

import pytest

from worsaga.output import format_table, render_json, render_structured, render_yaml


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
