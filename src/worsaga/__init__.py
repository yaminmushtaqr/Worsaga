"""worsaga — Open-source, local-first study toolkit for Moodle.

Read-only against Moodle; writes only local files on the user's own
machine. See "Responsible use" in README.md.
"""

__version__ = "0.8.1"

from worsaga.assignments import get_assignment_status, get_assignments, normalize_assignment
from worsaga.calendar import get_calendar_events, normalize_calendar_events
from worsaga.client import (
    ALLOWED_FUNCTION_POLICIES,
    ALLOWED_FUNCTIONS,
    BLOCKED_PATTERNS,
    IDENTITY_PARAMS,
    MAX_DOWNLOAD_BYTES,
    SELF_SCOPED_PARAMS,
    OTHERS_PERSONAL_FUNCTIONS,
    DownloadError,
    MoodleClient,
    MoodleRateLimitedError,
    MoodleResponseError,
    MoodleScopeError,
    MoodleServiceDisabledError,
    MoodleWriteAttemptError,
)
from worsaga.config import (
    DEFAULT_CONFIG_PATH,
    MoodleConfig,
    default_downloads_dir,
    test_connection,
)
from worsaga.deadlines import get_upcoming_deadlines
from worsaga.digest import get_digest
from worsaga.extraction import (
    FILE_PRIORITY,
    SUPPORTED_EXTENSIONS,
    clean_text,
    extract_file_structured,
    extract_file_text,
    is_boilerplate,
    strip_html,
)
from worsaga.forums import (
    get_course_forums,
    get_forum_discussions,
    get_latest_updates,
    normalize_forum_discussions,
    normalize_forums,
)
from worsaga.grades import get_grade_summary, get_grades, normalize_grade_items
from worsaga.materials import (
    MaterialSelectionError,
    download_material,
    extract_material_content,
    extract_materials,
    get_section_materials,
    match_section,
    search_course_content,
    select_material,
    strip_file_urls,
)
from worsaga.messages import (
    get_messages,
    get_notifications,
    normalize_messages,
    normalize_notifications,
)
from worsaga.sections import (
    WeekNotFoundError,
    classify_section,
    find_best_section,
    get_downloadable_files,
    score_section_match,
    summarize_modules,
)
from worsaga.autosync import (
    autosync_status,
    install_autosync,
    remove_autosync,
)
from worsaga.cache import CacheStore, default_cache_path, read_last_sync_at
from worsaga.principal import PrincipalMismatchError
from worsaga.notify import notification_backend, send_notification
from worsaga.studypack import build_study_pack, write_study_pack
from worsaga.watch import run_watch
from worsaga.textindex import (
    TextIndexError,
    TextIndexStore,
    build_text_index,
    default_index_path,
    search_text_index,
)
from worsaga.sync import (
    SYNC_CATEGORIES,
    SYNC_LOOKAHEAD_DAYS,
    UNATTENDED_SYNC_CATEGORIES,
    get_recent_changes,
    parse_sync_categories,
    resolve_sync_categories,
    run_sync,
    sync_outcome,
)
from worsaga.notices import (
    THIRD_PARTY_NOTICE,
    announce_third_party_collection,
)
from worsaga.redact import redact_payload, redact_text
from worsaga.synclock import SyncLock
from worsaga.syncstate import FAILURE_CLASSES, OUTCOMES
from worsaga.summaries import (
    build_deterministic_summary,
    build_summary,
    fallback_bullets,
    format_bullets,
)

__all__ = [
    "__version__",
    "ALLOWED_FUNCTIONS",
    "ALLOWED_FUNCTION_POLICIES",
    "BLOCKED_PATTERNS",
    "DEFAULT_CONFIG_PATH",
    "DownloadError",
    "FAILURE_CLASSES",
    "FILE_PRIORITY",
    "IDENTITY_PARAMS",
    "MAX_DOWNLOAD_BYTES",
    "MaterialSelectionError",
    "MoodleClient",
    "MoodleConfig",
    "MoodleRateLimitedError",
    "MoodleResponseError",
    "MoodleScopeError",
    "MoodleServiceDisabledError",
    "MoodleWriteAttemptError",
    "OUTCOMES",
    "PrincipalMismatchError",
    "SELF_SCOPED_PARAMS",
    "SUPPORTED_EXTENSIONS",
    "SYNC_CATEGORIES",
    "SYNC_LOOKAHEAD_DAYS",
    "SyncLock",
    "OTHERS_PERSONAL_FUNCTIONS",
    "THIRD_PARTY_NOTICE",
    "UNATTENDED_SYNC_CATEGORIES",
    "CacheStore",
    "TextIndexError",
    "TextIndexStore",
    "WeekNotFoundError",
    "announce_third_party_collection",
    "autosync_status",
    "install_autosync",
    "notification_backend",
    "remove_autosync",
    "run_watch",
    "send_notification",
    "build_study_pack",
    "build_text_index",
    "default_cache_path",
    "default_index_path",
    "get_recent_changes",
    "read_last_sync_at",
    "run_sync",
    "search_text_index",
    "write_study_pack",
    "download_material",
    "build_deterministic_summary",
    "build_summary",
    "classify_section",
    "clean_text",
    "default_downloads_dir",
    "extract_file_structured",
    "extract_file_text",
    "extract_material_content",
    "extract_materials",
    "fallback_bullets",
    "find_best_section",
    "format_bullets",
    "get_assignment_status",
    "get_assignments",
    "get_calendar_events",
    "get_course_forums",
    "get_digest",
    "get_downloadable_files",
    "get_forum_discussions",
    "get_grade_summary",
    "get_grades",
    "get_latest_updates",
    "get_messages",
    "get_notifications",
    "get_section_materials",
    "get_upcoming_deadlines",
    "is_boilerplate",
    "match_section",
    "normalize_assignment",
    "normalize_calendar_events",
    "normalize_forum_discussions",
    "normalize_forums",
    "normalize_grade_items",
    "normalize_messages",
    "normalize_notifications",
    "parse_sync_categories",
    "redact_payload",
    "redact_text",
    "resolve_sync_categories",
    "score_section_match",
    "search_course_content",
    "select_material",
    "strip_file_urls",
    "strip_html",
    "summarize_modules",
    "sync_outcome",
    "test_connection",
]
