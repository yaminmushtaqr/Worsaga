"""Startup banner for worsaga.

Displays a styled welcome banner using Rich (default path),
falling back to ANSI escape codes if Rich is unavailable.

Wide terminals (>=72 cols) get a large shaded ASCII-art banner.
Narrow terminals get a compact panel that still looks intentional.
"""

from __future__ import annotations

import os
import shutil
import sys


# ── Version ──────────────────────────────────────────────────────

def _get_version() -> str:
    """Return the package version.

    Uses the ``__version__`` attribute defined in the package ``__init__.py``
    so the reported version always matches the source code being executed —
    even in editable installs where stale ``.dist-info`` metadata may linger.
    Falls back to ``importlib.metadata`` (normal pip installs where __init__
    might theoretically be stripped), then ``"dev"``.
    """
    try:
        from worsaga import __version__
        if isinstance(__version__, str) and __version__:
            return __version__
    except Exception:
        pass
    try:
        from importlib.metadata import version
        ver = version("worsaga")
        if isinstance(ver, str) and ver:
            return ver
    except Exception:
        pass
    return "dev"


# ── Display predicates ───────────────────────────────────────────

def should_show_banner(
    *,
    json_mode: bool = False,
    quiet: bool = False,
    force: bool = False,
) -> bool:
    """Return True when the banner should be printed.

    Suppressed when:
    - ``--json`` mode is active (machine output must be clean)
    - ``-q`` / ``--quiet`` flag is set
    - stdout is not a TTY (piped / redirected), unless *force* is True
    """
    if json_mode or quiet:
        return False
    if force:
        return True
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


# ── Terminal width helper ────────────────────────────────────────

_LARGE_BANNER_MIN_WIDTH = 72


def _get_terminal_width() -> int:
    """Return the current terminal width in columns."""
    try:
        return shutil.get_terminal_size((80, 24)).columns
    except Exception:
        return 80


def _use_large_banner(width: int | None = None) -> bool:
    """Return True if the terminal is wide enough for the large banner."""
    if width is None:
        width = _get_terminal_width()
    return width >= _LARGE_BANNER_MIN_WIDTH


# ── ASCII art ────────────────────────────────────────────────────

_LOGO_LINES = [
    r" ██╗    ██╗ ██████╗ ██████╗ ███████╗ █████╗  ██████╗  █████╗ ",
    r" ██║    ██║██╔═══██╗██╔══██╗██╔════╝██╔══██╗██╔════╝ ██╔══██╗",
    r" ██║ █╗ ██║██║   ██║██████╔╝███████╗███████║██║  ███╗███████║",
    r" ██║███╗██║██║   ██║██╔══██╗╚════██║██╔══██║██║   ██║██╔══██║",
    r" ╚███╔███╔╝╚██████╔╝██║  ██║███████║██║  ██║╚██████╔╝██║  ██║",
    r"  ╚══╝╚══╝  ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝",
]

_SUBTITLE = "r e a d - o n l y   m o o d l e   t o o l k i t"


# ── Rich banner ──────────────────────────────────────────────────

_HAS_RICH = False
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    _HAS_RICH = True
except ImportError:
    pass


def _rich_banner_large(*, version: str) -> None:
    """Print the large shaded ASCII-art banner using Rich."""
    console = Console(highlight=False)

    body = Text()

    # Gradient shading across logo lines (dark red spectrum)
    shades = [
        "bold rgb(190,35,35)",
        "bold rgb(175,30,30)",
        "bold rgb(160,25,25)",
        "bold rgb(145,22,22)",
        "bold rgb(130,18,18)",
        "bold rgb(115,15,15)",
    ]
    for i, line in enumerate(_LOGO_LINES):
        body.append(line, style=shades[i])
        body.append("\n")

    # Subtitle centered under the art
    body.append("\n")
    body.append(f"  {_SUBTITLE}", style="rgb(160,50,50)")
    body.append(f"   v{version}", style="dim rgb(130,45,45)")
    body.append("\n")

    # Tagline
    body.append("  Read-only Moodle toolkit", style="rgb(150,55,55)")
    body.append("  —  for students and agents", style="dim rgb(120,45,45)")
    body.append("\n")

    panel = Panel(
        body,
        border_style="rgb(100,15,15)",
        padding=(0, 1),
        width=70,
    )
    console.print(panel)


def _rich_banner_compact(*, version: str) -> None:
    """Print a compact Rich banner for narrow terminals."""
    console = Console(highlight=False)

    title = Text()
    title.append("  w", style="bold rgb(180,30,30)")
    title.append("o", style="bold rgb(160,25,25)")
    title.append("r", style="bold rgb(145,20,20)")
    title.append("s", style="bold rgb(130,18,18)")
    title.append("a", style="bold rgb(120,15,15)")
    title.append("g", style="bold rgb(110,12,12)")
    title.append("a", style="bold rgb(100,10,10)")

    ver = Text(f"  v{version}", style="dim rgb(140,50,50)")
    tagline = Text("  Read-only Moodle toolkit", style="rgb(160,60,60)")
    detail = Text("  for students and agents", style="dim rgb(120,50,50)")

    body = Text.assemble(title, ver, "\n", tagline, "\n", detail, "\n")

    panel = Panel(
        body,
        border_style="rgb(100,15,15)",
        padding=(0, 1),
        width=42,
    )
    console.print(panel)


# ── ANSI fallback banner ────────────────────────────────────────

# 256-color dark reds: 88 = #870000, 124 = #af0000, 52 = #5f0000
_R = "\033[38;5;124m"   # dark red text
_DR = "\033[38;5;88m"   # deeper red
_DIM = "\033[38;5;52m"  # very dark red
_B = "\033[1m"           # bold
_RST = "\033[0m"         # reset


def _ansi_banner(*, version: str) -> None:
    """Print the banner using ANSI escape codes (no Rich)."""
    lines = [
        f"{_DR}  ┌{'─' * 38}┐{_RST}",
        f"{_DR}  │{_RST}  {_B}{_R}worsaga{_RST}  {_DIM}v{version:<22}{_DR}  │{_RST}",
        f"{_DR}  │{_RST}  {_R}Read-only Moodle toolkit{_RST}            {_DR}│{_RST}",
        f"{_DR}  │{_RST}  {_DIM}for students and agents{_RST}             {_DR}│{_RST}",
        f"{_DR}  └{'─' * 38}┘{_RST}",
    ]
    sys.stdout.write("\n".join(lines) + "\n")


# ── Public API ───────────────────────────────────────────────────

def print_banner(*, force_ansi: bool = False, width: int | None = None) -> None:
    """Print the startup banner to stdout.

    Uses Rich when available, ANSI 256-color otherwise.
    Set *force_ansi* to always use the ANSI path (useful for testing).
    Pass *width* to override terminal-width detection (useful for testing).
    """
    ver = _get_version()
    if _HAS_RICH and not force_ansi:
        if _use_large_banner(width):
            _rich_banner_large(version=ver)
        else:
            _rich_banner_compact(version=ver)
    else:
        _ansi_banner(version=ver)
