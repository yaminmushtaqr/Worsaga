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
    """Render YAML when PyYAML is installed."""
    try:
        yaml = importlib.import_module("yaml")
    except ImportError as exc:
        raise RuntimeError(
            'YAML output requires PyYAML. Install it with: pip install "worsaga[yaml]"'
        ) from exc
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=False).rstrip()


def render_structured(payload: Any, *, json_mode: bool = False, yaml_mode: bool = False) -> str:
    """Render payload as JSON or YAML."""
    if json_mode and yaml_mode:
        raise ValueError("--json and --yaml cannot be used together")
    if yaml_mode:
        return render_yaml(payload)
    return render_json(payload)


def format_table(rows: Iterable[dict[str, Any]], columns: Sequence[tuple[str, str, int]]) -> str:
    """Render compact fixed-width table text.

    ``columns`` is a sequence of ``(key, header, width)`` tuples.
    """
    rows = list(rows)
    header = "  ".join(label[:width].ljust(width) for _, label, width in columns)
    rule = "  ".join("-" * width for _, _, width in columns)
    lines = [header, rule]
    for row in rows:
        cells = []
        for key, _, width in columns:
            value = str(row.get(key, ""))
            cells.append(value[:width].ljust(width))
        lines.append("  ".join(cells))
    return "\n".join(lines)
