"""Tests for text extraction and boilerplate filtering."""

import io
import zipfile

import pytest

from worsaga.extraction import (
    clean_text,
    extract_docx_text,
    extract_file_structured,
    extract_file_text,
    extract_pptx_text,
    extract_txt_text,
    is_boilerplate,
    strip_html,
)


# ── strip_html ──────────────────────────────────────────────────


class TestStripHtml:
    def test_removes_tags(self):
        assert strip_html("<p>Hello <b>world</b></p>") == "Hello world"

    def test_decodes_entities(self):
        assert strip_html("&amp; &lt; &gt;") == "& < >"

    def test_normalizes_whitespace(self):
        assert strip_html("<p>  lots   of   space  </p>") == "lots of space"

    def test_empty_string(self):
        assert strip_html("") == ""

    def test_plain_text_unchanged(self):
        assert strip_html("no html here") == "no html here"


# ── is_boilerplate ──────────────────────────────────────────────


class TestIsBoilerplate:
    @pytest.mark.parametrize(
        "line",
        [
            "",
            "   ",
            "42",
            "Page 3",
            "slide 12",
            "p. 5",
            "© 2024 Example University",
            "Copyright 2024",
            "=========",
            "---",
            "ab",
            "hi",  # short single word < 6 chars
            "|||a|||b|||c|||",  # table-like
            "https://example.com",
            "user@example.com",
            ".....",
            "All Rights Reserved",
            "Click to edit",
            "12,345.67",
            "\t\t\t\t",
        ],
    )
    def test_detects_boilerplate(self, line):
        assert is_boilerplate(line) is True

    @pytest.mark.parametrize(
        "line",
        [
            # End-of-slide lines
            "Questions?",
            "Any questions?",
            "Thank you",
            "Thank you!",
            "Q & A",
            "Q&A",
            "End of lecture",
            "See you next week!",
            # Speaker / professor lines
            "Dr. Jane Smith",
            "Prof. Michael Brown",
            "Professor Anderson",
            # Academic term / date lines
            "Autumn Term 2025",
            "Michaelmas Term",
            "Spring Semester",
            "AT 2025",
            "WT 2026",
            "2025",
            "2025-26",
            # Course code only
            "CS210",
            "ECON101_2526",
            "STAT120",
            # Institutional boilerplate
            "Department of Management",
            "School of Economics",
            "Faculty of Social Sciences",
        ],
    )
    def test_detects_slide_boilerplate(self, line):
        assert is_boilerplate(line) is True

    @pytest.mark.parametrize(
        "line",
        [
            "Content you share will be processed in accordance with Lovable terms",
            "A survey will be used to collect that consent.",
        ],
    )
    def test_detects_consent_and_survey_boilerplate(self, line):
        assert is_boilerplate(line) is True

    def test_detects_garbled_text(self):
        """Lines with very low alphabetic ratio are garbled artifacts."""
        assert is_boilerplate("##$%^& @#$%^& *&^%") is True
        assert is_boilerplate("12.34 / 56.78 % 90.12") is True

    @pytest.mark.parametrize(
        "line",
        [
            "The theory of supply and demand explains market equilibrium.",
            "Key insight: marginal cost equals marginal revenue at optimum.",
            "EQUILIBRIUM",  # uppercase acronym >= 6 chars
            "Students should review chapter 4 before the seminar.",
            "Dr. Smith argues that markets tend toward efficiency over time.",
            "The CS210 framework distinguishes between three types of capital.",
        ],
    )
    def test_allows_content(self, line):
        assert is_boilerplate(line) is False


# Educational-context lines: preserved by the default tier, stripped
# only by the aggressive tier used in the summary-bullet pipeline.
EDUCATIONAL_CONTEXT_LINES = [
    # Slide structural elements
    "Outline",
    "Agenda",
    "Overview",
    "Today's Plan",
    "Learning Objectives",
    "Objectives:",
    "Road Map",
    "Key Takeaways",
    "In this lecture",
    # Week/lecture headings
    "Week 8: Power and politics in organisations",
    "Lecture 4 - Market structures",
    # Attribution lines
    "— Smith, 2019",
    "– Johnson et al.",
    "Source: Textbook, Ch. 5",
    "Adapted from: Jones (2020)",
    "Reference: Annual Report 2024",
    # Reading list / reference headers
    "Required Reading",
    "Recommended Readings:",
    "Further Reading",
    "Further References",
    "Suggested References:",
    "Key Readings",
    "Additional Materials",
    # Figure/table captions
    "Figure 3.1",
    "Fig. 2",
    "Table 4",
    "Chart 1: Revenue",
    "Diagram 5",
    # Standalone citation markers ("[1]" alone stays core noise via the
    # short-single-word rule)
    "[2, 3]",
    # Bibliography entries
    (
        "Portocarrero, F. F., Newbert, S. L., Young, M. J., "
        "& Zhu, L. Y. (2025). The affective revolution in "
        "entrepreneurship."
    ),
    "Smith, A. B., & Jones, C. D. (2020). Market dynamics.",
    "Johnson, A. B. (2019). The theory of institutional change.",
]


