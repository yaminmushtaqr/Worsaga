"""Tests for study summary generation: section scoring, fallback logic,
deterministic bullet building, and the high-level summary builder."""

import pytest

from worsaga.sections import (
    classify_section,
    find_best_section,
    get_downloadable_files,
    score_section_match,
    summarize_modules,
)
from worsaga.summaries import (
    build_deterministic_summary,
    build_summary,
    build_weekly_summary,
    fallback_bullets,
    format_bullets,
)
from worsaga.summary_text import (
    _condense_line,
    _content_words,
    _deduplicate_lines,
    _is_continuation,
    _merge_fragments,
    _polish_bullet,
    _reject_final_bullet,
    _score_line,
    _select_diverse,
)


# ── Helpers ──────────────────────────────────────────────────────


def _make_file_content(filename, size=1024):
    return {
        "type": "file",
        "filename": filename,
        "fileurl": f"https://moodle.example.com/pluginfile.php/0/{filename}",
        "filesize": size,
    }


def _make_section(name, section_num, modules=None):
    return {
        "id": 100 + section_num,
        "name": name,
        "section": section_num,
        "modules": modules or [],
    }


def _make_module(mod_id, name, modname="resource", contents=None):
    return {
        "id": mod_id,
        "name": name,
        "modname": modname,
        "url": f"https://moodle.example.com/mod/{modname}/view.php?id={mod_id}",
        "contents": contents or [],
    }


# ── classify_section ────────────────────────────────────────────


class TestClassifySection:
    @pytest.mark.parametrize(
        "name, expected",
        [
            ("Reading Week", "reading"),
            ("Independent Study Period", "reading"),
            ("Self-Study Week", "reading"),
            ("Consolidation Week", "reading"),
            ("Final Exam Period", "exam"),
            ("Assessment Period", "exam"),
            ("Examination Week", "exam"),
            ("Revision Week", "revision"),
            ("Review Week", "revision"),
            ("Recap Session", "revision"),
            ("Week 3: Markets", "normal"),
            ("General Information", "normal"),
        ],
    )
    def test_classification(self, name, expected):
        assert classify_section(name) == expected


# ── score_section_match ─────────────────────────────────────────


class TestScoreSectionMatch:
    def test_week_number_match(self):
        score, stype = score_section_match("Week 3: Markets", 3)
        assert score == 100
        assert stype == "normal"

    def test_lecture_number_match(self):
        score, stype = score_section_match("Lecture 5: Demand", 5)
        assert score == 95
        assert stype == "normal"

    def test_seminar_match(self):
        score, stype = score_section_match("Seminar 2", 2)
        assert score == 90
        assert stype == "normal"

    def test_session_match(self):
        score, stype = score_section_match("Session 4", 4)
        assert score == 85
        assert stype == "normal"

    def test_topic_match(self):
        score, stype = score_section_match("Topic 7: Finance", 7)
        assert score == 75
        assert stype == "normal"

    def test_leading_number_match(self):
        score, stype = score_section_match("7. Finance Basics", 7)
        assert score == 70
        assert stype == "normal"

    def test_no_match(self):
        score, stype = score_section_match("General Information", 3)
        assert score == 0
        assert stype == "general"

    def test_wrong_week_number(self):
        score, _ = score_section_match("Week 5: Trade", 3)
        assert score == 0

    def test_reading_week_with_number(self):
        score, stype = score_section_match("Reading Week 6", 6)
        assert score == 50
        assert stype == "reading"

    def test_reading_week_without_matching_number(self):
        score, stype = score_section_match("Reading Week", 3)
        assert score == 0
        assert stype == "reading"

    def test_exam_week(self):
        score, stype = score_section_match("Final Exam Period", 10)
        assert score == 0
        assert stype == "exam"

    def test_revision_with_number(self):
        score, stype = score_section_match("Revision Week 11", 11)
        assert score == 50
        assert stype == "revision"


# ── get_downloadable_files ──────────────────────────────────────


class TestGetDownloadableFiles:
    def test_extracts_supported_files(self):
        modules = [
            _make_module(1, "Slides", contents=[
                _make_file_content("slides.pdf"),
                _make_file_content("slides.pptx"),
            ]),
            _make_module(2, "Notes", contents=[
                _make_file_content("notes.docx"),
            ]),
        ]
        files = get_downloadable_files(modules)
        assert len(files) == 3
        # PDF should come first (priority 0)
        assert files[0]["filename"] == "slides.pdf"

    def test_skips_unsupported_extensions(self):
        modules = [
            _make_module(1, "Image", contents=[
                _make_file_content("photo.jpg"),
            ]),
        ]
        assert get_downloadable_files(modules) == []

    def test_skips_quiz_and_forum_modules(self):
        modules = [
            _make_module(1, "Quiz", modname="quiz", contents=[
                _make_file_content("quiz.pdf"),
            ]),
            _make_module(2, "Forum", modname="forum", contents=[
                _make_file_content("post.pdf"),
            ]),
        ]
        assert get_downloadable_files(modules) == []

    def test_deduplicates_by_url_and_filename(self):
        content = _make_file_content("slides.pdf")
        modules = [
            _make_module(1, "Slides A", contents=[content]),
            _make_module(2, "Slides B", contents=[content]),
        ]
        files = get_downloadable_files(modules)
        assert len(files) == 1

    def test_respects_max_files(self):
        contents = [_make_file_content(f"file{i}.pdf") for i in range(10)]
        modules = [_make_module(1, "Many files", contents=contents)]
        files = get_downloadable_files(modules, max_files=3)
        assert len(files) == 3

    def test_skips_non_file_content_types(self):
        modules = [
            _make_module(1, "Page", contents=[
                {"type": "url", "filename": "link.pdf", "fileurl": "https://example.com"},
            ]),
        ]
        assert get_downloadable_files(modules) == []

    def test_priority_ordering(self):
        modules = [
            _make_module(1, "Mix", contents=[
                _make_file_content("doc.docx"),
                _make_file_content("slides.pptx"),
                _make_file_content("notes.pdf"),
                _make_file_content("readme.txt"),
            ]),
        ]
        files = get_downloadable_files(modules)
        exts = [f["filename"].rsplit(".", 1)[1] for f in files]
        assert exts == ["pdf", "pptx", "docx", "txt"]


