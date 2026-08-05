"""Demo mode: built-in fake Moodle data, no credentials, no network.

Activated by the global ``--demo`` CLI flag or the ``WORSAGA_DEMO=1``
environment variable (the environment variable also covers the MCP
server, which has no CLI flags).

Everything here is fictional. The demo dataset uses invented course
codes (ECON101, CS210, PSY110, STAT120), invented staff and student
names, and URLs on the reserved ``.invalid`` top-level domain, so no
real institution, Moodle site, student, or course data can appear.
Demo PDFs are generated locally, byte-for-byte deterministic, and every
page is marked as fake data.

:class:`DemoMoodleClient` mirrors the subset of
:class:`worsaga.client.MoodleClient` used by the CLI commands and MCP
tools. It performs no network I/O; its ``call`` method always raises so
any accidental web-service use fails loudly.
"""

from __future__ import annotations

import copy
import functools
import os
import time
import urllib.parse

from worsaga.client import CourseNotFoundError

DEMO_BASE_URL = "https://moodle.demo.invalid"
DEMO_USERID = 7

_TRUTHY = {"1", "true", "yes", "on"}


def demo_mode_enabled() -> bool:
    """Return True when the WORSAGA_DEMO environment variable is set."""
    return os.environ.get("WORSAGA_DEMO", "").strip().lower() in _TRUTHY


# ─────────────────────────────────────────────────────────────────
# Deterministic fake PDF generation
#
# PyMuPDF's writer embeds a randomized trailer ID, so generated files
# would differ run to run. This minimal hand-rolled PDF writer produces
# identical bytes for identical input, needs no dependency, and its
# output is readable by worsaga's own PDF extractor.
# ─────────────────────────────────────────────────────────────────

# Placed at the top of every generated PDF. The word "placeholder" also
# makes worsaga's own extraction cleaner treat these lines as boilerplate,
# so the marker never surfaces in generated study-note bullets.
_FAKE_MARKER_LINES = (
    "FAKE DEMO DATA - placeholder file generated locally by Worsaga demo mode.",
    "Placeholder content only: not real course material, no real personal data.",
    "",
)

_LINES_PER_PAGE = 40


