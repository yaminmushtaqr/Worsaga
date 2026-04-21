"""Internal text-processing helpers for deterministic bullet generation.

Line-level pipeline backing :func:`worsaga.summaries.build_deterministic_summary`:
fragment merging, deduplication, scoring, condensing, polishing, rejection
filters, and MMR-style diverse selection.

All symbols here are leading-underscore / private by convention — they are
implementation details of the summary pipeline and should not be imported
from outside the ``worsaga`` package.
"""

from __future__ import annotations

import re

from worsaga.extraction import is_boilerplate


# ── Fragment merging & line-level synthesis ─────────────────────

# Words that signal a line is a continuation of the previous one.
_CONTINUATION_STARTS = frozenset({
    "and", "or", "but", "nor", "yet", "so", "for", "as", "if",
    "that", "which", "where", "when", "while", "because", "although",
    "though", "in", "on", "at", "by", "with", "from", "to", "than",
    "rather", "such", "not", "between", "versus", "vs", "vs.",
    "e.g.", "i.e.", "ie", "eg",
})

# Words that boost a line's informativeness score.
_SIGNAL_WORDS = (
    "means", "defined as", "refers to", "implies", "leads to",
    "because", "therefore", "however", "although", "whereas",
    "framework", "model", "theory", "explains", "determines",
    "suggests", "relationship between", "effect of", "caused by",
    "result of", "in contrast", "for example", "such as",
    "according to", "argument", "approach", "distinguish",
    "characteristic", "principle", "concept", "assumes",
    "predicts", "incentive", "trade-off", "equilibrium",
    "optimal", "constraint", "mechanism", "strategy",
)

# Light verb / copula indicators — lines containing these are more
# likely to express a proposition rather than being a bare label.
_VERB_INDICATORS = (
    " is ", " are ", " was ", " were ", " has ", " have ", " had ",
    " can ", " may ", " will ", " should ", " must ", " does ",
    " do ", " did ", " provide", " create", " determine",
    " affect", " influence", " lead", " cause", " result",
    " suggest", " show", " explain", " describe", " define",
    " argue", " claim", " increase", " decrease", " reduce",
    " improve", " depend", " require", " allow", " enable",
    " involve", " represent", " reflect", " assume", " predict",
    " maximize", " maximise", " minimize", " minimise",
    " equal", " generate", " produce", " emerge", " occur",
    " specialize", " specialise", " operate", " function",
    " tend", " drive", " shape", " promote", " restrict",
    " focus", " develop", " establish", " demonstrate",
    " indicate", " examine", " discuss", " identify",
)


# Words that, when trailing a line, signal the next line is a continuation.
_TRAILING_CONNECTORS = frozenset({
    "and", "or", "but", "nor", "yet", "so", "whereas", "while",
    "although", "because", "that", "which", "with", "from",
    "for", "in", "on", "at", "by", "to", "as", "of",
})


# Patterns indicating causal / explanatory / definitional structure —
# lines matching these are strong study-note candidates.
_CAUSAL_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r'\bbecause\b.{3,}',
        r'\bleads?\s+to\b',
        r'\bresults?\s+in\b',
        r'\bcauses?\b.{3,}\b(?:by|of|the|a|an|when)\b',
        r'\bimpl(?:y|ies)\s+that\b',
        r'\bmeans?\s+that\b',
        r'\bdefined\s+as\b',
        r'\brefers?\s+to\b',
        r'\bdistinguish\w*\s+between\b',
        r'\b(?:differs?|different)\s+from\b',
        r'\bin\s+contrast\s+(?:to|with)\b',
        r'\bwhereas\b.{10,}',
        r'\brather\s+than\b',
        r'\b(?:therefore|thus|hence|consequently)\b',
    )
]


def _looks_like_reference(line: str) -> bool:
    """Return True if *line* looks like a citation/reference entry."""
    lower = line.lower().strip()
    if not lower:
        return False

    if (re.search(r'[A-Z]\w+,\s+[A-Z]\.', line) and
            re.search(r'(?:\((?:19|20)\d{2}\w?\)|\b(?:19|20)\d{2}\b)', line)):
        return True

    if re.match(r'^\(?(?:19|20)\d{2}\)?\s*[:.\-]?\s*["“]?chapter\s+\d+', lower):
        return True

    if (re.search(r'\b(?:financial times|journal of|human resource management|'
                  r'harvard business review|academy of management|doi)\b', lower)
            and re.search(r'\b(?:19|20)\d{2}\b', lower)):
        return True

    return False