# ── find_best_section ───────────────────────────────────────────


class TestFindBestSection:
    def _sections_with_files(self):
        return [
            _make_section("General", 0, [
                _make_module(1, "Handbook", contents=[_make_file_content("handbook.pdf")]),
            ]),
            _make_section("Week 1: Intro", 1, [
                _make_module(10, "Slides", contents=[_make_file_content("w1.pdf")]),
            ]),
            _make_section("Week 2: Demand", 2, [
                _make_module(20, "Slides", contents=[_make_file_content("w2.pdf")]),
            ]),
            _make_section("Reading Week", 3, []),
            _make_section("Week 4: Supply", 4, [
                _make_module(40, "Slides", contents=[_make_file_content("w4.pdf")]),
            ]),
        ]

    def test_direct_week_match(self):
        section, stype, name = find_best_section(self._sections_with_files(), 1)
        assert name == "Week 1: Intro"
        assert stype == "normal"

    def test_reading_week_detected(self):
        section, stype, name = find_best_section(self._sections_with_files(), 3)
        assert stype == "reading"
        assert "Reading" in name

    def test_adjacent_week_fallback(self):
        # Week 8 doesn't exist and there's no special week nearby
        sections = [
            _make_section("Week 7: Trade", 7, [
                _make_module(70, "Slides", contents=[_make_file_content("w7.pdf")]),
            ]),
            _make_section("Week 9: Growth", 9, [
                _make_module(90, "Slides", contents=[_make_file_content("w9.pdf")]),
            ]),
        ]
        section, stype, name = find_best_section(sections, 8)
        assert stype == "fallback"
        assert section is not None

    def test_no_sections_returns_none(self):
        section, stype, name = find_best_section([], 1)
        assert section is None
        assert stype == "general"
        assert name == ""

    def test_prefers_sections_with_files(self):
        sections = [
            _make_section("Week 1: Intro", 1, []),  # no files
            _make_section("Week 1: Intro Materials", 2, [
                _make_module(10, "Slides", contents=[_make_file_content("w1.pdf")]),
            ]),
        ]
        section, stype, name = find_best_section(sections, 1)
        # The one with files should win (both score 100 for "Week 1")
        assert section is not None
        assert stype == "normal"

    def test_revision_week(self):
        sections = [
            _make_section("Revision Week", 10, []),
        ]
        section, stype, _ = find_best_section(sections, 10)
        assert stype == "revision"

    def test_exam_week(self):
        sections = [
            _make_section("Final Exam Period", 11, []),
        ]
        section, stype, _ = find_best_section(sections, 11)
        assert stype == "exam"


# ── summarize_modules ───────────────────────────────────────────


class TestSummarizeModules:
    def test_groups_slides_and_readings(self):
        modules = [
            _make_module(1, "Lecture Slides", modname="resource"),
            _make_module(2, "Case Study: XYZ", modname="resource"),
            _make_module(3, "Weekly Quiz", modname="quiz"),
        ]
        result = summarize_modules(modules)
        assert "Slides:" in result
        assert "Readings:" in result
        assert "Exercises:" in result

    def test_empty_modules(self):
        assert summarize_modules([]) == ""

    def test_skips_labels(self):
        modules = [_make_module(1, "Divider", modname="label")]
        assert summarize_modules(modules) == ""

    def test_truncates_long_names(self):
        long_name = "A" * 100
        modules = [_make_module(1, f"Lecture {long_name}", modname="resource")]
        result = summarize_modules(modules)
        # Should be truncated to 50 chars
        assert len(result) < 100


# ── fallback_bullets ────────────────────────────────────────────


class TestFallbackBullets:
    def test_reading_week(self):
        bullets = fallback_bullets("reading")
        assert len(bullets) == 4
        assert any("consolidate" in b.lower() for b in bullets)

    def test_exam_period(self):
        bullets = fallback_bullets("exam")
        assert len(bullets) == 4
        assert any("exam" in b.lower() for b in bullets)

    def test_revision_week(self):
        bullets = fallback_bullets("revision")
        assert len(bullets) == 4
        assert any("revisit" in b.lower() for b in bullets)

    def test_generic_fallback(self):
        bullets = fallback_bullets("normal")
        assert len(bullets) == 1
        assert "not yet available" in bullets[0].lower()

    def test_unknown_type_uses_generic(self):
        bullets = fallback_bullets("something_else")
        assert len(bullets) == 1


# ── _merge_fragments ───────────────────────────────────────────


class TestMergeFragments:
    def test_joins_continuation_lines(self):
        text = "Supply and demand\nand their interaction\ndetermine prices"
        result = _merge_fragments(text)
        assert len(result) == 1
        assert "Supply and demand and their interaction determine prices" in result[0]

    def test_splits_on_blank_lines(self):
        text = "First concept is important\n\nSecond concept is different"
        result = _merge_fragments(text)
        assert len(result) == 2

    def test_drops_short_fragments(self):
        text = "OK\nShort\n\nThis is a meaningful concept about economics"
        result = _merge_fragments(text)
        assert len(result) == 1
        assert "meaningful concept" in result[0]

    def test_lowercase_start_merges(self):
        text = "Markets tend toward equilibrium\nbecause of price signals"
        result = _merge_fragments(text)
        assert len(result) == 1
        assert "because" in result[0]

    def test_new_heading_does_not_merge(self):
        text = "First topic about supply\nDemand analysis is different"
        result = _merge_fragments(text)
        assert len(result) == 2