class TestBoilerplateTiers:
    @pytest.mark.parametrize("line", EDUCATIONAL_CONTEXT_LINES)
    def test_default_preserves_educational_context(self, line):
        assert is_boilerplate(line) is False

    @pytest.mark.parametrize("line", EDUCATIONAL_CONTEXT_LINES)
    def test_aggressive_strips_educational_context(self, line):
        assert is_boilerplate(line, aggressive=True) is True

    @pytest.mark.parametrize(
        "line",
        [
            "Page 3",
            "© 2024 Example University",
            "https://example.com",
            "All Rights Reserved",
        ],
    )
    def test_core_noise_stripped_in_both_tiers(self, line):
        assert is_boilerplate(line) is True
        assert is_boilerplate(line, aggressive=True) is True


# ── clean_text ──────────────────────────────────────────────────


class TestCleanText:
    def test_removes_boilerplate_lines(self):
        text = "Important content here.\nPage 1\nAnother good line here.\n42"
        result = clean_text(text)
        assert "Important content here." in result
        assert "Another good line here." in result
        assert "Page 1" not in result
        assert "\n42" not in result

    def test_collapses_blank_lines(self):
        text = "Line A\n\n\n\nLine B"
        result = clean_text(text)
        assert result == "Line A\n\nLine B"

    def test_empty_input(self):
        assert clean_text("") == ""

    def test_all_boilerplate(self):
        text = "Page 1\n42\nhttps://example.com"
        assert clean_text(text) == ""

    def test_suppresses_repeated_headers(self):
        """Lines appearing 3+ times (per-slide headers) are removed."""
        text = (
            "Lecture 3: Markets\n"
            "Supply creates its own demand.\n\n"
            "Lecture 3: Markets\n"
            "Equilibrium is where supply meets demand.\n\n"
            "Lecture 3: Markets\n"
            "Price signals coordinate economic activity."
        )
        result = clean_text(text)
        assert "Lecture 3: Markets" not in result
        assert "Supply creates its own demand" in result
        assert "Equilibrium" in result
        assert "Price signals" in result

    def test_suppresses_short_lines_repeated_twice(self):
        """Short lines appearing 2+ times (per-slide footers) are removed."""
        text = (
            "Topic: Trade\n"
            "Countries benefit from specialisation and exchange.\n\n"
            "Topic: Trade\n"
            "Comparative advantage drives international trade patterns."
        )
        result = clean_text(text)
        # "Topic: Trade" is short (< 40 chars) and appears 2 times
        assert "Topic: Trade" not in result
        assert "Countries benefit" in result
        assert "Comparative advantage" in result

    def test_default_preserves_educational_content(self):
        """M3 flip: captions, sources, objectives, and references survive."""
        text = (
            "Learning Objectives\n"
            "Understand how markets reach equilibrium.\n"
            "Figure 3.1\n"
            "Source: Textbook, Ch. 5\n"
            "Required Reading\n"
            "Smith, A. B., & Jones, C. D. (2020). Market dynamics.\n"
            "Page 3\n"
        )
        result = clean_text(text)
        assert "Learning Objectives" in result
        assert "Figure 3.1" in result
        assert "Source: Textbook, Ch. 5" in result
        assert "Required Reading" in result
        assert "Smith, A. B., & Jones, C. D. (2020)" in result
        # Core noise is still removed
        assert "Page 3" not in result

    def test_aggressive_strips_educational_content(self):
        text = (
            "Learning Objectives\n"
            "Understand how markets reach equilibrium.\n"
            "Figure 3.1\n"
            "Source: Textbook, Ch. 5\n"
            "Required Reading\n"
        )
        result = clean_text(text, aggressive=True)
        assert "Understand how markets reach equilibrium." in result
        assert "Learning Objectives" not in result
        assert "Figure 3.1" not in result
        assert "Source: Textbook, Ch. 5" not in result
        assert "Required Reading" not in result


# ── extract_txt_text ────────────────────────────────────────────


