"""Tests for structured output helpers."""

import ast
import importlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import worsaga
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


# ── ASCII-only runtime strings ──────────────────────────────────


#: Modules allowed to hold non-ASCII string constants, with the reason.
#: Each is either not output at all, or output that already handles its
#: own encoding.
_ASCII_EXEMPT_MODULES = {
    # Decorative block/box-drawing art, rendered through rich and only
    # when stdout is a TTY. rich handles terminal encoding itself.
    "banner",
    # Predicates and regexes over text extracted from PDFs and slide
    # decks: "does this line contain a smart quote / an ellipsis / a
    # copyright sign?". They have to be non-ASCII to match anything.
    "extraction",
    "summary_text",
}

#: Values allowed in any module. The BOM is stripped from *input*, never
#: written.
_ASCII_EXEMPT_VALUES = {"\ufeff"}


def _non_docstring_str_constants(tree):
    """Yield ``(lineno, value)`` for every string constant but docstrings.

    A docstring is prose for a developer reading the source; it never
    reaches a terminal, and the codebase writes it with typographic
    punctuation on purpose.
    """
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.lineno, node.value


class TestRuntimeStringsAreAscii:
    """No string that can reach a terminal may contain non-ASCII text.

    Worsaga runs on Windows consoles. A cp437 console cannot encode an
    em dash, a bullet, or a curly quote at all, and a cp1252 one encodes
    them to bytes that a UTF-8 terminal emulator then draws as
    replacement characters. Either way the user sees mojibake in a
    message meant to explain something to them.

    Enforced here rather than remembered, because "use a hyphen" is
    exactly the kind of rule that survives review and then quietly
    decays over the next twenty commits.
    """

    def _modules(self):
        root = Path(worsaga.__file__).parent
        return sorted(p for p in root.glob("*.py"))

    def test_every_module_is_checked(self):
        # Guards the guard: a typo in an exemption name would silently
        # excuse nothing, but a renamed module would silently escape.
        names = {p.stem for p in self._modules()}
        assert _ASCII_EXEMPT_MODULES <= names
        assert len(names) > 20

    def test_no_non_ascii_runtime_strings(self):
        offenders = []
        for path in self._modules():
            if path.stem in _ASCII_EXEMPT_MODULES:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for lineno, value in _non_docstring_str_constants(tree):
                if value in _ASCII_EXEMPT_VALUES:
                    continue
                try:
                    value.encode("ascii")
                except UnicodeEncodeError:
                    offenders.append(f"{path.name}:{lineno}: {value!r}")
        assert not offenders, (
            "non-ASCII runtime strings (use ASCII, or add a justified "
            "exemption):\n" + "\n".join(offenders)
        )

    def test_the_guard_actually_catches_something(self):
        # A test that can never fail is not a test.
        tree = ast.parse('x = "an em dash \u2014 here"\n')
        values = [v for _lineno, v in _non_docstring_str_constants(tree)]
        assert values == ["an em dash \u2014 here"]
        with pytest.raises(UnicodeEncodeError):
            values[0].encode("ascii")

    def test_docstrings_are_not_flagged(self):
        tree = ast.parse('"""Module prose \u2014 with a dash."""\nx = "plain"\n')
        values = [v for _lineno, v in _non_docstring_str_constants(tree)]
        assert values == ["plain"]