# ── _deduplicate_lines ─────────────────────────────────────────


class TestDeduplicateLines:
    def test_removes_exact_duplicates(self):
        lines = ["Price theory matters", "Something else here", "Price theory matters"]
        result = _deduplicate_lines(lines)
        assert result.count("Price theory matters") == 1

    def test_removes_near_duplicates(self):
        lines = [
            "The equilibrium price is determined by supply and demand",
            "The equilibrium price is determined by supply and demand curves",
        ]
        result = _deduplicate_lines(lines)
        assert len(result) == 1

    def test_keeps_distinct_lines(self):
        lines = [
            "Supply curves slope upward due to increasing costs",
            "Demand curves slope downward due to diminishing returns",
        ]
        result = _deduplicate_lines(lines)
        assert len(result) == 2

    def test_short_lines_only_exact_dedup(self):
        lines = ["Trade", "Trade", "Growth"]
        result = _deduplicate_lines(lines)
        assert result.count("Trade") == 1
        assert "Growth" in result


# ── _score_line ────────────────────────────────────────────────


class TestScoreLine:
    def test_propositional_scores_higher(self):
        prop = "Markets tend toward equilibrium because of price adjustment"
        label = "EQUILIBRIUM"
        assert _score_line(prop) > _score_line(label)

    def test_definition_scores_well(self):
        definition = "Comparative advantage: countries export goods with lower opportunity cost"
        heading = "Comparative Advantage"
        assert _score_line(definition) > _score_line(heading)

    def test_heading_with_colon_penalised(self):
        heading = "Key Concepts:"
        content = "Price elasticity measures responsiveness of demand to price"
        assert _score_line(content) > _score_line(heading)

    def test_verb_presence_boosts_score(self):
        with_verb = "Firms maximize profit where marginal cost equals revenue"
        without_verb = "Profit maximization marginal cost revenue"
        assert _score_line(with_verb) > _score_line(without_verb)

    def test_quote_line_penalised(self):
        quote = '\u201cOrganisations are complex and surprising systems\u201d'
        content = "Organisations are complex and surprising systems"
        assert _score_line(content) > _score_line(quote)

    def test_course_objective_penalised(self):
        objective = "Identify a range of strategies to increase your effectiveness"
        content = "Strategic thinking involves evaluating trade-offs between options"
        assert _score_line(content) > _score_line(objective)

    def test_blog_admin_penalised(self):
        admin = "Your blog is an opportunity to share your viewpoint on a topic"
        content = "Portfolio theory explains how investors minimize risk through diversification"
        assert _score_line(content) > _score_line(admin)

    def test_short_agenda_no_verb_penalised(self):
        agenda = "Interaction with thought leaders"
        content = "Leaders influence team outcomes through vision and motivation"
        assert _score_line(content) > _score_line(agenda)

    def test_meta_instructional_penalised_without_question(self):
        meta = "Consider the various approaches to leadership"
        content = "Transformational leaders inspire followers through idealized influence"
        assert _score_line(content) > _score_line(meta)

    def test_ellipsis_quote_penalised(self):
        quote = "\u201c\u2026Markets are efficient because prices reflect all information\u201d"
        content = "Markets are efficient because prices reflect all available information"
        assert _score_line(content) > _score_line(quote)

    def test_causal_structure_boosted(self):
        causal = "Markets fail because of information asymmetries between buyers"
        plain = "Information asymmetries exist in many markets around the world"
        assert _score_line(causal) > _score_line(plain)

    def test_definition_boosted(self):
        definition = "Moral hazard refers to the tendency to take risks when insured"
        statement = "Moral hazard is a common problem in insurance markets today"
        assert _score_line(definition) > _score_line(statement)

    def test_comma_list_no_verb_penalised(self):
        term_list = "Motivation, Performance, Leadership, Culture"
        content = "Motivation determines employee performance in organisations"
        assert _score_line(content) > _score_line(term_list)

    def test_substantive_colon_definition_boosted(self):
        rich = "Comparative advantage: countries export goods that use their abundant factor intensively"
        thin = "Comparative advantage: relative costs"
        assert _score_line(rich) > _score_line(thin)

    def test_bloom_imperative_penalised(self):
        objective = "Understand the principles of strengths-based leadership"
        content = "Strengths-based leadership focuses on developing individual capabilities"
        assert _score_line(content) > _score_line(objective)

    def test_truncated_line_penalised(self):
        truncated = "How might I show up and manage differently compared to when"
        complete = "Leaders must adapt their approach based on situational demands"
        assert _score_line(complete) > _score_line(truncated)

    def test_reflective_question_penalised(self):
        question = "How might leaders respond differently in a crisis situation?"
        content = "Crisis leadership requires rapid decision-making under uncertainty"
        assert _score_line(content) > _score_line(question)

    def test_inline_bullet_penalised(self):
        with_bullet = "Non-energisers are lower bars which: \u2022 Don't indicate ability"
        without = "Non-energisers are lower bars which indicate energy levels"
        assert _score_line(without) > _score_line(with_bullet)

    def test_promotional_slogan_penalised(self):
        slogan = "YOUR STRENGTHS ARE UNIQUE 1 in 346,000"
        content = "Strengths-based leadership focuses on developing individual capabilities"
        assert _score_line(content) > _score_line(slogan)

    def test_promotional_journey_penalised(self):
        promo = "Strengths-based leadership development is a continuous journey"
        content = "Transformational leadership creates organizational change through vision"
        assert _score_line(content) > _score_line(promo)

    def test_no_right_or_wrong_penalised(self):
        promo = "There are 24 strengths and no right or wrong answers"
        content = "Leaders influence team outcomes through vision and motivation"
        assert _score_line(content) > _score_line(promo)

    def test_platform_consent_penalised(self):
        consent = "Content you share will be processed in accordance with Lovable terms"
        content = "Portfolio theory explains how investors minimize risk through diversification"
        assert _score_line(content) > _score_line(consent)

    def test_survey_logistics_penalised(self):
        survey = "A survey will be used to collect that consent."
        content = "Agency theory examines conflicts between principals and agents"
        assert _score_line(content) > _score_line(survey)

    def test_long_comma_list_penalised(self):
        junk = "CAD, development, gamification, virtualization, accounting, collaboration, customer relationship management, Management"
        content = "Firms maximize profit where marginal cost equals marginal revenue"
        assert _score_line(content) > _score_line(junk)

    def test_parenthetical_fragment_penalised(self):
        junk = "(ignoring fixed assets, which are very small)"
        content = "Firms invest when expected returns exceed their cost of capital"
        assert _score_line(content) > _score_line(junk)

    def test_example_label_penalised(self):
        junk = "Example: exit in 2014 at 6x multiple implies Enterprise Value of $210 million"
        content = "Enterprise value equals equity value plus debt minus cash"
        assert _score_line(content) > _score_line(junk)

    def test_pronoun_led_fragment_penalised(self):
        junk = "They have the potential to impact your performance and management effectiveness"
        content = "Strengths influence performance when they are aligned with role demands"
        assert _score_line(content) > _score_line(junk)

    def test_first_person_quote_fragment_penalised(self):
        junk = "We’d have to be careful ---we don’t want to hire a bunch of unproductive people."
        content = "High-growth firms must balance hiring speed with productivity"
        assert _score_line(content) > _score_line(junk)


