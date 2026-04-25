"""Tests for course content traversal and material discovery."""

import pytest

from worsaga.materials import (
    extract_materials,
    extract_week_number,
    get_section_materials,
    match_section,
    search_course_content,
)


# ── Fixtures: realistic Moodle section data ──────────────────────


def _make_file(filename, mimetype="application/pdf", size=1024, ftype="file"):
    return {
        "type": ftype,
        "filename": filename,
        "fileurl": f"https://moodle.example.com/pluginfile.php/0/{filename}",
        "filesize": size,
        "mimetype": mimetype,
        "timemodified": 1700000000,
    }


SAMPLE_SECTIONS = [
    {
        "id": 100,
        "name": "General",
        "section": 0,
        "modules": [
            {
                "id": 1001,
                "name": "Course Handbook",
                "modname": "resource",
                "url": "https://moodle.example.com/mod/resource/view.php?id=1001",
                "contents": [_make_file("handbook.pdf", size=5000)],
            },
            {
                "id": 1002,
                "name": "Useful Links",
                "modname": "url",
                "url": "https://moodle.example.com/mod/url/view.php?id=1002",
                # no contents array — this is a URL module
            },
        ],
    },
    {
        "id": 101,
        "name": "Week 1: Introduction to Economics",
        "section": 1,
        "modules": [
            {
                "id": 1010,
                "name": "Lecture Slides",
                "modname": "resource",
                "url": "https://moodle.example.com/mod/resource/view.php?id=1010",
                "contents": [
                    _make_file("week1_slides.pdf", size=2048000),
                ],
            },
            {
                "id": 1011,
                "name": "Seminar Notes",
                "modname": "resource",
                "url": "https://moodle.example.com/mod/resource/view.php?id=1011",
                "contents": [
                    _make_file("week1_seminar.pdf", size=512000),
                ],
            },
            {
                "id": 1012,
                "name": "Weekly Quiz",
                "modname": "quiz",
                "url": "https://moodle.example.com/mod/quiz/view.php?id=1012",
                # quiz modules typically have no contents
            },
        ],
    },
    {
        "id": 102,
        "name": "Week 2: Supply and Demand",
        "section": 2,
        "modules": [
            {
                "id": 1020,
                "name": "Lecture Slides",
                "modname": "resource",
                "url": "https://moodle.example.com/mod/resource/view.php?id=1020",
                "contents": [
                    _make_file("week2_slides.pdf", size=3000000),
                    _make_file("week2_extra.pdf", size=100000),
                ],
            },
            {
                "id": 1021,
                "name": "Supplementary Reading",
                "modname": "folder",
                "url": "https://moodle.example.com/mod/folder/view.php?id=1021",
                "contents": [
                    _make_file("reading1.pdf"),
                    _make_file("reading2.pdf"),
                ],
            },
        ],
    },
    {
        "id": 103,
        "name": "Week 10: Revision",
        "section": 10,
        "modules": [
            {
                "id": 1030,
                "name": "Revision Pack",
                "modname": "resource",
                "contents": [_make_file("revision.pdf")],
            },
        ],
    },
]

COURSE_ID = 42


# ── extract_week_number ──────────────────────────────────────────


class TestExtractWeekNumber:
    @pytest.mark.parametrize(
        "name, expected",
        [
            ("Week 1: Introduction", 1),
            ("Week 01 - Lecture notes", 1),
            ("week 12", 12),
            ("Wk 3", 3),
            ("wk3", 3),
            ("Topic 5: Markets", 5),
            ("Session 7", 7),
            ("Lecture 4 slides", 4),
            ("Week 10: Revision", 10),
        ],
    )
    def test_extracts_number(self, name, expected):
        assert extract_week_number(name) == expected

    @pytest.mark.parametrize(
        "name",
        [
            "General",
            "Course Information",
            "",
            "Assessment Overview",
        ],
    )
    def test_returns_none_for_no_match(self, name):
        assert extract_week_number(name) is None


# ── match_section ────────────────────────────────────────────────


class TestMatchSection:
    def test_int_does_not_match_plain_section_number_without_week_label(self):
        section = {"name": "General", "section": 0}
        assert match_section(section, 0) is False
        assert match_section(section, 1) is False

    def test_int_matches_extracted_week(self):
        section = {"name": "Week 1: Introduction", "section": 1}
        assert match_section(section, 1) is True

    def test_int_uses_week_label_not_raw_section_index(self):
        section = {"name": "Week 3: Markets", "section": 5}
        assert match_section(section, 3) is True
        assert match_section(section, 5) is False

    def test_numeric_query_ignores_unrelated_section_slot(self):
        section = {"name": "Using Generative AI", "section": 1}
        assert match_section(section, 1) is False
        assert match_section(section, "1") is False

    def test_string_numeric_treated_as_int(self):
        section = {"name": "Week 2: Supply", "section": 2}
        assert match_section(section, "2") is True

    def test_string_substring_match(self):
        section = {"name": "Week 1: Introduction to Economics", "section": 1}
        assert match_section(section, "Introduction") is True
        assert match_section(section, "introduction") is True  # case insensitive
        assert match_section(section, "Revision") is False

    def test_no_match(self):
        section = {"name": "General", "section": 0}
        assert match_section(section, 99) is False
        assert match_section(section, "Nonexistent") is False