class TestExtractTxt:
    def test_utf8_bytes(self):
        assert extract_txt_text(b"Hello world") == "Hello world"

    def test_empty_bytes(self):
        assert extract_txt_text(b"") == ""

    def test_ignores_invalid_utf8(self):
        result = extract_txt_text(b"Good \xff\xfe text")
        assert "Good" in result
        assert "text" in result


# ── extract_pptx_text ───────────────────────────────────────────


def _make_pptx(slides: list[list[str]]) -> bytes:
    """Build a minimal PPTX (zip) with slide XML containing text runs."""
    a_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    p_ns = "http://schemas.openxmlformats.org/presentationml/2006/main"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i, paragraphs in enumerate(slides, 1):
            from xml.etree.ElementTree import Element, SubElement, tostring

            sld = Element(f"{{{p_ns}}}sld")
            cSld = SubElement(sld, f"{{{p_ns}}}cSld")
            spTree = SubElement(cSld, f"{{{p_ns}}}spTree")
            sp = SubElement(spTree, f"{{{p_ns}}}sp")
            txBody = SubElement(sp, f"{{{p_ns}}}txBody")
            for para_text in paragraphs:
                p_el = SubElement(txBody, f"{{{a_ns}}}p")
                r_el = SubElement(p_el, f"{{{a_ns}}}r")
                t_el = SubElement(r_el, f"{{{a_ns}}}t")
                t_el.text = para_text
            zf.writestr(f"ppt/slides/slide{i}.xml", tostring(sld))
    return buf.getvalue()


class TestExtractPptx:
    def test_extracts_slide_text(self):
        data = _make_pptx([["Hello", "World"], ["Slide two"]])
        result = extract_pptx_text(data)
        assert "Hello" in result
        assert "World" in result
        assert "Slide two" in result

    def test_extracts_slides_in_numeric_order(self):
        data = _make_pptx([[f"S{i:02d} content"] for i in range(1, 11)])
        result = extract_pptx_text(data)
        assert result.index("S02 content") < result.index("S10 content")

    def test_empty_pptx(self):
        data = _make_pptx([])
        assert extract_pptx_text(data) == ""

    def test_invalid_data(self):
        assert extract_pptx_text(b"not a zip") == ""


# ── extract_docx_text ───────────────────────────────────────────


def _make_docx(paragraphs: list[str]) -> bytes:
    """Build a minimal DOCX (zip) with document.xml containing text."""
    w_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    from xml.etree.ElementTree import Element, SubElement, tostring

    doc = Element(f"{{{w_ns}}}document")
    body = SubElement(doc, f"{{{w_ns}}}body")
    for para_text in paragraphs:
        p_el = SubElement(body, f"{{{w_ns}}}p")
        r_el = SubElement(p_el, f"{{{w_ns}}}r")
        t_el = SubElement(r_el, f"{{{w_ns}}}t")
        t_el.text = para_text
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", tostring(doc))
    return buf.getvalue()


