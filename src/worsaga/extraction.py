"""Text extraction from course material files and HTML stripping.


Supports PDF, PPTX, DOCX, and TXT. All operations work on in-memory
bytes — no temp files, no disk writes.

PDF extraction requires PyMuPDF (``pip install pymupdf``). PPTX and
DOCX are parsed via the stdlib ``zipfile`` + ``xml.etree`` — no
third-party dependency needed.

All functions are read-only. Nothing is written to Moodle or disk.
"""

from __future__ import annotations

import html as _html_mod
import io
import re
import zipfile
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET


# ── Constants ────────────────────────────────────────────────────

MAX_TEXT_PER_FILE = 120_000  # max chars returned per file

# Supported extensions in download priority order (lower = preferred).
FILE_PRIORITY: dict[str, int] = {
    ".pdf": 0,
    ".pptx": 1,
    ".ppt": 2,
    ".docx": 3,
    ".doc": 4,
    ".txt": 5,
}

SUPPORTED_EXTENSIONS = frozenset(FILE_PRIORITY)


# ── HTML stripping ───────────────────────────────────────────────

def strip_html(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# ── Boilerplate detection ────────────────────────────────────────

def is_boilerplate(line: str) -> bool:
    """Return True if *line* is likely boilerplate or noise."""
    line = line.strip()
    if not line or len(line) < 3:
        return True
    if line.isdigit():
        return True
    if "\u00a9" in line or "copyright" in line.lower():
        return True
    if re.match(r"^(page|slide|p\.?)\s*\d+$", line, re.IGNORECASE):
        return True
    if re.match(r"^[=\-_*#]{3,}$", line):
        return True
    # Single short word (unless plausible acronym >5 chars)
    if " " not in line and len(line) < 6 and not line.isupper():
        return True
    # Table-like noise
    if line.count("|") >= 3 or line.count("\t") >= 3:
        return True
    if re.match(r"^[\d\s.,/%$\xa3\u20ac\-+]+$", line):
        return True
    # URLs and email addresses
    if re.match(r"^https?://", line):
        return True
    if "@" in line and "." in line and " " not in line:
        return True
    # Decorative lines
    if re.match(r"^[.\-_=*~#\s]{4,}$", line):
        return True
    # Common slide boilerplate
    _boiler_kw = (
        "all rights reserved", "confidential", "do not distribute",
        "click to edit", "insert title", "placeholder",
    )
    if any(kw in line.lower() for kw in _boiler_kw):
        return True

    lower = line.lower().strip()

    # Slide structural elements (headings that are not content)
    if re.match(
        r"^(outline|agenda|overview|contents|today['\u2019]?s?\s+plan|"
        r"learning\s+objectives?|objectives?|goals?\s+for\s+today|"
        r"road\s*map|structure|plan|recap|key\s+takeaways?|"
        r"what\s+we\s+cover(ed)?|in\s+this\s+lecture)[\s:.\-]*$",
        lower,
    ):
        return True

    # End-of-slide / end-of-deck lines
    if re.match(
        r"^(questions?\??|any\s+questions?\??|thank\s+you!?|thanks!?|"
        r"the\s+end|q\s*&\s*a|end\s+of\s+(lecture|session|slides?)|"
        r"see\s+you\s+next\s+week!?)$",
        lower,
    ):
        return True

    # Professor / speaker name lines (short, starts with title)
    if (
        re.match(r"^(prof\.?|professor|dr\.?|mr\.?|ms\.?|mrs\.?)\s+\w+", lower)
        and len(line) < 50
    ):
        return True

    # Academic term / date header lines
    if re.match(
        r"^(autumn|spring|summer|winter|michaelmas|lent|hilary|trinity)"
        r"\s+(term|semester)",
        lower,
    ):
        return True
    if re.match(r"^(at|wt|st|mt|lt|ht)\s+20\d{2}", lower):
        return True
    # Bare year or term-year lines: "2025", "2025-26", "AT 2025/26"
    if re.match(r"^20\d{2}[\s/\-]*\d{0,2}$", lower):
        return True

    # Course-code-only lines: "MG488", "EC100_2526"
    if re.match(r"^[A-Z]{2,5}\d{3,4}[\w]*$", line.strip()) and len(line) < 25:
        return True

    # Department / institution boilerplate
    if re.match(
        r"^(department\s+of|school\s+of|faculty\s+of|"
        r"london\s+school\s+of\s+economics|lse)\b",
        lower,
    ):
        return True

    # Schedule/date-time lines: "Thursday, 16th October 12-1.30 pm"
    if re.match(
        r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
        r"[,\s]+\d{1,2}(st|nd|rd|th)?\s+\w+\s+\d",
        lower,
    ):
        return True

    # Standalone month names (schedule/timeline fragments)
    if re.match(
        r"^(january|february|march|april|may|june|july|august|"
        r"september|october|november|december)\s*:?\s*$",
        lower,
    ):
        return True

    # Slide / outline week headings: "Week 8: Power...", "Lecture 4 - ..."
    if re.match(
        r'^(week|lecture|seminar|session|topic)\s+\d+\s*[:\-–].{0,80}$',
        lower,
    ):
        return True

    # Assessment / quiz prompt markers.
    if re.match(r'^\(?multiple\s+choice\)?', lower):
        return True

    # Attribution lines: "— Author" or "– Source, Year"
    if re.match(r'^[\u2014\u2013\-]\s+[A-Z]', line) and len(line) < 60:
        return True
    # Source/reference citation markers
    if re.match(
        r'^(source|ref|reference|adapted from|based on|cited in)\s*:',
        lower,
    ):
        return True

    # Reading list / reference section headers
    if re.match(
        r'^(required|recommended|further|additional|suggested|key|essential)\s+'
        r'(readings?|references?|texts?|materials?|bibliography|sources?)'
        r'\s*:?\s*$',
        lower,
    ):
        return True

    # Figure/table/chart caption lines
    if re.match(r'^(figure|fig\.?|table|chart|diagram|exhibit)\s+\d', lower):
        return True

    # Standalone numbered citation markers: [1], [2,3]
    if re.match(r'^\[[\d,\s]+\]\s*$', line.strip()):
        return True

    # Academic bibliography/reference entry (APA, Harvard style)
    # "Author, A. B., ... (YYYY). Title..."
    if (re.search(r'[A-Z]\w+,\s+[A-Z]\.', line) and
            re.search(r'\((?:19|20)\d{2}\w?\)', line) and
            len(re.findall(r'\b[A-Z]\.', line)) >= 2):
        return True

    if "pp." in lower and ("edition" in lower or "vol" in lower or "press" in lower):
        return True
    if re.match(r"^\(\d{4}\)\s+[\"'\u201c\u2018]", line):
        return True
    if "doi.org" in lower or "et al." in lower:
        return True

    # Chapter / reading-list citation entries.
    if re.match(r'^\(?(?:19|20)\d{2}\)?\s*[:.\-]?\s*["“]?chapter\s+\d+', lower):
        return True

    # Consent / ToS / survey-logistics / platform-admin lines
    if re.search(
        r'\b(?:in accordance with|terms of service|terms and conditions|'
        r'privacy policy|processed in accordance|commercially sensitive)\b',
        lower,
    ):
        return True
    if re.search(r'\bsurvey\b', lower) and re.search(r'\bconsent\b', lower):
        return True

    # Garbled extraction artifacts (very low alphabetic ratio)
    if len(line) > 15:
        alpha_count = sum(1 for c in line if c.isalpha())
        if alpha_count / len(line) < 0.35:
            return True

    return False


def clean_text(text: str) -> str:
    """Remove boilerplate lines, repeated headers, and normalize whitespace."""
    lines = text.split("\n")

    # Pass 1: count line frequencies to detect repeated headers.
    # Lines appearing 3+ times are almost certainly per-slide headers.
    freq: dict[str, int] = {}
    for line in lines:
        key = line.strip().lower()
        if key:
            freq[key] = freq.get(key, 0) + 1

    cleaned: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue
        if is_boilerplate(stripped):
            continue
        # Suppress repeated lines: 3+ for any length, 2+ for short
        # lines (< 40 chars) that are almost always slide headers/footers.
        line_freq = freq.get(stripped.lower(), 0)
        if line_freq >= 3 or (line_freq >= 2 and len(stripped) < 40):
            continue
        cleaned.append(stripped)
    return "\n".join(cleaned).strip()


# ── Per-format extractors ────────────────────────────────────────

def extract_pdf_text(data: bytes) -> str:
    """Extract text from PDF bytes using PyMuPDF (fitz).

    Returns empty string if PyMuPDF is not installed or extraction fails.
    """
    try:
        import fitz  # type: ignore[import-untyped]
    except ImportError:
        return ""
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        pages: list[str] = []
        for page in doc:
            text = page.get_text()
            if text.strip():
                pages.append(text.strip())
        doc.close()
        return "\n\n".join(pages)
    except Exception:
        return ""


def extract_pptx_text(data: bytes) -> str:
    """Extract text from PPTX bytes via zipfile + slide XML parsing."""
    a_ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            slide_names = sorted(
                n for n in zf.namelist()
                if n.startswith("ppt/slides/slide") and n.endswith(".xml")
            )
            all_slides: list[str] = []
            for sn in slide_names:
                tree = ET.parse(zf.open(sn))
                paras: list[str] = []
                for p_elem in tree.iter(f"{a_ns}p"):
                    runs: list[str] = []
                    for t_elem in p_elem.iter(f"{a_ns}t"):
                        if t_elem.text:
                            runs.append(t_elem.text)
                    line = "".join(runs).strip()
                    if line:
                        paras.append(line)
                if paras:
                    all_slides.append("\n".join(paras))
            return "\n\n".join(all_slides)
    except Exception:
        return ""


def extract_docx_text(data: bytes) -> str:
    """Extract text from DOCX bytes via document.xml parsing."""
    w_ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if "word/document.xml" not in zf.namelist():
                return ""
            tree = ET.parse(zf.open("word/document.xml"))
            lines: list[str] = []
            for p_elem in tree.iter(f"{w_ns}p"):
                runs: list[str] = []
                for t_elem in p_elem.iter(f"{w_ns}t"):
                    if t_elem.text:
                        runs.append(t_elem.text)
                line = "".join(runs).strip()
                if line:
                    lines.append(line)
            return "\n".join(lines)
    except Exception:
        return ""


def extract_txt_text(data: bytes) -> str:
    """Decode plain-text bytes to a string."""
    try:
        return data.decode("utf-8", errors="ignore")
    except Exception:
        return ""


# ── Unified router ───────────────────────────────────────────────

_EXTRACTORS: dict[str, callable] = {
    ".pdf": extract_pdf_text,
    ".pptx": extract_pptx_text,
    ".docx": extract_docx_text,
    ".txt": extract_txt_text,
}


def extract_file_text(
    data: bytes,
    filename: str,
    *,
    max_chars: int = MAX_TEXT_PER_FILE,
    clean: bool = False,
) -> str:
    """Route to the correct extractor based on file extension.

    Parameters
    ----------
    data : bytes
        Raw file content.
    filename : str
        Used to determine format by extension.
    max_chars : int
        Truncate output to this many characters.
    clean : bool
        If True, run :func:`clean_text` to strip boilerplate.

    Returns empty string for unsupported formats or extraction failure.
    """
    ext = PurePosixPath(filename).suffix.lower()
    extractor = _EXTRACTORS.get(ext)
    if extractor is None:
        return ""
    text = extractor(data)
    if not text:
        return ""
    if clean:
        text = clean_text(text)
    return text[:max_chars] if max_chars else text
