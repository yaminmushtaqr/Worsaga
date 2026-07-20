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
MAX_PDF_PAGES = 150  # pages beyond this are not extracted

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

def is_boilerplate(line: str, *, aggressive: bool = False) -> bool:
    """Return True if *line* is likely boilerplate or noise.

    By default only genuine noise is flagged: page numbers, copyright
    lines, decorative rules, URLs, garbled artifacts, and similar.
    Educational context — figure/table captions, source and attribution
    lines, reading-list headers, bibliography entries, learning
    objectives, agenda/outline headings — is preserved.

    With ``aggressive=True``, those educational-context lines are also
    flagged. The summary-bullet pipeline uses this tier, where such
    lines make poor bullets; extraction output for reading keeps the
    light default.
    """
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

    # Course-code-only lines: "ECON101", "ECON101_2526"
    if re.match(r"^[A-Z]{2,5}\d{3,4}[\w]*$", line.strip()) and len(line) < 25:
        return True

    # Department / institution boilerplate
    if re.match(
        r"^(department\s+of|school\s+of|faculty\s+of|institute\s+of)\b",
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

    # Assessment / quiz prompt markers.
    if re.match(r'^\(?multiple\s+choice\)?', lower):
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

    if not aggressive:
        return False

    # ── Aggressive tier: educational context, stripped only on
    # request (summary-bullet pipeline). Kept by default so captions,
    # sources, objectives, and references survive extraction.

    # Slide structural elements (headings that are not content)
    if re.match(
        r"^(outline|agenda|overview|contents|today['\u2019]?s?\s+plan|"
        r"learning\s+objectives?|objectives?|goals?\s+for\s+today|"
        r"road\s*map|structure|plan|recap|key\s+takeaways?|"
        r"what\s+we\s+cover(ed)?|in\s+this\s+lecture)[\s:.\-]*$",
        lower,
    ):
        return True

    # Slide / outline week headings: "Week 8: Power...", "Lecture 4 - ..."
    if re.match(
        r'^(week|lecture|seminar|session|topic)\s+\d+\s*[:\-\u2013].{0,80}$',
        lower,
    ):
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

    return False


def clean_text(text: str, *, aggressive: bool = False) -> str:
    """Remove boilerplate lines, repeated headers, and normalize whitespace.

    The default keeps educational content — figure/table captions,
    source lines, learning objectives, agenda/outline slides, and
    references. Pass ``aggressive=True`` to also strip those (used by
    the summary-bullet pipeline).
    """
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
        if is_boilerplate(stripped, aggressive=aggressive):
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
        for page_index, page in enumerate(doc):
            if page_index >= MAX_PDF_PAGES:
                break
            text = page.get_text()
            if text.strip():
                pages.append(text.strip())
        doc.close()
        return "\n\n".join(pages)
    except Exception:
        return ""


def _pptx_slide_texts(data: bytes) -> list[str]:
    """Return per-slide text from PPTX bytes via zipfile + slide XML."""
    a_ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            slide_names = sorted(
                (
                    n for n in zf.namelist()
                    if n.startswith("ppt/slides/slide") and n.endswith(".xml")
                ),
                key=_pptx_slide_sort_key,
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
            return all_slides
    except Exception:
        return []


def extract_pptx_text(data: bytes) -> str:
    """Extract text from PPTX bytes via zipfile + slide XML parsing."""
    return "\n\n".join(_pptx_slide_texts(data))


def _pptx_slide_sort_key(name: str) -> tuple[int, str]:
    """Sort PowerPoint slide XML paths by slide number."""
    match = re.search(r"/slide(\d+)\.xml$", name)
    if match:
        return (int(match.group(1)), name)
    return (0, name)


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
    aggressive: bool = False,
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
        If True, run :func:`clean_text` to strip boilerplate. The
        default cleaning is light and preserves educational content.
    aggressive : bool
        With ``clean=True``, also strip captions, source lines,
        structural headings, and references (summary-pipeline tier).

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
        text = clean_text(text, aggressive=aggressive)
    return text[:max_chars] if max_chars else text


# ── Structured per-page extraction ───────────────────────────────
#
# Backend-agnostic page shape: every backend (PyMuPDF today, possibly
# Docling or a vision model later) must produce the same page dicts,
# so callers never depend on the extraction library.

LOW_TEXT_DENSITY_CHARS = 200  # pages with less text than this are flagged

IMAGE_WARNING = (
    "Some pages contain images or diagrams that may not be fully interpreted."
)


def _page_markdown(text: str) -> str:
    """Render extracted page text as light, deterministic Markdown.

    The first short heading-like line becomes an ``##`` heading and
    bullet-marker lines become Markdown list items. Everything else
    passes through unchanged — no structure is invented.
    """
    out: list[str] = []
    seen_content = False
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            out.append("")
            continue
        if (
            not seen_content
            and len(line) <= 80
            and not line.endswith((".", ":", ";", ","))
        ):
            out.append(f"## {line}")
            seen_content = True
            continue
        seen_content = True
        bullet = re.match(r"^[•◦▪‣·*]\s*(.+)$", line)
        if bullet is None:
            bullet = re.match(r"^[\-–]\s+(.+)$", line)
        if bullet:
            out.append(f"- {bullet.group(1)}")
        else:
            out.append(line)
    return "\n".join(out).strip()


def _pdf_structured_pages(data: bytes) -> tuple[list[dict], list[str]]:
    """Extract raw per-page records from PDF bytes via PyMuPDF.

    Returns ``(pages, warnings)`` where each page is an interim dict
    with ``text``, ``image_count``, and ``warnings`` keys.
    """
    try:
        import fitz  # type: ignore[import-untyped]
    except ImportError:
        return [], ["PDF extraction unavailable: PyMuPDF is not installed."]
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return [], ["PDF could not be parsed."]
    warnings: list[str] = []
    if doc.page_count > MAX_PDF_PAGES:
        warnings.append(
            f"Only the first {MAX_PDF_PAGES} of {doc.page_count} pages "
            "were extracted."
        )
    pages: list[dict] = []
    for page_index, page in enumerate(doc):
        if page_index >= MAX_PDF_PAGES:
            break
        try:
            text = page.get_text()
        except Exception:
            pages.append({
                "text": "",
                "image_count": 0,
                "warnings": ["Text extraction failed for this page."],
            })
            continue
        try:
            image_count = len(page.get_images(full=True))
        except Exception:
            image_count = 0
        pages.append({"text": text, "image_count": image_count, "warnings": []})
    doc.close()
    return pages, warnings


def extract_file_structured(
    data: bytes,
    filename: str,
    *,
    max_chars: int = MAX_TEXT_PER_FILE,
    clean: bool = False,
    aggressive: bool = False,
) -> dict:
    """Extract per-page structured content from a course material file.

    Returns a dict of the shape::

        {
          "filename": "week3-slides.pdf",
          "file_type": "pdf",
          "pages": [
            {
              "page": 1,
              "text": "...",
              "markdown": "...",
              "image_count": 2,
              "has_low_text_density": false,
              "warnings": []
            }
          ],
          "warnings": []
        }

    PDFs produce one entry per page, PPTX one per slide, DOCX/TXT a
    single entry. ``max_chars`` caps the total text across pages;
    cleaning follows the same light default as :func:`extract_file_text`.
    Unsupported formats and extraction failures are reported through
    the top-level ``warnings`` list, never as exceptions.
    """
    ext = PurePosixPath(filename).suffix.lower()
    result: dict = {
        "filename": filename,
        "file_type": ext.lstrip(".") or "unknown",
        "pages": [],
        "warnings": [],
    }
    if ext not in _EXTRACTORS:
        result["warnings"].append(
            f"Unsupported file type: {ext or 'no extension'}."
        )
        return result

    if ext == ".pdf":
        raw_pages, doc_warnings = _pdf_structured_pages(data)
    elif ext == ".pptx":
        raw_pages = [
            {"text": t, "image_count": 0, "warnings": []}
            for t in _pptx_slide_texts(data)
        ]
        doc_warnings = []
    else:
        text = _EXTRACTORS[ext](data)
        raw_pages = (
            [{"text": text, "image_count": 0, "warnings": []}] if text else []
        )
        doc_warnings = []
    result["warnings"].extend(doc_warnings)

    budget = max_chars if max_chars else None
    truncated = False
    for number, raw in enumerate(raw_pages, 1):
        text = raw["text"].strip()
        if clean:
            text = clean_text(text, aggressive=aggressive)
        if budget is not None:
            if budget <= 0:
                truncated = True
                break
            if len(text) > budget:
                text = text[:budget]
                truncated = True
            budget -= len(text)
        result["pages"].append({
            "page": number,
            "text": text,
            "markdown": _page_markdown(text),
            "image_count": raw["image_count"],
            "has_low_text_density": len(text) < LOW_TEXT_DENSITY_CHARS,
            "warnings": raw["warnings"],
        })
    if truncated:
        result["warnings"].append(
            f"Output truncated at {max_chars} characters."
        )
    if any(p["image_count"] for p in result["pages"]):
        result["warnings"].append(IMAGE_WARNING)
    return result