# ── _reject_final_bullet (expanded) ───────────────────────────


class TestRejectFinalBullet:
    def test_rejects_trailing_colon(self):
        assert _reject_final_bullet("Rested on an agricultural foundation:") is True

    def test_rejects_narrative_opener(self):
        assert _reject_final_bullet(
            "It did so first in America. There were three prompts for change:"
        ) is True

    def test_rejects_parenthetical_fragment(self):
        assert _reject_final_bullet("(ignoring fixed assets, which are very small)") is True

    def test_rejects_example_label(self):
        assert _reject_final_bullet(
            "Example: exit in 2014 at 6x multiple implies Enterprise Value of $210 million"
        ) is True

    def test_rejects_pronoun_led_fragment(self):
        assert _reject_final_bullet(
            "They have the potential to impact your performance and management effectiveness"
        ) is True

    def test_rejects_first_person_quote_fragment(self):
        assert _reject_final_bullet(
            "We’d have to be careful ---we don’t want to hire a bunch of unproductive people. But I think we could pull this off.”"
        ) is True

    def test_rejects_century_label_fragment(self):
        assert _reject_final_bullet(
            "16th and 17th C: emergence of some of the most remarkable business organisations the world has ever seen."
        ) is True


# ── _condense_line ────────────────────────────────────────────


class TestCondenseLine:
    def test_strips_it_is_important(self):
        result = _condense_line(
            "It is important to note that markets tend toward equilibrium"
        )
        assert result == "Markets tend toward equilibrium"

    def test_strips_in_other_words(self):
        result = _condense_line("In other words, firms maximize profit at MC=MR")
        assert result == "Firms maximize profit at MC=MR"

    def test_strips_according_to(self):
        result = _condense_line(
            "According to Porter, competitive advantage arises from value chains"
        )
        assert result == "Competitive advantage arises from value chains"

    def test_strips_research_suggests(self):
        result = _condense_line(
            "Research suggests that diversification reduces portfolio risk"
        )
        assert result == "Diversification reduces portfolio risk"

    def test_strips_inline_citation(self):
        result = _condense_line(
            "Firms benefit from specialization (Smith, 2020)"
        )
        assert result == "Firms benefit from specialization"

    def test_strips_numbered_reference(self):
        result = _condense_line("Transaction costs matter in governance [3]")
        assert result == "Transaction costs matter in governance"

    def test_preserves_content_without_filler(self):
        line = "Firms maximize profit where marginal cost equals revenue"
        assert _condense_line(line) == line

    def test_capitalizes_after_stripping(self):
        result = _condense_line("Essentially, competitive markets are efficient")
        assert result.startswith("Competitive")


# ── _select_diverse ───────────────────────────────────────────


class TestSelectDiverse:
    def test_selects_max_items(self):
        scored = [(f"Line {i} about topic {i}", 100.0 - i) for i in range(10)]
        result = _select_diverse(scored, 3)
        assert len(result) == 3

    def test_returns_all_if_fewer_than_max(self):
        scored = [("Line A", 50.0), ("Line B", 40.0)]
        result = _select_diverse(scored, 5)
        assert len(result) == 2

    def test_prefers_diversity_over_pure_score(self):
        scored = [
            ("Markets tend toward equilibrium through price adjustment", 90.0),
            ("Markets reach equilibrium via price signals and feedback", 85.0),
            ("Leadership styles affect team performance and outcomes", 80.0),
        ]
        result = _select_diverse(scored, 2)
        # Should pick the top-scored + the diverse third line, not the similar second
        selected_texts = [line for line, _ in result]
        assert any("Leadership" in t for t in selected_texts)

    def test_empty_input(self):
        assert _select_diverse([], 3) == []


class TestContentWords:
    def test_removes_stop_words(self):
        words = _content_words("The theory of supply and demand is fundamental")
        assert "the" not in words
        assert "of" not in words
        assert "theory" in words
        assert "supply" in words
        assert "demand" in words
        assert "fundamental" in words

    def test_removes_punctuation(self):
        words = _content_words("markets, firms, and equilibrium.")
        assert "markets" in words
        assert "equilibrium" in words