def _is_continuation(line: str) -> bool:
    """True if *line* looks like it continues the previous line."""
    if not line:
        return False
    # Starts with a lowercase letter → almost always a continuation.
    if line[0].islower():
        return True
    # Starts with a digit + comma/punctuation (e.g. "2, and ..." from a
    # page break splitting "Year\n2, and ...").
    if re.match(r"^\d+[,;)\s]", line):
        return True
    first_word = line.split()[0].lower().rstrip(".,;:")
    return first_word in _CONTINUATION_STARTS


def _ends_mid_thought(line: str) -> bool:
    """True if *line* ends with a connector, suggesting the next line continues it."""
    if not line:
        return False
    last_word = line.rstrip(".,;: ").rsplit(None, 1)[-1].lower()
    return last_word in _TRAILING_CONNECTORS


def _merge_fragments(text: str) -> list[str]:
    """Split *text* into logical content lines, merging broken fragments.

    Lecture-slide text often has hard line breaks mid-sentence.  This
    function joins continuation lines back together and returns a list
    of coherent phrases/sentences.
    """
    raw_lines = text.split("\n")
    merged: list[str] = []
    current: list[str] = []

    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped:
            # Blank line → flush current accumulator.
            if current:
                merged.append(" ".join(current))
                current = []
            continue

        # Merge if this line is a continuation OR the previous line ended
        # mid-thought (trailing connector word).
        prev_continues = current and _ends_mid_thought(current[-1])
        if current and (_is_continuation(stripped) or prev_continues):
            current.append(stripped)
        else:
            if current:
                merged.append(" ".join(current))
            current = [stripped]

    if current:
        merged.append(" ".join(current))

    # For long merged lines (prose paragraphs), split on sentence
    # boundaries so each sentence can be scored independently.
    final: list[str] = []
    for line in merged:
        if len(line) > 200:
            sentences = re.split(r"(?<=[.!?])\s+", line)
            final.extend(s.strip() for s in sentences if s.strip())
        else:
            final.append(line)

    # Drop very short fragments (isolated labels/keywords).
    return [line for line in final if len(line) >= 15]


def _deduplicate_lines(lines: list[str]) -> list[str]:
    """Remove exact and near-duplicate lines (word-level overlap)."""
    result: list[str] = []
    seen_keys: set[str] = set()
    seen_word_sets: list[frozenset[str]] = []

    for line in lines:
        key = re.sub(r"[^\w\s]", "", line.lower())
        key = " ".join(key.split())

        if key in seen_keys:
            continue

        words = frozenset(key.split())
        if len(words) >= 3:
            is_dup = False
            for existing_words in seen_word_sets:
                if len(existing_words) < 3:
                    continue
                overlap = len(words & existing_words)
                threshold = min(len(words), len(existing_words)) * 0.8
                if overlap >= threshold:
                    is_dup = True
                    break
            if is_dup:
                continue
            seen_word_sets.append(words)

        seen_keys.add(key)
        result.append(line)

    return result