# ── extract_materials ────────────────────────────────────────────


class TestExtractMaterials:
    def test_extracts_files_from_resource_modules(self):
        materials = extract_materials(SAMPLE_SECTIONS, COURSE_ID)
        filenames = [m["file_name"] for m in materials]
        assert "handbook.pdf" in filenames
        assert "week1_slides.pdf" in filenames
        assert "week2_extra.pdf" in filenames

    def test_extracts_url_modules_without_contents(self):
        materials = extract_materials(SAMPLE_SECTIONS, COURSE_ID)
        url_records = [m for m in materials if m["module_name"] == "Useful Links"]
        assert len(url_records) == 1
        assert url_records[0]["module_type"] == "url"
        assert url_records[0]["file_name"] == ""

    def test_skips_non_file_content_types(self):
        """Content entries with type='content' (HTML body) should be skipped."""
        sections = [
            {
                "id": 200,
                "name": "Test",
                "section": 0,
                "modules": [
                    {
                        "id": 2001,
                        "name": "A Page",
                        "modname": "page",
                        "contents": [
                            {"type": "content", "filename": "index.html", "fileurl": ""},
                            _make_file("attachment.pdf"),
                        ],
                    },
                ],
            },
        ]
        materials = extract_materials(sections, COURSE_ID)
        assert len(materials) == 1
        assert materials[0]["file_name"] == "attachment.pdf"

    def test_record_structure(self):
        materials = extract_materials(SAMPLE_SECTIONS, COURSE_ID)
        rec = next(m for m in materials if m["file_name"] == "week1_slides.pdf")
        assert rec["course_id"] == COURSE_ID
        assert rec["section_name"] == "Week 1: Introduction to Economics"
        assert rec["section_num"] == 1
        assert rec["module_name"] == "Lecture Slides"
        assert rec["module_type"] == "resource"
        assert rec["file_size"] == 2048000
        assert rec["mime_type"] == "application/pdf"
        assert rec["dedupe_key"].startswith("1010:week1_slides.pdf:")
        assert "/pluginfile.php/0/week1_slides.pdf" in rec["dedupe_key"]

    def test_no_view_url_without_base_url(self):
        materials = extract_materials(SAMPLE_SECTIONS, COURSE_ID)
        assert "view_url" not in materials[0]

    def test_view_url_with_base_url(self):
        base = "https://moodle.example.com"
        materials = extract_materials(SAMPLE_SECTIONS, COURSE_ID, base_url=base)
        rec = next(m for m in materials if m["file_name"] == "week1_slides.pdf")
        assert rec["view_url"] == f"{base}/mod/resource/view.php?id=1010"

    def test_view_url_for_url_module(self):
        base = "https://moodle.example.com"
        materials = extract_materials(SAMPLE_SECTIONS, COURSE_ID, base_url=base)
        url_rec = next(m for m in materials if m["module_name"] == "Useful Links")
        assert url_rec["view_url"] == f"{base}/mod/url/view.php?id=1002"

    def test_dedupe_suppresses_duplicates(self):
        """Same module+filename appearing twice should be deduped."""
        dup_file = _make_file("dup.pdf")
        sections = [
            {
                "id": 300,
                "name": "Section A",
                "section": 0,
                "modules": [
                    {"id": 3001, "name": "File", "modname": "resource", "contents": [dup_file]},
                ],
            },
            {
                "id": 301,
                "name": "Section B",
                "section": 1,
                "modules": [
                    {"id": 3001, "name": "File", "modname": "resource", "contents": [dup_file]},
                ],
            },
        ]
        with_dedupe = extract_materials(sections, COURSE_ID, dedupe=True)
        without_dedupe = extract_materials(sections, COURSE_ID, dedupe=False)
        assert len(with_dedupe) == 1
        assert len(without_dedupe) == 2

    def test_dedupe_keeps_same_filename_at_different_urls(self):
        """Same module+filename can still represent distinct folder files."""
        sections = [
            {
                "id": 300,
                "name": "Section A",
                "section": 0,
                "modules": [
                    {
                        "id": 3001,
                        "name": "Folder",
                        "modname": "folder",
                        "contents": [
                            {
                                **_make_file("slides.pdf"),
                                "fileurl": "https://moodle.example.com/pluginfile.php/0/a/slides.pdf",
                            },
                            {
                                **_make_file("slides.pdf"),
                                "fileurl": "https://moodle.example.com/pluginfile.php/0/b/slides.pdf",
                            },
                        ],
                    },
                ],
            },
        ]

        materials = extract_materials(sections, COURSE_ID, dedupe=True)

        assert len(materials) == 2
        assert len({m["dedupe_key"] for m in materials}) == 2

    def test_empty_sections(self):
        assert extract_materials([], COURSE_ID) == []

    def test_section_with_no_modules(self):
        sections = [{"id": 400, "name": "Empty", "section": 0, "modules": []}]
        assert extract_materials(sections, COURSE_ID) == []

    def test_folder_module_with_multiple_files(self):
        materials = extract_materials(SAMPLE_SECTIONS, COURSE_ID)
        folder_files = [m for m in materials if m["module_id"] == 1021]
        assert len(folder_files) == 2
        assert {m["file_name"] for m in folder_files} == {"reading1.pdf", "reading2.pdf"}