# ── _polish_bullet ─────────────────────────────────────────────


class TestPolishBullet:
    def test_strips_bullet_markers(self):
        assert _polish_bullet("- Some point here") == "Some point here"
        assert _polish_bullet("• Another point") == "Another point"

    def test_strips_numbering(self):
        assert _polish_bullet("1. First point here") == "First point here"
        assert _polish_bullet("3) Third point here") == "Third point here"

    def test_capitalizes_first_letter(self):
        assert _polish_bullet("lower case start") == "Lower case start"

    def test_removes_trailing_dash(self):
        result = _polish_bullet("Incomplete thought —")
        assert not result.endswith("—")

    def test_strips_surrounding_quotes(self):
        result = _polish_bullet('\u201cMarkets are efficient.\u201d')
        assert "Markets are efficient" in result
        assert '\u201c' not in result
        assert '\u201d' not in result

    def test_strips_leading_ellipsis_quote(self):
        result = _polish_bullet('\u201c\u2026Organizations are complex systems')
        assert result.startswith('Organizations')
        assert '\u201c' not in result
        assert '\u2026' not in result

    def test_strips_for_example(self):
        result = _polish_bullet('For example, trait theory explains individual differences')
        assert result.startswith('Trait')
        assert 'for example' not in result.lower()

    def test_trims_unclosed_parenthetical(self):
        result = _polish_bullet(
            'Teams can be allocated based on preferences (either AT or WT'
        )
        assert '(' not in result
        assert result.endswith('preferences')

    def test_trims_dangling_clause_at_boundary(self):
        """Long bullets with incomplete trailing clauses trim at clause boundary."""
        long_line = (
            "Positive affectivity (PA): the tendency to experience pleasant "
            "feeling states such as enthusiasm, alertness, and joviality, "
            "whereas Negative Affectivity (NA): tendency to experience unpleasant"
        )
        result = _polish_bullet(long_line)
        assert "whereas" not in result.lower()
        assert "joviality" in result.lower()
        assert result.endswith("joviality")

    def test_cleans_inline_bullet_markers(self):
        result = _polish_bullet(
            "Items which: \u2022 indicate ability and \u2022 measure capacity"
        )
        assert "\u2022" not in result
        assert "indicate ability" in result
        assert "measure capacity" in result

    def test_caps_very_long_bullet(self):
        long = 'A' * 50 + '. ' + 'B' * 50 + '. ' + 'C' * 120
        result = _polish_bullet(long)
        assert len(result) <= 200


# ── build_deterministic_summary ─────────────────────────────────