def _score_line(line: str) -> float:
    """Score a line for informativeness (higher = better study-note)."""
    score: float = 0.0
    length = len(line)
    lower = line.lower()

    # Length sweet-spot: 40–200 chars.
    if 40 <= length <= 200:
        score += 20
    elif 25 <= length < 40:
        score += 10
    elif length > 200:
        score += 12

    # Signal-word bonus.
    for sw in _SIGNAL_WORDS:
        if sw in lower:
            score += 8

    # Verb presence → propositional content, not a label.
    if any(v in lower for v in _VERB_INDICATORS):
        score += 15

    # Colon inside (likely a definition) — but not a trailing colon.
    if ":" in line and not line.rstrip().endswith(":"):
        colon_pos = line.index(":")
        after_colon = line[colon_pos + 1:].strip()
        # Stronger bonus when the part after : is a substantive explanation.
        if len(after_colon) > 30 and any(v in after_colon.lower() for v in _VERB_INDICATORS):
            score += 12
        else:
            score += 5

    # Causal/explanatory/definitional structure bonus.
    if any(pat.search(lower) for pat in _CAUSAL_PATTERNS):
        score += 12

    # ── Penalties ──
    # All-caps heading.
    if line.isupper() and length < 60:
        score -= 20

    # Trailing question mark (question heading / prompt, not content).
    if line.endswith("?") and length < 100:
        score -= 10

    # Very short → probably a heading/label.
    if length < 25:
        score -= 15

    # Trailing colon → section header, not content.
    if line.rstrip().endswith(":"):
        score -= 10

    # Administrative / logistical content — not study material.
    _ADMIN_SIGNALS = (
        "you are expected", "attendance", "attend all",
        "register", "sign up", "office hours", "email me",
        "submit by", "due date", "deadline", "submission",
        "assessment criteria", "marking scheme", "grading",
        "class participation", "seminar preparation",
        "lectures are", "not recorded", "in person lecture",
        "in person seminar", "virtual lecture", "hybrid",
        "unable to attend", "reasons for absence", "please email",
        "formative", "summative", "coursework worth",
        "essay title", "essay question", "choice of two",
        "receive an email", "receive email", "receive your",
        "instructions to", "more information in",
        "will be published", "will be marked", "will be graded",
        "complete your", "complete online", "complete the questionnaire",
        "see dates", "see moodle", "fortnightly",
        "meeting with", "attend meeting", "your allocated",
        "allocated day", "allocated time", "on campus",
        "share your", "your point of view",
        "unique strengths", "discussion opportunities",
        "group members", "your ideas and progress",
    )
    _admin_count = sum(1 for sig in _ADMIN_SIGNALS if sig in lower)
    if _admin_count >= 1:
        score -= 25
    if _admin_count >= 2:
        score -= 15  # Extra penalty for heavily admin content

    # Lines containing email addresses — contact info, not content.
    if re.search(r'\b[\w.+-]+@[\w.-]+\.\w+\b', lower):
        score -= 25

    # Student-directed instructions (logistics, not content).
    # "You will...", "We will..." without domain signal words
    # are almost always logistics/scheduling lines.
    if re.match(r"^(you will|you should|you need|you must|we will)\b", lower):
        if not any(sw in lower for sw in _SIGNAL_WORDS):
            score -= 20

    # High second-person pronoun density signals instructions to students.
    _you_count = len(re.findall(r'\byou(?:r|rs|rself)?\b', lower))
    if _you_count >= 2:
        score -= 20

    # Meta-instructional lines (questions/prompts to the reader).
    if re.match(r"^(consider|think about|discuss|reflect on|what)\b", lower):
        score -= 20
        if line.endswith("?"):
            score -= 10  # Extra penalty for question prompts

    # Instructional prompts after a heading / colon are not revision notes.
    if re.search(
        r':\s*(discuss|describe|identify|select|outline|create|list|'
        r'compare|consider|reflect|explain)\b',
        lower,
    ):
        score -= 30

    if re.match(r'^\(?multiple\s+choice\)?', lower):
        score -= 40

    # Reflective/meta question prompts: "How might...", "Why do...", etc.
    if re.match(
        r"^(how\s+(?:might|do|can|would|could|should|does|did)|"
        r"why\s+(?:do|does|did|might|would|could|should|is|are)|"
        r"in\s+what\s+way)",
        lower,
    ):
        score -= 20
        if line.endswith("?"):
            score -= 10

    # Direct quotation: starts with opening quote mark.
    if re.match(r'^["\u201c\u2018\u00ab]', line):
        score -= 15
        # Full wrapped quote (also ends with closing quote mark).
        if re.search(r'["\u201d\u2019\u00bb][.!?]?\s*$', line):
            score -= 15

    # Ellipsis suggests partial/attributed quote.
    if '\u2026' in line or re.search(r'(?<!\.)\.\.\.(?!\.)', line):
        score -= 10

    # Course-objective / learning-outcome language.
    _OBJECTIVE_SIGNALS = (
        "identify a range", "you will learn", "you will be able",
        "by the end of", "learning outcome", "module aim",
        "course objective", "aim of this", "goal of this",
        "able to explain", "able to describe", "able to apply",
        "able to identify", "able to analyse", "able to evaluate",
    )
    if any(sig in lower for sig in _OBJECTIVE_SIGNALS):
        score -= 25

    # Blog / group-work / team-admin language.
    _GROUP_ADMIN = (
        "your blog", "blog group", "your team", "team project",
        "group project", "your presentation", "allocated to",
        "will be allocated",
    )
    if any(sig in lower for sig in _GROUP_ADMIN):
        score -= 25

    # Promotional / motivational / slogan-like language.
    _PROMOTIONAL_SIGNALS = (
        "continuous journey", "are unique", "are special",
        "no right or wrong", "it's about", "is key to",
        "is critical to", "is essential to", "is important to",
        "is a journey", "is a process",
    )
    if any(sig in lower for sig in _PROMOTIONAL_SIGNALS):
        score -= 20

    # Platform consent / ToS / survey logistics language.
    _PLATFORM_ADMIN = (
        "in accordance with", "terms of service", "terms and conditions",
        "consent", "privacy policy", "survey will be",
        "proprietary", "commercially sensitive",
        "processed in accordance",
    )
    if any(sig in lower for sig in _PLATFORM_ADMIN):
        score -= 25

    # Weak pronoun-led continuations are often context-dependent fragments.
    if re.match(
        r'^(they|it|this|these|those)\s+'
        r'(have|has|had|are|is|were|was|can|could|may|might|would|will|should)\b',
        lower,
    ):
        score -= 18

    # Parenthetical fragments and example labels rarely make good study notes.
    if re.match(r'^\([^)]{1,140}\)$', line.strip()):
        score -= 45
    if re.match(r'^(example|for example)\s*:', lower):
        score -= 25

    # Time/period labels often introduce contextual narration rather than a concept.
    if re.match(
        r'^(?:\d{1,2}(?:st|nd|rd|th)\s+(?:and\s+\d{1,2}(?:st|nd|rd|th)\s+)?'
        r'c(?:entury)?|\d{4}s?)\s*:',
        lower,
    ):
        score -= 25
    if re.match(
        r'^[a-z][a-z/&\- ]{0,20}:\s+(?:in\s+(?:ancient|medieval|early|late)\b|'
        r'\d{1,2}(?:st|nd|rd|th)\b)',
        lower,
    ):
        score -= 20

    # Conversational first-person quote fragments read badly as study bullets.
    _first_person_count = len(re.findall(r'\b(?:i|we|our|us|my)\b', lower))
    if _first_person_count >= 2:
        score -= 20

    # Bibliography / citation-only lines are not useful bullets.
    if _looks_like_reference(line):
        score -= 45

    # Bare imperative learning objectives (Bloom's taxonomy verbs).
    # "Understand the principles..." is an objective, not study content.
    if re.match(
        r'^(understand|explain|describe|analyse|analyze|evaluate|apply|'
        r'demonstrate|compare|contrast|outline|summarise|summarize|'
        r'define|assess|examine|explore|investigate|recognise|recognize|'
        r'distinguish|classify|illustrate|identify|'
        r'critically\s+\w+)\s+'
        r'(the|how|what|why|key|various|different|a\b|an\b|your)',
        lower,
    ):
        score -= 30

    # Truncated lines ending with a trailing connector/preposition.
    _trail = line.rstrip(".,;: ")
    if _trail:
        _last_word = _trail.rsplit(None, 1)[-1].lower()
        if _last_word in _TRAILING_CONNECTORS or _last_word in {
            "the", "a", "an", "when", "where", "whether", "than",
        }:
            score -= 20

    # Inline bullet markers produce awkward output.
    if re.search(r'[\u2022\u2023\u25e6\u2043]', line):
        score -= 15

    # Quiz / worksheet prompts and assessment instructions.
    if re.search(
        r'\b(?:multiple choice|look(?:ing)?\s+through\s+the\s+list|'
        r'select\s+\d+\s*-\s*\d+|within\s+the\s+body\s+of\s+the\s+essay|'
        r'questionnaire)\b',
        lower,
    ):
        score -= 35

    # Quote-heavy or footnoted lines usually read badly as study notes.
    if re.search(r'["“”‘’]', line) and re.search(r'\d+\s*$', line.strip('”\" ')):
        score -= 25

    # Lines with no content indicators (no signal words AND no verb
    # indicators) are likely headings, agenda items, or structural text.
    if (not any(sw in lower for sw in _SIGNAL_WORDS) and
            not any(v in lower for v in _VERB_INDICATORS)):
        score -= 10

    # Short lines (25–50 chars) without clear verbs: likely agenda/outline items.
    # Uses word-boundary matching to avoid false positives from nouns
    # like "leaders" matching the substring " lead".
    if 25 <= length <= 50:
        _has_verb = bool(re.search(
            r'\b(is|are|was|were|has|have|had|can|may|will|should|must|'
            r'does|do|did|means?|explains?|determines?|suggests?|shows?|'
            r'leads?|causes?|results?|involves?|requires?|allows?|depends?|'
            r'affects?|influences?|creates?|provides?|represents?|reflects?|'
            r'assumes?|predicts?|produces?|tends?|drives?|shapes?|promotes?|'
            r'reduces?|improves?|increases?|decreases?|generates?|emerges?|'
            r'focus(?:es|sed)?|develops?|establishes?|demonstrates?|'
            r'indicates?|examines?|discusses?|identifies?)\b',
            lower,
        ))
        if not _has_verb:
            score -= 25

    # Comma-separated term lists (agenda/outline items, not propositions).
    comma_parts = [p.strip() for p in line.split(',') if p.strip()]
    if len(comma_parts) >= 5:
        score -= 25
    elif len(comma_parts) >= 3:
        if not any(v in lower for v in _VERB_INDICATORS):
            score -= 15

    return score


