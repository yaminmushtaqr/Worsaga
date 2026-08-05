"""Shared structured-output and compact table helpers."""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterable, Sequence
from typing import Any


def wants_structured(args: Any) -> bool:
    """Return True when an argparse namespace requested JSON or YAML."""
    return bool(getattr(args, "json", False) or getattr(args, "yaml", False))


def render_json(payload: Any) -> str:
    """Render a deterministic JSON payload."""
    return json.dumps(payload, indent=2)


def render_yaml(payload: Any) -> str:
    """Render YAML when PyYAML is installed.

    ``allow_unicode=True`` emits UTF-8 characters literally instead of
    ``\\uXXXX`` escapes, so an en-dash stays an en-dash. This is safe on
    legacy consoles because :func:`worsaga.cli._reconfigure_streams` sets
    ``errors="replace"`` on stdout/stderr, so a character the console
    encoding cannot represent degrades to a replacement rather than
    crashing the command.
    """
    try:
        yaml = importlib.import_module("yaml")
    except ImportError as exc:
        raise RuntimeError(
            'YAML output requires PyYAML. Install it with: pip install "worsaga[yaml]"'
        ) from exc
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).rstrip()


def render_structured(payload: Any, *, json_mode: bool = False, yaml_mode: bool = False) -> str:
    """Render payload as JSON or YAML."""
    if json_mode and yaml_mode:
        raise ValueError("--json and --yaml cannot be used together")
    if yaml_mode:
        return render_yaml(payload)
    return render_json(payload)


def truncate_cell(value: Any, width: int) -> str:
    """Return *value* as text, marking any loss with an ASCII ``...``.

    A value longer than *width* keeps the column width fixed and replaces
    its final three characters with ``...`` so a cut name is never mistaken
    for the whole name. The indicator is ASCII on purpose — cosmetic output
    must stay cp1252-safe (the Unicode ellipsis would raise
    ``UnicodeEncodeError`` on legacy Windows consoles). Columns three
    characters or narrower have no room for the marker and are hard-cut.
    """
    text = str(value)
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def format_table(rows: Iterable[dict[str, Any]], columns: Sequence[tuple[str, str, int]]) -> str:
    """Render compact fixed-width table text.

    ``columns`` is a sequence of ``(key, header, width)`` tuples. Data cells
    wider than their column are truncated with an ASCII ``...`` indicator so
    a cut value is visibly cut (see :func:`truncate_cell`).
    """
    rows = list(rows)
    header = "  ".join(label[:width].ljust(width) for _, label, width in columns)
    rule = "  ".join("-" * width for _, _, width in columns)
    lines = [header, rule]
    for row in rows:
        cells = []
        for key, _, width in columns:
            cells.append(truncate_cell(row.get(key, ""), width).ljust(width))
        lines.append("  ".join(cells))
    return "\n".join(lines)