class TestBuildDeterministicSummary:
    def test_produces_bullets_from_prose(self):
        text = (
            "The theory of comparative advantage explains international trade. "
            "Countries specialize in goods where they have lower opportunity costs. "
            "Free trade leads to mutual gains for trading partners. "
            "Tariffs and quotas reduce overall economic welfare."
        )
        file_texts = [("slides.pdf", text)]
        bullets = build_deterministic_summary(file_texts, max_bullets=3)
        assert len(bullets) == 3
        assert all(len(b) > 20 for b in bullets)

    def test_empty_input(self):
        assert build_deterministic_summary([]) == []

    def test_all_boilerplate_input(self):
        file_texts = [("notes.txt", "Page 1\n42\nhttps://example.com")]
        assert build_deterministic_summary(file_texts) == []

    def test_respects_max_bullets(self):
        lines = [f"Important concept number {i} is about economic theory" for i in range(20)]
        text = "\n\n".join(lines)
        file_texts = [("doc.txt", text)]
        bullets = build_deterministic_summary(file_texts, max_bullets=4)
        assert len(bullets) <= 4

    def test_multiple_files_combined(self):
        file_texts = [
            ("a.txt", "First file has important theory about economics and trade."),
            ("b.txt", "Second file discusses applications of these theories in practice."),
        ]
        bullets = build_deterministic_summary(file_texts)
        assert len(bullets) >= 1

    def test_filters_slide_noise(self):
        """Realistic slide-like input should produce clean bullets."""
        text = (
            "MG488\n"
            "Dr. Jane Smith\n"
            "Department of Management\n"
            "Autumn Term 2025\n\n"
            "Outline\n\n"
            "Comparative advantage determines trade patterns\n"
            "based on relative costs rather than absolute costs\n\n"
            "The Heckscher-Ohlin model predicts that countries export goods\n"
            "that use their abundant factor intensively\n\n"
            "Questions?\n"
            "Thank you"
        )
        file_texts = [("lecture1.pptx", text)]
        bullets = build_deterministic_summary(file_texts)
        assert len(bullets) >= 1
        combined = " ".join(bullets).lower()
        # Should NOT contain slide noise
        assert "dr. jane" not in combined
        assert "outline" not in combined
        assert "questions" not in combined
        assert "thank you" not in combined
        assert "mg488" not in combined
        # Should contain actual content
        assert "comparative advantage" in combined or "heckscher" in combined

    def test_merges_broken_fragments(self):
        """Broken line wraps should be merged into coherent bullets."""
        text = (
            "Markets tend toward equilibrium\n"
            "because of the price adjustment mechanism\n"
            "that operates through supply and demand"
        )
        file_texts = [("slides.pdf", text)]
        bullets = build_deterministic_summary(file_texts)
        assert len(bullets) >= 1
        # The fragments should be merged
        assert "because" in bullets[0].lower()

    def test_deduplicates_repeated_content(self):
        text = (
            "Supply and demand determine market equilibrium prices.\n\n"
            "Supply and demand determine market equilibrium prices.\n\n"
            "Externalities cause market failure when costs are not internalised."
        )
        file_texts = [("notes.pdf", text)]
        bullets = build_deterministic_summary(file_texts)
        # Should not have duplicate bullets
        lower_bullets = [b.lower() for b in bullets]
        assert len(lower_bullets) == len(set(lower_bullets))

    def test_prefers_propositional_content(self):
        """Lines with verbs/explanations should rank above bare labels."""
        text = (
            "Key Concepts:\n\n"
            "TRADE THEORY\n\n"
            "Firms maximize profit where marginal cost equals marginal revenue\n\n"
            "MARKET STRUCTURE\n\n"
            "Perfect competition leads to allocative efficiency in equilibrium"
        )
        file_texts = [("slides.pptx", text)]
        bullets = build_deterministic_summary(file_texts, max_bullets=2)
        assert len(bullets) == 2
        combined = " ".join(bullets).lower()
        # The propositional lines should beat the labels
        assert "marginal" in combined or "efficiency" in combined

    def test_excludes_low_quality_noise(self):
        """Lines below the quality floor should not appear in output."""
        text = (
            "Interaction with thought leaders\n\n"
            "The equilibrium model explains how markets clear through price adjustment\n\n"
            "Team presentations next week\n\n"
            "Comparative advantage determines trade patterns between nations"
        )
        file_texts = [("slides.pptx", text)]
        bullets = build_deterministic_summary(file_texts)
        combined = " ".join(bullets).lower()
        assert "interaction with" not in combined
        assert "team presentations" not in combined
        assert "equilibrium" in combined or "comparative advantage" in combined

    def test_suppresses_quotes_and_objectives(self):
        """Logistics-heavy intro deck should produce clean output."""
        text = (
            '\u201c\u2026Organisations are complex, surprising, deceptive, '
            'and ambiguous\u201d\n\n'
            'Identify a range of strategies to increase your effectiveness '
            'as a manager\n\n'
            'Your blog is an opportunity to share your viewpoint on a topic\n\n'
            'You will be allocated to a blog group based on one seminar\n\n'
            'Management involves understanding how people behave in '
            'organizational settings and why\n\n'
            'Effective managers develop skills in communication, '
            'decision-making, and strategic analysis'
        )
        file_texts = [("intro.pptx", text)]
        bullets = build_deterministic_summary(file_texts)
        combined = " ".join(bullets).lower()
        assert "your blog" not in combined
        assert "allocated" not in combined
        assert "identify a range" not in combined
        # Should contain actual content
        assert "manage" in combined or "decision" in combined

    def test_realistic_lecture_deck(self):
        """Content-rich deck should produce concept-focused bullets."""
        text = (
            'Consider the purpose of the theory: What is being explained?\n\n'
            'OB examines how individuals behave within organizations shaped '
            'by hierarchies, teams, and leadership structures\n\n'
            'Trait activation theory explains how personality traits are '
            'enacted through behaviour when activated by contextual cues\n\n'
            'Big-O (Organization): focuses on structures, routines, and '
            'authority systems that pattern behaviour\n\n'
            'Think about how this applies to your experience\n\n'
            'Management is a mechanism for achieving multiple interests '
            'within organizations'
        )
        file_texts = [("lecture.pptx", text)]
        bullets = build_deterministic_summary(file_texts, max_bullets=4)
        combined = " ".join(bullets).lower()
        # Meta-instructional prompts excluded
        assert "consider the purpose" not in combined
        assert "think about" not in combined
        # Conceptual content included
        assert any(
            kw in combined
            for kw in ("ob examines", "trait activation", "big-o", "mechanism")
        )

    def test_condenses_filler_in_bullets(self):
        """Filler phrases should be stripped from the final bullets."""
        text = (
            "It is important to note that firms choose governance structures "
            "to minimize transaction costs\n\n"
            "According to Coase, markets and firms are alternative governance "
            "mechanisms for organizing production\n\n"
            "Research suggests that vertical integration reduces hold-up risk "
            "when asset specificity is high"
        )
        file_texts = [("lecture.pptx", text)]
        bullets = build_deterministic_summary(file_texts)
        combined = " ".join(bullets).lower()
        # Filler should be stripped
        assert "it is important to note" not in combined
        assert "according to coase" not in combined
        assert "research suggests that" not in combined
        # Core content preserved
        assert "transaction costs" in combined or "governance" in combined

    def test_diverse_bullet_selection(self):
        """Bullets should cover different topics, not cluster on one."""
        text = (
            "Transaction costs arise from uncertainty in market exchanges\n\n"
            "Transaction costs include search, bargaining, and enforcement\n\n"
            "Transaction costs determine whether firms or markets govern\n\n"
            "Agency theory examines conflicts between principals and agents\n\n"
            "Agency problems arise when interests diverge between parties\n\n"
            "Property rights define who controls and benefits from assets"
        )
        file_texts = [("lecture.pptx", text)]
        bullets = build_deterministic_summary(file_texts, max_bullets=3)
        combined = " ".join(bullets).lower()
        # Should cover at least 2 distinct topics (not 3 transaction cost bullets)
        topics_found = sum([
            "transaction" in combined,
            "agency" in combined,
            "property" in combined,
        ])
        assert topics_found >= 2

    def test_inline_citations_stripped(self):
        """Inline academic citations should not appear in final bullets."""
        text = (
            "Firms benefit from specialization in production (Smith, 2020)\n\n"
            "Market efficiency depends on information availability [3]\n\n"
            "Institutions shape economic outcomes through incentive structures"
        )
        file_texts = [("paper.pdf", text)]
        bullets = build_deterministic_summary(file_texts)
        combined = " ".join(bullets)
        assert "(Smith, 2020)" not in combined
        assert "[3]" not in combined

    def test_filters_bibliography_entries(self):
        """Bibliography/citation lines should never survive as bullets."""
        text = (
            "Entrepreneurial motivation drives firm performance and growth\n\n"
            "Portocarrero, F. F., Newbert, S. L., Young, M. J., & Zhu, L. Y. "
            "(2025). The affective revolution in entrepreneurship: An "
            "integrative conceptual review and guidelines for future "
            "investigation.\n\n"
            "Affect plays a significant role in entrepreneurial decision-making"
        )
        file_texts = [("slides.pptx", text)]
        bullets = build_deterministic_summary(file_texts)
        combined = " ".join(bullets).lower()
        assert "portocarrero" not in combined
        assert "integrative conceptual review" not in combined

    def test_filters_incomplete_prompt_tails(self):
        """Truncated prompt/question lines should not survive as bullets."""
        text = (
            "Leadership effectiveness depends on situational awareness "
            "and adaptability\n\n"
            "How might I show up and manage differently under extreme "
            "pressure/stress compared to when\n\n"
            "Emotional intelligence involves understanding and managing "
            "your own emotions and those of others"
        )
        file_texts = [("slides.pptx", text)]
        bullets = build_deterministic_summary(file_texts)
        combined = " ".join(bullets).lower()
        assert "how might i show up" not in combined

    def test_penalizes_learning_objectives(self):
        """Bare imperative objectives should lose to substantive content."""
        text = (
            "Understand the principles of strengths-based leadership\n\n"
            "Transformational leadership creates organizational change "
            "through vision and inspiration\n\n"
            "Authentic leaders build trust by demonstrating consistency "
            "between values and actions"
        )
        file_texts = [("slides.pptx", text)]
        bullets = build_deterministic_summary(file_texts, max_bullets=2)
        combined = " ".join(bullets).lower()
        assert "understand the principles" not in combined
        assert "transformational" in combined or "authentic" in combined

    def test_cleans_inline_bullet_markers_in_output(self):
        """Inline bullet characters should be cleaned in final output."""
        text = (
            "Strategic planning involves setting long-term organizational "
            "goals and objectives\n\n"
            "Impacting non-energisers are lower bars which: \u2022 Don't "
            "indicate ability but the level of energy you have after "
            "completing the task"
        )
        file_texts = [("slides.pptx", text)]
        bullets = build_deterministic_summary(file_texts)
        for b in bullets:
            assert "\u2022" not in b

    def test_filters_promotional_slogans(self):
        """Promotional/motivational slogans should not appear as bullets."""
        text = (
            "YOUR STRENGTHS ARE UNIQUE 1 in 346,000\n\n"
            "There are 24 strengths and no right or wrong answers\n\n"
            "Strengths-based leadership development is a continuous journey\n\n"
            "Transformational leadership creates organizational change "
            "through vision and inspiration"
        )
        file_texts = [("slides.pptx", text)]
        bullets = build_deterministic_summary(file_texts)
        combined = " ".join(bullets).lower()
        assert "unique" not in combined or "transformational" in combined
        assert "no right or wrong" not in combined
        assert "continuous journey" not in combined

    def test_filters_consent_and_platform_admin(self):
        """Consent/ToS/survey lines should not survive as bullets."""
        text = (
            "Content you share will be processed in accordance with "
            "Lovable terms and conditions\n\n"
            "A survey will be used to collect that consent.\n\n"
            "Agency theory examines conflicts between principals and agents\n\n"
            "Transaction costs arise from uncertainty in market exchanges"
        )
        file_texts = [("slides.pptx", text)]
        bullets = build_deterministic_summary(file_texts)
        combined = " ".join(bullets).lower()
        assert "in accordance with" not in combined
        assert "consent" not in combined
        assert "agency" in combined or "transaction" in combined

    def test_filters_long_comma_lists(self):
        """Long comma-separated term lists should not survive as bullets."""
        text = (
            "CAD, development, gamification, virtualization, accounting, "
            "collaboration, customer relationship management, Management\n\n"
            "Firms maximize profit where marginal cost equals marginal revenue\n\n"
            "Comparative advantage determines trade patterns between nations"
        )
        file_texts = [("slides.pptx", text)]
        bullets = build_deterministic_summary(file_texts)
        combined = " ".join(bullets).lower()
        assert "gamification" not in combined
        assert "marginal" in combined or "comparative" in combined

    def test_filters_parenthetical_example_and_quote_fragments(self):
        """Parenthetical fragments, example labels, and clipped quotes should not survive."""
        text = (
            "Enterprise value equals equity value plus debt minus cash\n\n"
            "(ignoring fixed assets, which are very small)\n\n"
            "Example: exit in 2014 at 6x multiple implies Enterprise Value of $210 million\n\n"
            "We’d have to be careful ---we don’t want to hire a bunch of unproductive people. But I think we could pull this off.”"
        )
        file_texts = [("slides.pdf", text)]
        bullets = build_deterministic_summary(file_texts)
        combined = " ".join(bullets).lower()
        assert "ignoring fixed assets" not in combined
        assert "example:" not in combined
        assert "we’d have to be careful" not in combined
        assert "enterprise value equals equity value plus debt minus cash" in combined

    def test_filters_pronoun_led_fragments_when_named_content_exists(self):
        """Weak pronoun-led continuations should lose to substantive named content."""
        text = (
            "Strengths influence performance when they are aligned with the demands of the role\n\n"
            "Leaders create better outcomes by applying strengths in context rather than generically\n\n"
            "They have the potential to impact your performance and management effectiveness\n\n"
            "Non-strengths are not necessarily weaknesses, but they can drain energy if required repeatedly"
        )
        file_texts = [("slides.pdf", text)]
        bullets = build_deterministic_summary(file_texts, max_bullets=3)
        combined = " ".join(bullets).lower()
        assert "they have the potential" not in combined
        assert "aligned with the demands of the role" in combined or "applying strengths in context" in combined

    def test_filters_contextless_century_fragments(self):
        """Historical time-label fragments should not outrank cleaner conceptual bullets."""
        text = (
            "Theory is an understanding of what leads to what and why\n\n"
            "Economies of scale lower average costs as output rises\n\n"
            "16th and 17th C: emergence of some of the most remarkable business organisations the world has ever seen.\n\n"
            "Power: In ancient times wealth generally was the reward for political, religious or military power – not for economic activity"
        )
        file_texts = [("slides.pptx", text)]
        bullets = build_deterministic_summary(file_texts, max_bullets=3)
        combined = " ".join(bullets).lower()
        assert "16th and 17th c" not in combined
        assert "in ancient times" not in combined
        assert "economies of scale" in combined or "what leads to what and why" in combined