def _polish_bullet(line: str) -> str:
    """Clean up a line for use as a bullet point."""
    # Strip leading bullet markers the source text may contain.
    line = re.sub(r"^[\-\*\u2022\u2023\u25e6]+\s*", "", line)
    # Strip leading list numbering: "1. ", "2) ", etc.
    line = re.sub(r"^\d+[.\)]\s+", "", line)
    # Clean inline bullet markers into spaces for coherent sentences.
    line = re.sub(r':\s*[\-\*\u2022\u2023\u25e6\u2043]+\s*', ' ', line)
    line = re.sub(r'\s*[\u2022\u2023\u25e6\u2043]\s*', ' ', line)
    # Normalize capitalized helper verbs left behind by inline-bullet cleanup.
    line = re.sub(
        r'\b(which|that|and|but|or)\s+'
        r'(Don[\'’]t|Do|Does|Did|Is|Are|Was|Were|Have|Has|Had|Can|May|Will|Should|Must)\b',
        lambda m: f"{m.group(1)} {m.group(2).lower()}",
        line,
    )
    # Strip full-quote wrapping (opens and closes with quote marks).
    if (re.match(r'^["\u201c\u2018\u00ab]', line) and
            re.search(r'["\u201d\u2019\u00bb][.!?]?\s*$', line)):
        line = re.sub(r'^["\u201c\u2018\u00ab]+\s*', '', line)
        line = re.sub(r'\s*["\u201d\u2019\u00bb]+[.!?]?\d*\s*$', '', line)
    # Strip leading ellipsis with optional quote mark (partial attribution).
    line = re.sub(r'^["\u201c\u2018\u00ab]*[\u2026]+\s*', '', line)
    # Strip leading "For example, " (tangential reference).
    line = re.sub(r'^[Ff]or example,?\s+', '', line)
    line = line.strip()
    # Capitalize first letter.
    if line and line[0].islower():
        line = line[0].upper() + line[1:]
    # Remove trailing incomplete-thought markers.
    line = re.sub(r"[\-\u2013\u2014\u2026]+\s*$", "", line).strip()
    # Trim trailing dangling conjunctions/prepositions (broken continuation).
    line = re.sub(
        r",?\s+\b(and|or|but|whereas|while|although|because|that|which|"
        r"with|from|for|in|on|at|by|to|as|of)\s*$",
        "", line, flags=re.IGNORECASE,
    ).strip()
    # Trim unclosed trailing parenthetical.
    if '(' in line and ')' not in line[line.rindex('('):]:
        line = line[:line.rindex('(')].strip()
    # If the line is long, doesn't end with terminal punctuation, and
    # contains a clause boundary marker, trim to the last complete clause
    # rather than leaving a dangling fragment.
    if len(line) > 150 and not re.search(r'[.!?)]\s*$', line):
        clause_break = re.search(
            r',?\s+(?:whereas|while|although|however)\s+',
            line[80:],
        )
        if clause_break:
            trim_pos = 80 + clause_break.start()
            line = line[:trim_pos].rstrip(',; ')

    # Cap very long bullets at a natural sentence boundary.
    if len(line) > 200:
        m = re.search(r'[.!?]\s', line[:200])
        if m:
            line = line[:m.start() + 1].strip()
        else:
            line = line[:200].rsplit(' ', 1)[0].rstrip('.,;: ')
    return line


