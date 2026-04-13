"""Tests for text extraction and boilerplate filtering."""

import io
import zipfile

import pytest

from worsaga.extraction import (
    clean_text,
    extract_docx_text,
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
            "© 2024 LSE",
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
            "MG488",
            "EC100_2526",
            "MG434",
            # Institutional boilerplate
            "Department of Management",
            "London School of Economics",
            "School of Economics",
            "LSE",
        ],
    )
    def test_detects_slide_boilerplate(self, line):
        assert is_boilerplate(line) is True

    @pytest.mark.parametrize(
        "line",
        [
            # Attribution lines
            "\u2014 Smith, 2019",
            "\u2013 Johnson et al.",
            "Source: Textbook, Ch. 5",
            "Adapted from: Jones (2020)",
            "Reference: Annual Report 2024",
        ],
    )
    def test_detects_attribution_boilerplate(self, line):
        assert is_boilerplate(line) is True

    @pytest.mark.parametrize(
        "line",
        [
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
            # Standalone citation markers
            "[1]",
            "[2, 3]",
        ],
    )
    def test_detects_reference_and_caption_boilerplate(self, line):
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
            # Full APA-style bibliography entry
            (
                "Portocarrero, F. F., Newbert, S. L., Young, M. J., "
                "& Zhu, L. Y. (2025). The affective revolution in "
                "entrepreneurship."
            ),
            # Shorter APA reference
            "Smith, A. B., & Jones, C. D. (2020). Market dynamics.",
            # Single author with multiple initials
            "Johnson, A. B. (2019). The theory of institutional change.",
        ],
    )
    def test_detects_bibliography_entries(self, line):
        assert is_boilerplate(line) is True

    @pytest.mark.parametrize(
        "line",
        [
            "The theory of supply and demand explains market equilibrium.",
            "Key insight: marginal cost equals marginal revenue at optimum.",
            "EQUILIBRIUM",  # uppercase acronym >= 6 chars
            "Students should review chapter 4 before the seminar.",
            "Dr. Smith argues that markets tend toward efficiency over time.",
            "The MG488 framework distinguishes between three types of capital.",
        ],
    )
    def test_allows_content(self, line):
        assert is_boilerplate(line) is False


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