# ── build_summary (high-level) ──────────────────────────────────


class TestBuildSummary:
    def test_extractive_with_content(self):
        text = (
            "The efficient market hypothesis states that prices reflect all information.\n\n"
            "This has important implications for portfolio management strategies."
        )
        file_texts = [("slides.pdf", text)]
        result = build_summary(file_texts)
        assert result["method"] == "extractive"
        assert len(result["bullets"]) >= 1
        assert result["section_type"] == "normal"
        assert result["file_count"] == 1

    def test_fallback_with_no_content(self):
        result = build_summary([], section_type="reading")
        assert result["method"] == "fallback"
        assert len(result["bullets"]) == 4
        assert result["section_type"] == "reading"

    def test_generic_fallback_for_empty_normal_week(self):
        result = build_summary([], section_type="normal")
        assert result["method"] == "fallback"
        assert "not yet available" in result["bullets"][0].lower()

    def test_exam_fallback(self):
        result = build_summary([], section_type="exam")
        assert result["method"] == "fallback"
        assert any("exam" in b.lower() for b in result["bullets"])

    def test_admin_content_fallback(self):
        """Files exist but all content is admin/logistics → clearer fallback."""
        # Only admin-heavy content, no subject matter
        file_texts = [("intro.pptx", "Please attend all sessions. Submit by the deadline.")]
        result = build_summary(file_texts)
        assert result["method"] == "fallback"
        assert result["file_count"] == 1
        assert any("introductory" in b.lower() or "administrative" in b.lower()
                    for b in result["bullets"])