# ── Condensation ────────────────────────────────────────────────

# Leading filler phrases stripped case-insensitively from lines.
_LEADING_FILLER = [
    re.compile(p, re.IGNORECASE) for p in (
        r"^it\s+is\s+(?:important|essential|worth|useful)\s+to\s+"
        r"(?:note|understand|recogni[sz]e)\s+that\s+",
        r"^it\s+should\s+be\s+noted\s+that\s+",
        r"^(?:the\s+)?key\s+(?:point|takeaway|insight|message)\s+"
        r"(?:here\s+)?is\s+that\s+",
        r"^the\s+main\s+(?:point|takeaway|insight)\s+is\s+that\s+",
        r"^what\s+this\s+means\s+is\s+that\s+",
        r"^in\s+other\s+words,?\s+",
        r"^that\s+is\s+to\s+say,?\s+",
        r"^put\s+(?:simply|differently),?\s+",
        r"^(?:simply|essentially|basically|fundamentally),?\s+",
        r"^according\s+to\s+[\w\s.,']+?,\s+",
        r"^as\s+[\w\s.,']+?\s+(?:argues?|suggests?|notes?|"
        r"observes?|points?\s+out),?\s+",
        r"^(?:research|evidence)\s+(?:suggests?|shows?|indicates?|"
        r"demonstrates?)\s+that\s+",
        r"^studies\s+(?:have\s+)?(?:shown?|found|demonstrated?|"
        r"indicated?)\s+that\s+",
        r"^see\s+for\s+example:?\s+",
        r"^\(?multiple\s+choice\)?\s*",
    )
]