class TestExtractDocx:
    def test_extracts_paragraphs(self):
        data = _make_docx(["First paragraph", "Second paragraph"])
        result = extract_docx_text(data)
        assert "First paragraph" in result
        assert "Second paragraph" in result

    def test_empty_docx(self):
        data = _make_docx([])
        assert extract_docx_text(data) == ""

    def test_missing_document_xml(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("other.xml", "<root/>")
        assert extract_docx_text(buf.getvalue()) == ""

    def test_invalid_data(self):
        assert extract_docx_text(b"not a zip") == ""


# ── extract_file_text (router) ──────────────────────────────────


class TestPdfPageCap:
    def test_pdf_extraction_stops_at_max_pages(self):
        pytest.importorskip("fitz")
        from worsaga.demo import _render_pdf
        from worsaga.extraction import MAX_PDF_PAGES, extract_pdf_text

        pages = [[f"unique-page-marker-{i}"] for i in range(MAX_PDF_PAGES + 10)]
        text = extract_pdf_text(_render_pdf(pages))
        assert f"unique-page-marker-{MAX_PDF_PAGES - 1}" in text
        assert f"unique-page-marker-{MAX_PDF_PAGES}" not in text


class TestExtractFileText:
    def test_routes_txt(self):
        assert extract_file_text(b"hello", "notes.txt") == "hello"

    def test_routes_pptx(self):
        data = _make_pptx([["Test slide content"]])
        result = extract_file_text(data, "slides.pptx")
        assert "Test slide content" in result

    def test_routes_docx(self):
        data = _make_docx(["Test doc content"])
        result = extract_file_text(data, "doc.docx")
        assert "Test doc content" in result

    def test_unsupported_extension(self):
        assert extract_file_text(b"data", "image.png") == ""

    def test_max_chars_truncation(self):
        data = b"x" * 1000
        result = extract_file_text(data, "big.txt", max_chars=50)
        assert len(result) == 50

    def test_clean_flag(self):
        # "42" on its own line is boilerplate
        data = b"Important content line here.\n42\nAnother good line here."
        result = extract_file_text(data, "notes.txt", clean=True)
        assert "Important content line here." in result
        assert "\n42\n" not in result

    def test_case_insensitive_extension(self):
        assert extract_file_text(b"hello", "notes.TXT") == "hello"

    def test_empty_data(self):
        assert extract_file_text(b"", "empty.txt") == ""


# ── extract_file_structured ─────────────────────────────────────


PAGE_KEYS = {
    "page", "text", "markdown", "image_count",
    "has_low_text_density", "warnings",
}


class TestExtractFileStructured:
    def test_txt_single_page(self):
        result = extract_file_structured(b"Some study notes here.", "notes.txt")
        assert result["filename"] == "notes.txt"
        assert result["file_type"] == "txt"
        assert result["warnings"] == []
        assert len(result["pages"]) == 1
        page = result["pages"][0]
        assert set(page) == PAGE_KEYS
        assert page["page"] == 1
        assert page["text"] == "Some study notes here."
        assert page["image_count"] == 0
        assert page["has_low_text_density"] is True
        assert page["warnings"] == []

    def test_pptx_page_per_slide(self):
        data = _make_pptx([["Slide one title", "Point A"], ["Slide two title"]])
        result = extract_file_structured(data, "slides.pptx")
        assert result["file_type"] == "pptx"
        assert [p["page"] for p in result["pages"]] == [1, 2]
        assert "Point A" in result["pages"][0]["text"]
        assert "Slide two title" in result["pages"][1]["text"]

    def test_unsupported_extension_warns(self):
        result = extract_file_structured(b"data", "image.png")
        assert result["pages"] == []
        assert any("Unsupported" in w for w in result["warnings"])

    def test_empty_data_gives_no_pages(self):
        result = extract_file_structured(b"", "empty.txt")
        assert result["pages"] == []

    def test_max_chars_truncates_with_warning(self):
        data = ("word " * 200).encode()
        result = extract_file_structured(data, "big.txt", max_chars=50)
        assert len(result["pages"][0]["text"]) == 50
        assert any("truncated" in w.lower() for w in result["warnings"])

    def test_clean_default_preserves_educational_content(self):
        data = b"Learning Objectives\nMarkets reach equilibrium.\nPage 3\n"
        result = extract_file_structured(data, "notes.txt", clean=True)
        text = result["pages"][0]["text"]
        assert "Learning Objectives" in text
        assert "Page 3" not in text

    def test_markdown_heading_and_bullets(self):
        data = (
            "Week 3 Overview\n"
            "• Supply and demand basics\n"
            "- Elasticity of demand\n"
            "Ordinary paragraph text continues here.\n"
        ).encode()
        result = extract_file_structured(data, "notes.txt")
        md = result["pages"][0]["markdown"]
        assert "## Week 3 Overview" in md
        assert "- Supply and demand basics" in md
        assert "- Elasticity of demand" in md
        assert "Ordinary paragraph text continues here." in md

    def test_pdf_pages_and_density(self):
        pytest.importorskip("fitz")
        from worsaga.demo import _render_pdf

        long_line = "Substantive lecture content about market equilibrium. "
        pages = [
            [long_line * 2] * 5,  # > 200 chars once joined
            ["Sparse slide"],
        ]
        result = extract_file_structured(_render_pdf(pages), "week3-slides.pdf")
        assert result["file_type"] == "pdf"
        assert len(result["pages"]) == 2
        assert result["pages"][0]["has_low_text_density"] is False
        assert result["pages"][1]["has_low_text_density"] is True
        assert result["pages"][0]["image_count"] == 0

    def test_pdf_page_cap_warns(self):
        pytest.importorskip("fitz")
        from worsaga.demo import _render_pdf
        from worsaga.extraction import MAX_PDF_PAGES

        pages = [[f"page-{i}"] for i in range(MAX_PDF_PAGES + 5)]
        result = extract_file_structured(_render_pdf(pages), "big.pdf")
        assert len(result["pages"]) == MAX_PDF_PAGES
        assert any("first" in w and str(MAX_PDF_PAGES) in w
                   for w in result["warnings"])

    def test_invalid_pdf_warns(self):
        pytest.importorskip("fitz")
        result = extract_file_structured(b"not a pdf", "bad.pdf")
        assert result["pages"] == []
        assert any("could not be parsed" in w for w in result["warnings"])