# ── get_section_materials ────────────────────────────────────────


class TestGetSectionMaterials:
    def test_week_1_by_number(self):
        mats = get_section_materials(SAMPLE_SECTIONS, COURSE_ID, 1)
        assert all(m["section_num"] == 1 for m in mats)
        filenames = [m["file_name"] for m in mats]
        assert "week1_slides.pdf" in filenames
        assert "week1_seminar.pdf" in filenames

    def test_week_2_by_string(self):
        mats = get_section_materials(SAMPLE_SECTIONS, COURSE_ID, "2")
        assert all(m["section_num"] == 2 for m in mats)

    def test_by_name_substring(self):
        mats = get_section_materials(SAMPLE_SECTIONS, COURSE_ID, "Supply")
        assert len(mats) > 0
        assert all("Supply" in m["section_name"] for m in mats)

    def test_no_match_returns_empty(self):
        assert get_section_materials(SAMPLE_SECTIONS, COURSE_ID, 99) == []

    def test_week_10_does_not_match_week_1(self):
        """Ensure 'Week 10' is not confused with week 1."""
        mats_1 = get_section_materials(SAMPLE_SECTIONS, COURSE_ID, 1)
        mats_10 = get_section_materials(SAMPLE_SECTIONS, COURSE_ID, 10)
        names_1 = {m["section_name"] for m in mats_1}
        names_10 = {m["section_name"] for m in mats_10}
        assert "Week 10: Revision" not in names_1
        assert "Week 10: Revision" in names_10

    def test_base_url_propagated(self):
        base = "https://moodle.example.com"
        mats = get_section_materials(SAMPLE_SECTIONS, COURSE_ID, 1, base_url=base)
        assert all("view_url" in m for m in mats)


# ── search_course_content ────────────────────────────────────────


class TestSearchCourseContent:
    def test_search_by_module_name(self):
        results = search_course_content(SAMPLE_SECTIONS, "Lecture Slides")
        assert len(results) == 2  # Week 1 + Week 2 both have "Lecture Slides"

    def test_search_by_section_name_returns_all_modules(self):
        results = search_course_content(SAMPLE_SECTIONS, "Introduction")
        # "Week 1: Introduction to Economics" matches — all its modules returned
        module_names = {r["module_name"] for r in results}
        assert "Lecture Slides" in module_names
        assert "Seminar Notes" in module_names
        assert "Weekly Quiz" in module_names

    def test_search_case_insensitive(self):
        results = search_course_content(SAMPLE_SECTIONS, "lecture slides")
        assert len(results) == 2

    def test_search_no_match(self):
        assert search_course_content(SAMPLE_SECTIONS, "Nonexistent") == []

    def test_search_deduplicates_module_ids(self):
        """A module matching both section and module name should appear once."""
        sections = [
            {
                "id": 500,
                "name": "Slides Overview",
                "section": 0,
                "modules": [
                    {"id": 5001, "name": "Slides Pack", "modname": "resource", "url": ""},
                ],
            },
        ]
        results = search_course_content(sections, "Slides")
        assert len(results) == 1

    def test_search_result_structure(self):
        results = search_course_content(SAMPLE_SECTIONS, "Handbook")
        assert len(results) == 1
        r = results[0]
        assert r["section_name"] == "General"
        assert r["section_num"] == 0
        assert r["module_id"] == 1001
        assert r["module_name"] == "Course Handbook"
        assert r["module_type"] == "resource"

    def test_empty_sections(self):
        assert search_course_content([], "anything") == []
