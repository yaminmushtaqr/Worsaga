"""worsaga — Study kit for university LMS systems."""

__version__ = "0.2.0"

from worsaga.client import (
    ALLOWED_FUNCTIONS,
    BLOCKED_PATTERNS,
    MoodleClient,
    MoodleWriteAttemptError,
)
from worsaga.config import DEFAULT_CONFIG_PATH, MoodleConfig, test_connection
from worsaga.deadlines import get_upcoming_deadlines
from worsaga.extraction import (
    FILE_PRIORITY,
    SUPPORTED_EXTENSIONS,
    clean_text,
    extract_file_text,
    is_boilerplate,
    strip_html,
)
from worsaga.materials import (
    MaterialSelectionError,
    download_material,
    extract_materials,
    get_section_materials,
    match_section,
    search_course_content,
    select_material,
)
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
    fallback_bullets,
    format_bullets,
)

__all__ = [
    "__version__",
    "ALLOWED_FUNCTIONS",
    "BLOCKED_PATTERNS",
    "DEFAULT_CONFIG_PATH",
    "FILE_PRIORITY",
    "MaterialSelectionError",
    "MoodleClient",
    "MoodleConfig",
    "MoodleWriteAttemptError",
    "SUPPORTED_EXTENSIONS",
    "download_material",
    "build_deterministic_summary",
    "build_summary",
    "classify_section",
    "clean_text",
    "extract_file_text",
    "extract_materials",
    "fallback_bullets",
    "find_best_section",
    "format_bullets",
    "get_downloadable_files",
    "get_section_materials",
    "get_upcoming_deadlines",
    "is_boilerplate",
    "match_section",
    "score_section_match",
    "search_course_content",
    "select_material",
    "strip_html",
    "summarize_modules",
    "test_connection",
]
