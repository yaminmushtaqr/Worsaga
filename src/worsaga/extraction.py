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
# Legacy binary formats (.ppt, .doc) have no extractor and are therefore
# not supported — listing them here would let unextractable files consume
# the summary pipeline's download budget.
FILE_PRIORITY: dict[str, int] = {
    ".pdf": 0,
    ".pptx": 1,
    ".docx": 2,
    ".txt": 3,
}

SUPPORTED_EXTENSIONS = frozenset(FILE_PRIORITY)

# ── OOXML archive safety budgets ─────────────────────────────────
#
# PPTX/DOCX are ZIP archives; a small download can decompress into a
# huge XML payload (zip bomb). Parsing is bounded by these budgets and
# aborted with a structured warning when exceeded.

MAX_PPTX_SLIDES = 300
_MAX_ARCHIVE_MEMBERS = 10_000
_MAX_XML_MEMBER_BYTES = 20 * 1024 * 1024
_MAX_TOTAL_XML_BYTES = 100 * 1024 * 1024


class _OoxmlLimitError(Exception):
    """Raised when an OOXML archive exceeds a safety budget."""


def _read_xml_member(zf: zipfile.ZipFile, name: str, state: dict) -> bytes:
    """Read one XML member with per-member and cumulative size caps.

    ``state`` carries the running ``total`` of decompressed bytes for
    the archive. Reads are capped on actual decompressed output, not on
    the (spoofable) sizes declared in the archive directory.
    """
    with zf.open(name) as member:
        data = member.read(_MAX_XML_MEMBER_BYTES + 1)
    if len(data) > _MAX_XML_MEMBER_BYTES:
        raise _OoxmlLimitError(
            "Archive member expands beyond the safety limit; "
            "extraction stopped."
        )
    state["total"] = state.get("total", 0) + len(data)
    if state["total"] > _MAX_TOTAL_XML_BYTES:
        raise _OoxmlLimitError(
            "Archive expands beyond the total safety limit; "
            "extraction stopped."
        )
    return data


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


def clean_text(
    text: str,
    *,
    aggressive: bool = False,
    line_frequencies: dict[str, int] | None = None,
) -> str:
    """Remove boilerplate lines, repeated headers, and normalize whitespace.

    The default keeps educational content — figure/table captions,
    source lines, learning objectives, agenda/outline slides, and
    references. Pass ``aggressive=True`` to also strip those (used by
    the summary-bullet pipeline).

    ``line_frequencies`` supplies precomputed lowercase-line counts for
    repeated-header detection. Per-page callers (structured extraction)
    pass document-wide counts here — a header repeated on every slide
    would otherwise appear only once per cleaning call and survive.
    """
    lines = text.split("\n")

    # Pass 1: count line frequencies to detect repeated headers.
    # Lines appearing 3+ times are almost certainly per-slide headers.
    if line_frequencies is not None:
        freq = line_frequencies
    else:
        freq = {}
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


_PML_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_DML_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_WML_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_REL_ATTR_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _pptx_slide_sort_key(name: str) -> tuple[int, str]:
    """Sort PowerPoint slide XML paths by slide number."""
    match = re.search(r"/slide(\d+)\.xml$", name)
    if match:
        return (int(match.group(1)), name)
    return (0, name)


def _pptx_slide_order(zf: zipfile.ZipFile, state: dict) -> list[str]:
    """Return slide XML paths in true presentation order.

    Slide order comes from ``presentation.xml``'s ``sldIdLst`` resolved
    through the relationship table — slides can be reordered in an
    editor without renaming their XML parts, so ``slideN.xml`` numbering
    is only a fallback.
    """
    names = set(zf.namelist())
    fallback = sorted(
        (
            n for n in names
            if n.startswith("ppt/slides/slide") and n.endswith(".xml")
        ),
        key=_pptx_slide_sort_key,
    )
    try:
        presentation = ET.fromstring(
            _read_xml_member(zf, "ppt/presentation.xml", state)
        )
        relationships = ET.fromstring(
            _read_xml_member(zf, "ppt/_rels/presentation.xml.rels", state)
        )
    except (KeyError, ET.ParseError, _OoxmlLimitError):
        return fallback

    id_to_target: dict[str, str] = {}
    for relationship in relationships.iter(f"{_PKG_REL_NS}Relationship"):
        target = str(relationship.get("Target") or "")
        normalized = (
            target.lstrip("/") if target.startswith("/")
            else str(PurePosixPath("ppt") / target)
        )
        id_to_target[str(relationship.get("Id") or "")] = normalized

    ordered: list[str] = []
    for slide_id in presentation.iter(f"{_PML_NS}sldId"):
        target = id_to_target.get(str(slide_id.get(f"{_REL_ATTR_NS}id") or ""))
        if target and target in names:
            ordered.append(target)
    return ordered or fallback