# ── build_weekly_summary (shared orchestration) ─────────────────


class _StubClient:
    """Minimal stand-in for MoodleClient used by build_weekly_summary tests."""

    def __init__(self, *, sections=None, file_bytes=None):
        self._sections = sections or []
        self._file_bytes = file_bytes
        self.downloaded: list[str] = []

    def get_course_contents(self, course_id):
        return self._sections

    def download_file(self, fileurl, *, max_bytes=None):
        self.downloaded.append(fileurl)
        return self._file_bytes


def _text_pdf_bytes(text: str) -> bytes:
    """Plain .txt bytes — extraction works on TXT without extra deps."""
    return text.encode("utf-8")


class TestBuildWeeklySummary:
    def test_no_section_falls_back(self):
        client = _StubClient(sections=[])
        result = build_weekly_summary(client, 42, 1)
        assert result["method"] == "fallback"
        assert result["course_id"] == 42
        assert result["week"] == 1
        assert result["section_name"] == ""

    def test_section_without_modules_falls_back(self):
        sections = [
            _make_section("Reading Week", 3, []),
        ]
        client = _StubClient(sections=sections)
        result = build_weekly_summary(client, 42, 3)
        assert result["method"] == "fallback"
        assert result["section_type"] == "reading"
        assert result["section_name"] == "Reading Week"
        assert result["course_id"] == 42

    def test_pre_supplied_sections_skip_fetch(self):
        called = {"n": 0}

        class _Client(_StubClient):
            def get_course_contents(self, course_id):
                called["n"] += 1
                return super().get_course_contents(course_id)

        client = _Client(sections=[])
        build_weekly_summary(client, 42, 1, sections=[])
        assert called["n"] == 0

    def test_on_extract_invoked_per_file(self):
        sections = [
            _make_section("Week 1: Intro", 1, [
                _make_module(10, "Slides", contents=[
                    _make_file_content("w1.txt"),
                ]),
            ]),
        ]
        # Plain text bytes so extract_file_text returns substantive content.
        client = _StubClient(
            sections=sections,
            file_bytes=_text_pdf_bytes(
                "Markets tend toward equilibrium because of price adjustment. "
                "Supply and demand forces determine prevailing prices."
            ),
        )
        seen: list[str] = []
        result = build_weekly_summary(
            client, 7, 1,
            on_extract=lambda f: seen.append(f),
        )
        assert seen == ["w1.txt"]
        assert result["course_id"] == 7
        assert result["week"] == 1
        assert result["section_name"] == "Week 1: Intro"

    def test_failed_download_does_not_raise(self):
        sections = [
            _make_section("Week 1: Intro", 1, [
                _make_module(10, "Slides", contents=[
                    _make_file_content("w1.pdf"),
                ]),
            ]),
        ]
        # download_file returns None → fall through to fallback.
        client = _StubClient(sections=sections, file_bytes=None)
        result = build_weekly_summary(client, 42, 1)
        assert result["section_name"] == "Week 1: Intro"
        assert result["course_id"] == 42
        # No content extracted, so fallback bullets kick in.
        assert result["method"] == "fallback"


# ── format_bullets ──────────────────────────────────────────────


class TestFormatBullets:
    def test_default_marker(self):
        result = format_bullets(["First", "Second"])
        assert result == "  \u2022 First\n  \u2022 Second"

    def test_custom_marker(self):
        result = format_bullets(["A", "B"], marker="-")
        assert result == "  - A\n  - B"

    def test_empty_list(self):
        assert format_bullets([]) == ""