# Inline patterns stripped from lines (citations, cross-references).
_INLINE_STRIP = [
    re.compile(p) for p in (
        r"\s*\([\w\s.,&]+\d{4}\w?\)",
        r"\s*\[\d+(?:,\s*\d+)*\]",
        r"\s*\((?:see|cf\.?)\s+[^)]{1,40}\)",
        r"\s*\((?:ibid|op\.?\s*cit)\.?\)",
        r"^[A-Z][a-z]+\s+(?:et\s+al\.?\s*)?\(\d{4}\)\s*:\s*",
    )
]


def _condense_line(line: str) -> str:
    """Strip filler phrases, inline citations, and hedging from a line."""
    for pat in _LEADING_FILLER:
        line = pat.sub("", line, count=1)
    line = re.sub(
        r'^[A-Z][A-Za-z\'’.-]+\s*\((?:19|20)\d{2}\)\s*:\s+',
        '',
        line,
    )
    line = re.sub(r'^after\s+[^.]{0,140}\.\s*', '', line, flags=re.IGNORECASE)
    for pat in _INLINE_STRIP:
        line = pat.sub("", line)
    line = line.strip()
    if _looks_like_reference(line):
        return ""
    if line and line[0].islower():
        line = line[0].upper() + line[1:]
    return line