def _pptx_structured(data: bytes) -> tuple[list[dict], list[str]]:
    """Extract raw per-slide records from PPTX bytes.

    Every slide produces a page record — image-only and empty slides
    are kept (with their real ``image_count``) so page numbers match
    the deck. Parse failures and safety-budget hits become structured
    warnings, never silent empties.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError):
        return [], ["PPTX could not be parsed."]
    warnings: list[str] = []
    pages: list[dict] = []
    with archive as zf:
        if len(zf.namelist()) > _MAX_ARCHIVE_MEMBERS:
            return [], [
                "PPTX archive has too many members to process safely."
            ]
        state: dict = {"total": 0}
        slide_names = _pptx_slide_order(zf, state)
        if len(slide_names) > MAX_PPTX_SLIDES:
            warnings.append(
                f"Only the first {MAX_PPTX_SLIDES} of {len(slide_names)} "
                "slides were extracted."
            )
        for slide_name in slide_names[:MAX_PPTX_SLIDES]:
            try:
                xml_bytes = _read_xml_member(zf, slide_name, state)
            except KeyError:
                pages.append({
                    "text": "", "image_count": 0,
                    "warnings": ["Slide data is missing from the archive."],
                })
                continue
            except _OoxmlLimitError as exc:
                warnings.append(str(exc))
                break
            try:
                root = ET.fromstring(xml_bytes)
            except ET.ParseError:
                pages.append({
                    "text": "", "image_count": 0,
                    "warnings": ["Slide could not be parsed."],
                })
                continue
            paragraphs: list[str] = []
            for p_elem in root.iter(f"{_DML_NS}p"):
                runs = [t.text for t in p_elem.iter(f"{_DML_NS}t") if t.text]
                line = "".join(runs).strip()
                if line:
                    paragraphs.append(line)
            image_count = sum(1 for _ in root.iter(f"{_PML_NS}pic"))
            pages.append({
                "text": "\n".join(paragraphs),
                "image_count": image_count,
                "warnings": [],
            })
    return pages, warnings


def _pptx_slide_texts(data: bytes) -> list[str]:
    """Return per-slide text from PPTX bytes (non-empty slides only)."""
    pages, _ = _pptx_structured(data)
    return [page["text"] for page in pages if page["text"].strip()]


def extract_pptx_text(data: bytes) -> str:
    """Extract text from PPTX bytes via zipfile + slide XML parsing."""
    return "\n\n".join(_pptx_slide_texts(data))


def _docx_structured(data: bytes) -> tuple[list[dict], list[str]]:
    """Extract the document body from DOCX bytes as one page record."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError):
        return [], ["DOCX could not be parsed."]
    with archive as zf:
        if len(zf.namelist()) > _MAX_ARCHIVE_MEMBERS:
            return [], [
                "DOCX archive has too many members to process safely."
            ]
        if "word/document.xml" not in zf.namelist():
            return [], ["DOCX has no document body."]
        try:
            xml_bytes = _read_xml_member(zf, "word/document.xml", {"total": 0})
        except _OoxmlLimitError as exc:
            return [], [str(exc)]
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError:
            return [], ["DOCX could not be parsed."]
        lines: list[str] = []
        for p_elem in root.iter(f"{_WML_NS}p"):
            runs = [t.text for t in p_elem.iter(f"{_WML_NS}t") if t.text]
            line = "".join(runs).strip()
            if line:
                lines.append(line)
    text = "\n".join(lines)
    if not text:
        return [], []
    return [{"text": text, "image_count": 0, "warnings": []}], []


def extract_docx_text(data: bytes) -> str:
    """Extract text from DOCX bytes via document.xml parsing."""
    pages, _ = _docx_structured(data)
    return pages[0]["text"] if pages else ""


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
        if ext in {".ppt", ".doc"}:
            result["warnings"].append(
                f"Legacy format {ext} is not supported; "
                f"convert the file to {ext}x."
            )
        else:
            result["warnings"].append(
                f"Unsupported file type: {ext or 'no extension'}."
            )
        return result

    if ext == ".pdf":
        raw_pages, doc_warnings = _pdf_structured_pages(data)
    elif ext == ".pptx":
        raw_pages, doc_warnings = _pptx_structured(data)
    elif ext == ".docx":
        raw_pages, doc_warnings = _docx_structured(data)
    else:
        text = _EXTRACTORS[ext](data)
        raw_pages = (
            [{"text": text, "image_count": 0, "warnings": []}] if text else []
        )
        doc_warnings = []
    result["warnings"].extend(doc_warnings)

    # Repeated headers/footers recur across pages, so frequency counting
    # must span the whole document — per-page counts would never reach
    # the repetition threshold.
    line_frequencies: dict[str, int] | None = None
    if clean and len(raw_pages) > 1:
        line_frequencies = {}
        for raw in raw_pages:
            for raw_line in raw["text"].split("\n"):
                key = raw_line.strip().lower()
                if key:
                    line_frequencies[key] = line_frequencies.get(key, 0) + 1

    budget = max_chars if max_chars and max_chars > 0 else None
    truncated = False
    for number, raw in enumerate(raw_pages, 1):
        text = raw["text"].strip()
        if clean:
            text = clean_text(
                text, aggressive=aggressive, line_frequencies=line_frequencies,
            )
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