def _pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _render_pdf(pages: list[list[str]]) -> bytes:
    """Render pages of plain-text lines into deterministic PDF bytes."""
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}

    def add(num: int, body: bytes) -> None:
        offsets[num] = len(out)
        out.extend(f"{num} 0 obj\n".encode("ascii"))
        out.extend(body)
        out.extend(b"\nendobj\n")

    n_pages = len(pages)
    kids = " ".join(f"{4 + i * 2} 0 R" for i in range(n_pages))
    add(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    add(2, f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode("ascii"))
    add(3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for i, lines in enumerate(pages):
        page_num = 4 + i * 2
        content_num = page_num + 1
        add(page_num, (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_num} 0 R >>"
        ).encode("ascii"))
        parts = ["BT /F1 11 Tf 72 770 Td 16 TL"]
        for j, line in enumerate(lines):
            if j:
                parts.append("T*")
            parts.append(f"({_pdf_escape(line)}) Tj")
        parts.append("ET")
        stream = "\n".join(parts).encode("ascii", "replace")
        add(content_num, (
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\n"
            b"stream\n" + stream + b"\nendstream"
        ))

    total = 3 + n_pages * 2
    xref_pos = len(out)
    out.extend(f"xref\n0 {total + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for num in range(1, total + 1):
        out.extend(f"{offsets[num]:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {total + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


# Fictional PDF content, keyed by filename. Every file gets the fake-data
# marker prepended to its first page. ASCII only (the writer emits ASCII).
_DEMO_PDFS: dict[str, tuple[str, list[str]]] = {
    "ECON101-syllabus.pdf": ("ECON101 Course Syllabus", [
        "ECON101 - Introduction to Economics: course syllabus.",
        "This module introduces the core tools economists use to study choices.",
        "Weekly structure combines one lecture, one workshop, and guided readings.",
        "Assessment consists of weekly problem sets and one final examination.",
        "Week 1 covers scarcity, opportunity cost, and economic reasoning.",
        "Week 2 covers supply, demand, and market equilibrium.",
        "Week 3 covers elasticity and how markets respond to price changes.",
        "Week 4 covers consumer choice and budget constraints.",
    ]),
    "ECON101-week1-lecture-slides.pdf": ("Week 1 - Foundations of Economic Thinking", [
        "Scarcity forces every economic actor to make trade-offs between alternatives.",
        "Opportunity cost is the value of the next best alternative given up by a choice.",
        "Economic models simplify reality to isolate the mechanism being studied.",
        "Incentives shape behaviour, and policy changes work by changing incentives.",
    ]),
    "ECON101-week2-lecture-slides.pdf": ("Week 2 - Supply and Demand", [
        "The demand curve shows the quantity buyers purchase at each possible price.",
        "The supply curve shows the quantity sellers offer at each possible price.",
        "Market equilibrium occurs where quantity demanded equals quantity supplied.",
        "Shifts in demand or supply move the equilibrium price and quantity.",
    ]),
    "ECON101-week3-lecture-slides.pdf": ("Week 3 - Elasticity and Market Responses", [
        "Price elasticity of demand measures how strongly quantity demanded responds to a price change.",
        "Demand is elastic when the percentage change in quantity exceeds the percentage change in price.",
        "Necessities with few substitutes tend to have inelastic demand in the short run.",
        "Total revenue rises after a price cut when demand is elastic, and falls when demand is inelastic.",
        "Income elasticity distinguishes normal goods from inferior goods as incomes change.",
        "Cross-price elasticity is positive for substitutes and negative for complements.",
        "The incidence of a tax falls more heavily on the side of the market that is less elastic.",
        "Elasticity estimates guide price setting, tax policy, and market regulation.",
    ]),
    "ECON101-week3-assignment-brief.pdf": ("Problem Set 3 - Assignment Brief", [
        "Problem Set 3 applies elasticity concepts to three fictional market scenarios.",
        "Question one asks you to compute price elasticity from a demand schedule.",
        "Question two asks how a transit fare change would affect total revenue.",
        "Question three asks who bears the burden of a new tax on a fictional good.",
        "Show your working for each calculation and interpret every result in words.",
        "Submit a single PDF through the course page before the stated deadline.",
    ]),
    "ECON101-week3-reading-pack.pdf": ("Week 3 - Reading Pack", [
        "Reading one reviews the definition and measurement of price elasticity of demand.",
        "Reading two examines empirical elasticity estimates for everyday consumer goods.",
        "Reading three discusses how firms use elasticity evidence when setting prices.",
        "While reading, note which factors make demand for a good more or less elastic.",
        "Bring one question about the readings to this week's workshop discussion.",
    ]),
    "ECON101-week3-workshop-notes.pdf": ("Week 3 - Workshop Notes", [
        "The workshop practises calculating elasticities using the midpoint method.",
        "Work in pairs to classify each example good as elastic or inelastic and justify the choice.",
        "The closing discussion links elasticity to the tax incidence examples from the lecture.",
        "Attempt the first question of Problem Set 3 before attending the workshop.",
    ]),
    "ECON101-week4-lecture-slides.pdf": ("Week 4 - Consumer Choice", [
        "The budget constraint shows the bundles a consumer can afford at given prices and income.",
        "Indifference curves represent combinations of goods giving equal satisfaction.",
        "The consumer's optimal bundle is where an indifference curve touches the budget line.",
        "Changes in prices rotate the budget line and change the chosen bundle.",
    ]),
    "CS210-syllabus.pdf": ("CS210 Course Syllabus", [
        "CS210 - Building With AI: course syllabus.",
        "This module is a practical introduction to building applications with AI models.",
        "Week 1 surveys what modern AI applications look like and how they are structured.",
        "Week 2 covers working with language models, prompting, and evaluation.",
        "Week 3 covers retrieval, tool use, and grounding model outputs in data.",
        "Assessment is by a group project delivered in staged milestones.",
    ]),
    "CS210-week3-lecture-slides.pdf": ("Week 3 - Retrieval and Tool Use", [
        "Retrieval augmented generation supplies a model with relevant documents at question time.",
        "A retrieval pipeline chunks documents, indexes them, and ranks matches for a query.",
        "Tool use lets a model call external functions such as search, calculators, or databases.",
        "Grounding responses in retrieved sources reduces fabricated answers and aids verification.",
        "Evaluation should measure both answer quality and whether cited sources support the answer.",
    ]),
    "CS210-week3-workshop-notes.pdf": ("Week 3 - Workshop Notes", [
        "In this workshop each team indexes a small fictional document set and queries it.",
        "Compare keyword search results against embedding-based retrieval for the same queries.",
        "Discuss when retrieval errors, rather than model errors, cause a wrong final answer.",
        "Record your findings in the project log for Milestone 1.",
    ]),
    "PSY110-week3-lecture-slides.pdf": ("Week 3 - Memory and Learning", [
        "Memory research distinguishes encoding, storage, and retrieval processes.",
        "Working memory holds a small amount of information for immediate use.",
        "Long-term memory divides into declarative and procedural systems.",
        "Retrieval practice strengthens memory more than repeated passive review.",
        "Spacing study sessions over time improves retention compared with massed practice.",
    ]),
    "PSY110-week3-reading-pack.pdf": ("Week 3 - Reading Pack", [
        "Reading one introduces classic experiments on the capacity of working memory.",
        "Reading two reviews evidence on retrieval practice and the testing effect.",
        "Consider how the findings apply to your own study habits this term.",
    ]),
    "STAT120-week3-lecture-slides.pdf": ("Week 3 - Summarising Data", [
        "Measures of centre include the mean, the median, and the mode.",
        "Measures of spread include the range, interquartile range, and standard deviation.",
        "Skewed distributions pull the mean away from the median toward the tail.",
        "Boxplots summarise centre, spread, and outliers in a single display.",
        "Choosing the right summary depends on the shape of the distribution.",
    ]),
    "STAT120-week3-assignment-brief.pdf": ("Data Cleaning Exercise - Brief", [
        "The exercise uses a small fictional dataset of workshop attendance records.",
        "Identify missing values and decide, with justification, how to handle each one.",
        "Produce summary statistics before and after cleaning and compare them.",
        "Submit your cleaned dataset and a short written summary of the changes.",
    ]),
}


@functools.lru_cache(maxsize=None)
def demo_pdf_bytes(filename: str) -> bytes:
    """Return deterministic fake PDF bytes for a demo file name."""
    title, body = _DEMO_PDFS.get(
        filename,
        ("Worsaga Demo Document", ["This is a generic fake demo document."]),
    )
    lines = [*_FAKE_MARKER_LINES, title, ""] + body
    pages = [
        lines[i:i + _LINES_PER_PAGE]
        for i in range(0, len(lines), _LINES_PER_PAGE)
    ]
    return _render_pdf(pages)


# ─────────────────────────────────────────────────────────────────
# Demo dataset (raw Moodle-shaped payloads)
# ─────────────────────────────────────────────────────────────────

# Fields mirror ``core_enrol_get_users_courses``: alongside the useful
# id/shortname/fullname, Moodle returns bulky context an agent rarely needs
# (HTML ``summary`` with inline styles, ``enrolledusercount``, a course
# image, progress). Keeping them here lets demo mode exercise the
# ``course_record`` normalisation boundary that strips them.
DEMO_COURSES = [
    {"id": 101, "shortname": "ECON101", "fullname": "Introduction to Economics",
     "category": 2, "startdate": 1_725_148_800, "enddate": 1_744_675_200,
     "enrolledusercount": 214, "visible": 1, "format": "topics",
     "summaryformat": 1, "progress": 42,
     "summary": ("<div class=\"no-overflow\"><p style=\"font-size:1.05em;"
                 "line-height:1.5\">A first course in <strong>microeconomics"
                 "</strong>: scarcity, incentives, and how markets set prices."
                 "</p></div>")},
    {"id": 102, "shortname": "CS210", "fullname": "Building With AI",
     "category": 3, "startdate": 1_725_148_800, "enddate": 1_744_675_200,
     "enrolledusercount": 96, "visible": 1, "format": "topics",
     "summaryformat": 1, "progress": 30,
     "summary": ("<div class=\"no-overflow\"><p style=\"margin:0 0 8px\">"
                 "Design and evaluate practical applications built on "
                 "<em>language models</em>.</p></div>")},
    {"id": 103, "shortname": "PSY110", "fullname": "Foundations of Psychology",
     "category": 4, "startdate": 1_725_148_800, "enddate": 1_744_675_200,
     "enrolledusercount": 301, "visible": 1, "format": "topics",
     "summaryformat": 1, "progress": 55,
     "summary": ("<div class=\"no-overflow\"><p>Core ideas across cognitive, "
                 "developmental, and social psychology.</p></div>")},
    {"id": 104, "shortname": "STAT120", "fullname": "Data For Decisions",
     "category": 2, "startdate": 1_725_148_800, "enddate": 1_744_675_200,
     "enrolledusercount": 158, "visible": 1, "format": "topics",
     "summaryformat": 1, "progress": 18,
     "summary": ("<div class=\"no-overflow\"><p style=\"color:#333\">Turning "
                 "messy data into defensible decisions with basic statistics."
                 "</p></div>")},
]


def _file_module(module_id: int, name: str, filename: str, *, modified: int) -> dict:
    return {
        "id": module_id,
        "name": name,
        "modname": "resource",
        "url": f"{DEMO_BASE_URL}/mod/resource/view.php?id={module_id}",
        "contents": [{
            "type": "file",
            "filename": filename,
            "filepath": "/",
            "fileurl": (
                f"{DEMO_BASE_URL}/webservice/pluginfile.php/{module_id}"
                f"/mod_resource/content/1/{filename}"
            ),
            "filesize": len(demo_pdf_bytes(filename)),
            "mimetype": "application/pdf",
            "timemodified": modified,
        }],
    }


def _url_module(module_id: int, name: str, target: str, *, added: int) -> dict:
    return {
        "id": module_id,
        "name": name,
        "modname": "url",
        "url": f"{DEMO_BASE_URL}/mod/url/view.php?id={module_id}",
        "added": added,
        "contents": [{
            "type": "url",
            "filename": name,
            "fileurl": target,
            "filesize": 0,
            "timemodified": added,
        }],
    }


def _plain_module(module_id: int, name: str, modname: str) -> dict:
    return {
        "id": module_id,
        "name": name,
        "modname": modname,
        "url": f"{DEMO_BASE_URL}/mod/{modname}/view.php?id={module_id}",
    }


def build_demo_dataset(now: int | None = None) -> dict:
    """Build the full fake dataset with timestamps anchored near *now*.

    Timestamps are truncated to the hour so repeated calls within the
    same hour produce identical data.
    """
    now = int(time.time()) if now is None else int(now)
    base = now - (now % 3600)

    def at(day_offset: int, hour_offset: int = 0) -> int:
        return base + day_offset * 86400 + hour_offset * 3600

    contents = {
        101: [
            {"id": 1100, "section": 0, "name": "Course Information",
             "summary": ("<div class=\"no-overflow\"><p style=\"font-size:"
                         "1.1em\">Welcome to <strong>ECON101</strong>. Start "
                         "with the syllabus, then work through one week at a "
                         "time.</p></div>"),
             "summaryformat": 1, "modules": [
                _file_module(5100, "Course syllabus", "ECON101-syllabus.pdf",
                             modified=at(-30)),
                _plain_module(5199, "Announcements", "forum"),
            ]},
            {"id": 1101, "section": 1,
             "name": "Week 1 - Foundations of Economic Thinking",
             "summary": ("<div class=\"no-overflow\"><p>Opportunity cost, "
                         "incentives, and thinking at the margin.</p></div>"),
             "summaryformat": 1, "modules": [
                _file_module(5101, "Week 1 lecture slides",
                             "ECON101-week1-lecture-slides.pdf", modified=at(-21)),
             ]},
            {"id": 1102, "section": 2, "name": "Week 2 - Supply and Demand",
             "modules": [
                _file_module(5102, "Week 2 lecture slides",
                             "ECON101-week2-lecture-slides.pdf", modified=at(-14)),
             ]},
            {"id": 1103, "section": 3,
             "name": "Week 3 - Elasticity and Market Responses",
             # A realistically verbose, inline-styled section summary of the
             # kind real Moodle courses carry — it strips to a couple of
             # plain-text sentences in get_course_contents.
             "summary": (
                 "<div class=\"no-overflow\">"
                 "<h4 style=\"margin:0 0 6px;font-family:Arial,sans-serif;"
                 "color:#1a1a1a\">Week 3: Elasticity and Market Responses</h4>"
                 "<p style=\"line-height:1.6;color:#222;font-size:1.02em\">"
                 "This week examines <strong>price elasticity</strong> of "
                 "demand and supply and how total <em>revenue</em> responds "
                 "when prices change.</p>"
                 "<ul style=\"margin:8px 0 8px 18px;padding:0;color:#333\">"
                 "<li style=\"margin-bottom:4px\">Define and compute the "
                 "price elasticity of demand.</li>"
                 "<li style=\"margin-bottom:4px\">Distinguish elastic, "
                 "inelastic, and unit-elastic ranges.</li>"
                 "<li style=\"margin-bottom:4px\">Relate elasticity to a "
                 "firm's pricing decisions.</li></ul>"
                 "<p style=\"line-height:1.6;color:#222\">Read the reading "
                 "pack and attempt the problem set <span style=\"font-weight:"
                 "600\">before</span> the workshop.</p></div>"
             ),
             "summaryformat": 1, "modules": [
                _file_module(5103, "Week 3 lecture slides",
                             "ECON101-week3-lecture-slides.pdf", modified=at(-5)),
                _file_module(5104, "Problem Set 3 brief",
                             "ECON101-week3-assignment-brief.pdf", modified=at(-4)),
                _file_module(5105, "Week 3 reading pack",
                             "ECON101-week3-reading-pack.pdf", modified=at(-4)),
                _file_module(5106, "Week 3 workshop notes",
                             "ECON101-week3-workshop-notes.pdf", modified=at(-3)),
                _url_module(5107, "Week 3 further reading (external link)",
                            "https://example.com/econ101/week-3-readings",
                            added=at(-4)),
                _plain_module(5108, "Problem Set 3", "assign"),
             ]},
            {"id": 1104, "section": 4, "name": "Week 4 - Consumer Choice",
             "modules": [
                _file_module(5109, "Week 4 lecture slides",
                             "ECON101-week4-lecture-slides.pdf", modified=at(-1)),
             ]},
            {"id": 1105, "section": 5, "name": "Revision and Exam Preparation",
             "modules": []},
        ],
        102: [
            {"id": 1200, "section": 0, "name": "Course Information", "modules": [
                _file_module(5200, "Course syllabus", "CS210-syllabus.pdf",
                             modified=at(-30)),
                _plain_module(5299, "Announcements", "forum"),
            ]},
            {"id": 1201, "section": 1, "name": "Week 1 - What Is an AI Application",
             "modules": []},
            {"id": 1202, "section": 2, "name": "Week 2 - Working With Language Models",
             "modules": []},
            {"id": 1203, "section": 3, "name": "Week 3 - Retrieval and Tool Use",
             "modules": [
                _file_module(5203, "Week 3 lecture slides",
                             "CS210-week3-lecture-slides.pdf", modified=at(-6)),
                _file_module(5204, "Week 3 workshop notes",
                             "CS210-week3-workshop-notes.pdf", modified=at(-5)),
                _plain_module(5205, "Project Milestone 1", "assign"),
             ]},
        ],
        103: [
            {"id": 1300, "section": 0, "name": "Course Information", "modules": [
                _plain_module(5399, "Announcements", "forum"),
            ]},
            {"id": 1301, "section": 3, "name": "Week 3 - Memory and Learning",
             "modules": [
                _file_module(5301, "Week 3 lecture slides",
                             "PSY110-week3-lecture-slides.pdf", modified=at(-5)),
                _file_module(5302, "Week 3 reading pack",
                             "PSY110-week3-reading-pack.pdf", modified=at(-5)),
                _plain_module(5303, "Week 3 Reading Quiz", "quiz"),
             ]},
        ],
        104: [
            {"id": 1400, "section": 0, "name": "Course Information", "modules": [
                _plain_module(5499, "Announcements", "forum"),
            ]},
            {"id": 1401, "section": 3, "name": "Week 3 - Summarising Data",
             "modules": [
                _file_module(5401, "Week 3 lecture slides",
                             "STAT120-week3-lecture-slides.pdf", modified=at(-4)),
                _file_module(5402, "Data Cleaning Exercise brief",
                             "STAT120-week3-assignment-brief.pdf", modified=at(-3)),
                _plain_module(5403, "Data Cleaning Exercise", "assign"),
             ]},
        ],
    }

    assignments = {"courses": [
        {"id": 101, "shortname": "ECON101", "assignments": [
            {"id": 7101, "cmid": 5108, "course": 101, "name": "Problem Set 3",
             "duedate": at(3, 17), "cutoffdate": at(5, 17),
             "allowsubmissionsfromdate": at(-4, 9)},
            {"id": 7102, "cmid": 5110, "course": 101, "name": "Problem Set 2",
             "duedate": at(-4, 17), "cutoffdate": at(-2, 17),
             "allowsubmissionsfromdate": at(-11, 9)},
        ]},
        {"id": 102, "shortname": "CS210", "assignments": [
            {"id": 7201, "cmid": 5205, "course": 102, "name": "Project Milestone 1",
             "duedate": at(6, 12), "cutoffdate": 0,
             "allowsubmissionsfromdate": at(-7, 9)},
        ]},
        {"id": 103, "shortname": "PSY110", "assignments": []},
        {"id": 104, "shortname": "STAT120", "assignments": [
            {"id": 7401, "cmid": 5403, "course": 104, "name": "Data Cleaning Exercise",
             "duedate": at(10, 17), "cutoffdate": 0,
             "allowsubmissionsfromdate": at(-3, 9)},
        ]},
    ]}

    submission_statuses = {
        7101: {"lastattempt": {"submission": {"status": "draft"}}},
        7102: {
            "lastattempt": {"submission": {"status": "submitted"}},
            "feedback": {"grade": {"grade": "72.00"}},
        },
        7201: {"lastattempt": {"submission": {"status": "new"}}},
        7401: {"lastattempt": {"submission": {"status": "new"}}},
    }

    quizzes = {"quizzes": [
        {"id": 8101, "course": 101, "name": "Elasticity Concept Check",
         "timeopen": at(-2, 9), "timeclose": at(8, 23)},
        {"id": 8301, "course": 103, "name": "Week 3 Reading Quiz",
         "timeopen": at(-2, 9), "timeclose": at(5, 10)},
    ]}

    def grade_item(item_id, name, grade, percent, **extra):
        item = {"id": item_id, "itemname": name, "itemmodule": "assign",
                "grademin": 0, "grademax": 100}
        if grade is not None:
            item["gradeformatted"] = grade
        if percent is not None:
            item["percentageformatted"] = percent
        item.update(extra)
        return item

    grades = {
        101: {"usergrades": [{"courseid": 101, "gradeitems": [
            grade_item(9101, "Problem Set 1", "68.00", "68.00 %",
                       gradedategraded=at(-10, 14)),
            grade_item(9102, "Problem Set 2", "72.00", "72.00 %",
                       gradedategraded=at(-5, 16)),
            grade_item(9103, "Problem Set 3", "-", None),
            grade_item(9104, "Course total", "70.00", "70.00 %",
                       itemtype="course", itemmodule=None,
                       gradedategraded=at(-5, 16)),
        ]}]},
        102: {"usergrades": [{"courseid": 102, "gradeitems": [
            grade_item(9201, "Project Milestone 1", "-", None),
            grade_item(9202, "Course total", "-", None,
                       itemtype="course", itemmodule=None),
        ]}]},
        103: {"usergrades": [{"courseid": 103, "gradeitems": [
            grade_item(9301, "Week 1 Quiz", "Hidden", None, hidden=1),
            grade_item(9302, "Course total", "-", None,
                       itemtype="course", itemmodule=None),
        ]}]},
        104: {"usergrades": [{"courseid": 104, "gradeitems": [
            grade_item(9401, "Weekly Exercises", "81.00", "81.00 %",
                       gradedategraded=at(-7, 11)),
            grade_item(9402, "Course total", "81.00", "81.00 %",
                       itemtype="course", itemmodule=None,
                       gradedategraded=at(-7, 11)),
        ]}]},
    }

    forums = [
        {"id": 6101, "course": 101, "name": "Announcements", "type": "news",
         "intro": "Course announcements from the teaching team.",
         "numdiscussions": 2},
        {"id": 6102, "course": 101, "name": "General discussion", "type": "general",
         "intro": "Open discussion for ECON101 students.", "numdiscussions": 1},
        {"id": 6201, "course": 102, "name": "Announcements", "type": "news",
         "intro": "Course announcements.", "numdiscussions": 1},
        {"id": 6301, "course": 103, "name": "Announcements", "type": "news",
         "intro": "Course announcements.", "numdiscussions": 1},
        {"id": 6401, "course": 104, "name": "Announcements", "type": "news",
         "intro": "Course announcements.", "numdiscussions": 1},
    ]

    def discussion(did, name, author, when, unread=0):
        return {"discussion": did, "name": name, "userfullname": author,
                "created": when, "timemodified": when, "numunread": unread}

    discussions = {
        6101: [
            discussion(301, "Week 3 workshop moves to room B12",
                       "Dr Avery Demo", at(-1, 15), unread=1),
            discussion(302, "Problem Set 3 released",
                       "Dr Avery Demo", at(-4, 9)),
        ],
        6102: [
            discussion(303, "Study group for Problem Set 3?",
                       "Demo Student", at(-1, 19)),
        ],
        6201: [
            discussion(304, "Guest talk on evaluating AI systems next week",
                       "Prof Riley Sample", at(-2, 11)),
        ],
        6301: [
            discussion(305, "Week 3 Reading Quiz opens Monday",
                       "Dr Casey Fixture", at(-3, 10)),
        ],
        6401: [
            discussion(306, "Dataset for the cleaning exercise is posted",
                       "Dr Morgan Mock", at(-2, 16)),
        ],
    }

    notifications = {"notifications": [
        {"id": 401, "subject": "Assignment due soon: Problem Set 3",
         "fullmessage": "Problem Set 3 for ECON101 is due in 3 days.",
         "userfromfullname": "Worsaga Demo University", "courseid": 101,
         "timecreated": at(-1, 8), "read": False,
         "contexturl": f"{DEMO_BASE_URL}/mod/assign/view.php?id=5108"},
        {"id": 402, "subject": "New material: Week 3 reading pack",
         "fullmessage": "A new reading pack was added to ECON101 Week 3.",
         "userfromfullname": "Worsaga Demo University", "courseid": 101,
         "timecreated": at(-4, 14), "read": True,
         "contexturl": f"{DEMO_BASE_URL}/mod/resource/view.php?id=5105"},
        {"id": 403, "subject": "Week 3 Reading Quiz is open",
         "fullmessage": "The PSY110 Week 3 Reading Quiz is now open.",
         "userfromfullname": "Worsaga Demo University", "courseid": 103,
         "timecreated": at(-2, 9), "read": True,
         "contexturl": f"{DEMO_BASE_URL}/mod/quiz/view.php?id=5303"},
    ]}

    messages = {"messages": [
        {"id": 501, "subject": "Study group Thursday",
         "fullmessage": "A few of us are meeting Thursday to work through "
                        "Problem Set 3 together. Want to join? (fake message)",
         "userfromfullname": "Demo Classmate", "timecreated": at(-1, 18),
         "read": False},
        {"id": 502, "subject": "Re: workshop question",
         "fullmessage": "Good question - bring it to the Week 3 workshop and "
                        "we will go through it. (fake message)",
         "userfromfullname": "Dr Avery Demo", "timecreated": at(-3, 12),
         "read": True},
    ]}

    events = {"events": [
        {"id": 601, "courseid": 101, "name": "Week 3 workshop",
         "description": "Elasticity workshop in room B12.",
         "eventtype": "course", "timestart": at(2, 14), "timeduration": 3600,
         "url": f"{DEMO_BASE_URL}/calendar/view.php?view=day"},
        {"id": 602, "courseid": 101, "name": "Problem Set 3 due",
         "description": "Submit through the ECON101 course page.",
         "eventtype": "due", "timestart": at(3, 17), "timeduration": 0,
         "url": f"{DEMO_BASE_URL}/mod/assign/view.php?id=5108"},
        {"id": 603, "courseid": 103, "name": "Week 3 Reading Quiz closes",
         "description": "Last chance to complete the reading quiz.",
         "eventtype": "close", "timestart": at(5, 10), "timeduration": 0,
         "url": f"{DEMO_BASE_URL}/mod/quiz/view.php?id=5303"},
        {"id": 604, "courseid": 102, "name": "Project Milestone 1 due",
         "description": "Submit the milestone report and project log.",
         "eventtype": "due", "timestart": at(6, 12), "timeduration": 0,
         "url": f"{DEMO_BASE_URL}/mod/assign/view.php?id=5205"},
        {"id": 605, "courseid": 101, "name": "Elasticity Concept Check closes",
         "description": "Online concept check for weeks 2 and 3.",
         "eventtype": "close", "timestart": at(8, 23), "timeduration": 0,
         "url": f"{DEMO_BASE_URL}/mod/quiz/view.php?id=8101"},
        {"id": 606, "courseid": 104, "name": "Data Cleaning Exercise due",
         "description": "Submit the cleaned dataset and summary.",
         "eventtype": "due", "timestart": at(10, 17), "timeduration": 0,
         "url": f"{DEMO_BASE_URL}/mod/assign/view.php?id=5403"},
    ]}

    return {
        "courses": DEMO_COURSES,
        "contents": contents,
        "assignments": assignments,
        "submission_statuses": submission_statuses,
        "quizzes": quizzes,
        "grades": grades,
        "forums": forums,
        "discussions": discussions,
        "notifications": notifications,
        "messages": messages,
        "events": events,
    }


# ─────────────────────────────────────────────────────────────────
# Demo client
# ─────────────────────────────────────────────────────────────────


class DemoMoodleClient:
    """Offline stand-in for MoodleClient backed by the fake dataset.

    Mirrors the read-only method subset the CLI commands and MCP tools
    use. Never touches the network; ``call`` always raises.
    """

    is_demo = True

    def __init__(self, now: int | None = None):
        self._data = build_demo_dataset(now=now)

    @property
    def base_url(self) -> str:
        return DEMO_BASE_URL

    @property
    def userid(self) -> int:
        return DEMO_USERID

    def call(self, wsfunction: str, **params):
        raise RuntimeError(
            "Demo mode is fully offline - no Moodle web-service call "
            f"('{wsfunction}') is ever made."
        )

    def site_info(self) -> dict:
        """Demo stand-in for core_webservice_get_site_info (no network)."""
        return {
            "sitename": "Worsaga Demo University (fake data)",
            "username": "demo.student",
            "fullname": "Demo Student",
            "userid": DEMO_USERID,
            "release": "demo",
        }

    # ── Read methods mirroring MoodleClient ────────────────────────

    def get_courses(self) -> list[dict]:
        return copy.deepcopy(self._data["courses"])

    def _require_known_course(self, course_id: int) -> None:
        """Mirror Moodle: an unknown course id is a not-found failure.

        Real Moodle raises "Can't find data record in database table
        course." for a course the user is not enrolled in; the demo client
        raises the same :class:`CourseNotFoundError` so error-path
        behaviour (CLI friendly message, MCP structured error dict) is
        exercised offline. A *known* course with no contents/grades stays a
        valid empty state.
        """
        known = {int(c["id"]) for c in self._data["courses"]}
        if int(course_id) not in known:
            raise CourseNotFoundError(int(course_id))

    def get_course_contents(self, course_id: int) -> list[dict]:
        self._require_known_course(course_id)
        return copy.deepcopy(self._data["contents"].get(int(course_id), []))

    def get_assignments(self, course_id: int) -> dict:
        return self.get_assignments_by_courses([course_id])

    def get_assignments_by_courses(self, course_ids: list[int]) -> dict:
        wanted = {int(cid) for cid in course_ids}
        return {"courses": [
            copy.deepcopy(c)
            for c in self._data["assignments"]["courses"]
            if c["id"] in wanted
        ]}

    def get_assignment_submission_status(self, assignment_id: int) -> dict:
        return copy.deepcopy(
            self._data["submission_statuses"].get(int(assignment_id), {})
        )

    def get_user_grade_items(self, course_id: int, user_id: int | None = None) -> dict:
        self._require_known_course(course_id)
        return copy.deepcopy(
            self._data["grades"].get(int(course_id), {"usergrades": []})
        )

    def get_quizzes(self, course_ids: list[int] | None = None) -> dict:
        if course_ids is None:
            course_ids = [c["id"] for c in self._data["courses"]]
        wanted = {int(cid) for cid in course_ids}
        return {"quizzes": [
            copy.deepcopy(q)
            for q in self._data["quizzes"]["quizzes"]
            if q["course"] in wanted
        ]}

    def get_forums_by_courses(self, course_ids: list[int]) -> dict:
        wanted = {int(cid) for cid in course_ids}
        return {"forums": [
            copy.deepcopy(f)
            for f in self._data["forums"]
            if f["course"] in wanted
        ]}

    def get_forum_discussions(self, forum_id: int) -> dict:
        return {"discussions": copy.deepcopy(
            self._data["discussions"].get(int(forum_id), [])
        )}

    def get_popup_notifications(self, unread_only: bool = False) -> dict:
        return copy.deepcopy(self._data["notifications"])

    def get_messages(self, since_time: int | None = None) -> dict:
        return copy.deepcopy(self._data["messages"])

    def get_calendar_events(
        self,
        course_ids: list[int] | None = None,
        timestart: int | None = None,
        timeend: int | None = None,
    ) -> dict:
        events = self._data["events"]["events"]
        if course_ids is not None:
            wanted = {int(cid) for cid in course_ids}
            events = [e for e in events if e["courseid"] in wanted]
        if timestart is not None:
            events = [e for e in events if e["timestart"] >= timestart]
        if timeend is not None:
            events = [e for e in events if e["timestart"] <= timeend]
        return {"events": copy.deepcopy(events)}

    def download_file(
        self, fileurl: str, *, max_bytes: int | None = None,
    ) -> bytes | None:
        """Return locally generated fake bytes for demo pluginfile URLs."""
        parsed = urllib.parse.urlparse(str(fileurl or ""))
        demo_host = urllib.parse.urlparse(DEMO_BASE_URL).netloc
        if parsed.netloc.lower() != demo_host:
            return None
        filename = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
        if filename not in _DEMO_PDFS:
            return None
        data = demo_pdf_bytes(filename)
        return data if max_bytes is None else data[:max_bytes]