def _reject_final_bullet(line: str) -> bool:
    """Return True when a polished line is still not bullet-worthy."""
    lower = line.lower().strip()
    if not lower:
        return True

    if is_boilerplate(line) or _looks_like_reference(line):
        return True

    if re.match(r'^(week|lecture|seminar|session|topic)\s+\d+\s*[:\-–]', lower):
        return True

    if re.match(r'^\(?multiple\s+choice\)?', lower):
        return True

    if re.match(r'^\([^)]{1,140}\)$', line.strip()):
        return True

    if re.match(r'^(example|for example)\s*:', lower):
        return True

    if re.match(
        r'^(discuss|describe|identify|select|outline|create|list|'
        r'consider|reflect|look\b|understand|explain)\b',
        lower,
    ):
        return True

    if line.endswith('?'):
        return True

    # Trailing colon → section header / intro to a list, not content.
    if line.rstrip().endswith(':'):
        return True

    # Narrative openers that lack domain context (history fragments).
    if re.match(r'^It did\b', line):
        return True
    if re.match(r'^[A-Z][a-z]+ed\s+on\b.*:', line):
        return True
    if re.match(
        r'^(?:\d{1,2}(?:st|nd|rd|th)\s+(?:and\s+\d{1,2}(?:st|nd|rd|th)\s+)?'
        r'c(?:entury)?|\d{4}s?)\s*:',
        lower,
    ):
        return True
    if re.match(
        r'^[a-z][a-z/&\- ]{0,20}:\s+(?:in\s+(?:ancient|medieval|early|late)\b|'
        r'\d{1,2}(?:st|nd|rd|th)\b)',
        lower,
    ):
        return True

    if re.match(
        r'^(they|it|this|these|those)\s+'
        r'(have|has|had|are|is|were|was|can|could|may|might|would|will|should)\b',
        lower,
    ):
        return True

    if re.search(r'["“”‘’]', line) and re.search(r'\b(?:i|we|our|us|my)\b', lower):
        return True

    trail = line.rstrip('.,;: ')
    if trail:
        last_word = trail.rsplit(None, 1)[-1].lower()
        if last_word in _TRAILING_CONNECTORS | {'the', 'a', 'an', 'when', 'where', 'whether', 'than'}:
            return True

    return False


# ── Diverse bullet selection ───────────────────────────────────

# Stop words excluded from topic similarity calculations.
_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "shall", "should", "may", "might", "must", "can",
    "could", "and", "but", "or", "nor", "not", "so", "yet",
    "for", "at", "by", "from", "in", "into", "of", "on", "to",
    "with", "as", "if", "that", "than", "this", "these", "those",
    "it", "its", "they", "them", "their", "he", "she", "his",
    "her", "we", "our", "you", "your", "more", "most", "also",
    "how", "what", "when", "where", "which", "who", "why",
    "about", "between", "through", "during", "before", "after",
})


def _content_words(text: str) -> set[str]:
    """Extract content words from text (lowered, stop words removed)."""
    words = set(re.sub(r'[^\w\s]', '', text.lower()).split())
    return words - _STOP_WORDS


def _select_diverse(
    scored_lines: list[tuple[str, float]],
    max_items: int,
) -> list[tuple[str, float]]:
    """Select items maximising quality and topic diversity (MMR-style).

    For each slot after the first, the candidate with the best combined
    score of (quality - similarity_penalty) is chosen.
    """
    if len(scored_lines) <= max_items:
        return list(scored_lines)

    selected: list[tuple[str, float]] = [scored_lines[0]]
    sel_words: list[set[str]] = [_content_words(scored_lines[0][0])]
    candidates = list(scored_lines[1:])

    while len(selected) < max_items and candidates:
        best_idx = 0
        best_combined = -float('inf')

        for i, (line, score) in enumerate(candidates):
            words = _content_words(line)
            max_sim = 0.0
            for sw in sel_words:
                if words and sw:
                    union = len(words | sw)
                    sim = len(words & sw) / union if union else 0.0
                    if sim > max_sim:
                        max_sim = sim

            combined = score - (max_sim * 40)
            if combined > best_combined:
                best_combined = combined
                best_idx = i

        line, score = candidates[best_idx]
        selected.append((line, score))
        sel_words.append(_content_words(line))
        candidates.pop(best_idx)

    return selected
