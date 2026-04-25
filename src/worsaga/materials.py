"""Course content traversal, material discovery, and authenticated download.

Navigates Moodle course sections/modules and extracts structured,
agent-friendly metadata for downloadable files and resources.
Provides selection helpers and an authenticated download path that
keeps tokens out of returned metadata.

All operations are read-only — no Moodle writes.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from worsaga.client import MoodleClient


class MaterialSelectionError(ValueError):
    """Raised when material selection fails (no match or ambiguous).

    Attributes
    ----------
    candidates : list[dict]
        Matching material records (empty if nothing matched).
    """

    def __init__(self, message: str, candidates: list[dict] | None = None):
        super().__init__(message)
        self.candidates = candidates or []


# ── Week / section matching ──────────────────────────────────────

_WEEK_PATTERNS = [
    re.compile(r"\bweek\s*0*(\d+)\b", re.IGNORECASE),
    re.compile(r"\bwk\s*0*(\d+)\b", re.IGNORECASE),
    re.compile(r"\btopic\s*0*(\d+)\b", re.IGNORECASE),
    re.compile(r"\bsession\s*0*(\d+)\b", re.IGNORECASE),
    re.compile(r"\blecture\s*0*(\d+)\b", re.IGNORECASE),
]


def extract_week_number(section_name: str) -> int | None:
    """Extract a week/topic number from a section name, or None."""
    for pattern in _WEEK_PATTERNS:
        m = pattern.search(section_name)
        if m:
            return int(m.group(1))
    return None


def match_section(section: dict, query: int | str) -> bool:
    """Test whether *section* matches a week-like query.

    Parameters
    ----------
    section : dict
        A Moodle section dict (from ``core_course_get_contents``).
    query : int | str
        * ``int`` or numeric ``str`` — matches a week/topic number extracted
          from the section name.
        * non-numeric ``str`` — case-insensitive substring match on the
          section name.

    Notes
    -----
    For numeric queries we intentionally do *not* match Moodle's raw
    ``section`` index. In real courses those indices often represent layout
    order rather than teaching week, which can make ``week=1`` pull unrelated
    sections like "Using Generative AI" simply because they sit in slot 1.
    """
    name = section.get("name", "")

    if isinstance(query, str):
        stripped = query.strip()
        if not stripped:
            return False
        try:
            query = int(stripped)
        except ValueError:
            return stripped.lower() in name.lower()

    extracted = extract_week_number(name)
    return extracted is not None and extracted == query


# ── Material record builder ──────────────────────────────────────


def _build_material(
    course_id: int,
    section: dict,
    module: dict,
    file_info: dict | None = None,
    *,
    base_url: str = "",
) -> dict:
    """Build one structured material record.

    If *file_info* is provided the record describes a specific file
    inside a module's ``contents`` array.  Otherwise it describes the
    module itself (e.g. a URL resource with no file list).

    When *base_url* is given, a ``view_url`` key is added pointing to
    the human-readable Moodle page for the module.
    """
    module_id = module.get("id", 0)

    if file_info:
        file_name = file_info.get("filename", "")
        file_url = file_info.get("fileurl", "")
        file_size = file_info.get("filesize", 0)
        mime_type = file_info.get("mimetype", "")
        time_modified = file_info.get("timemodified", 0)
        dedupe_key = f"{module_id}:{file_name}:{_dedupe_location_key(file_info)}"
    else:
        file_name = ""
        file_url = module.get("url", "")
        file_size = 0
        mime_type = ""
        time_modified = module.get("added", 0)
        dedupe_key = f"{module_id}::{_token_free_url_key(file_url)}"

    record = {
        "course_id": course_id,
        "section_id": section.get("id", 0),
        "section_name": section.get("name", ""),
        "section_num": section.get("section", 0),
        "module_id": module_id,
        "module_name": module.get("name", ""),
        "module_type": module.get("modname", ""),
        "file_name": file_name,
        "file_url": file_url,
        "file_size": file_size,
        "mime_type": mime_type,
        "time_modified": time_modified,
        "dedupe_key": dedupe_key,
    }

    if base_url:
        modname = module.get("modname", "")
        record["view_url"] = (
            f"{base_url}/mod/{modname}/view.php?id={module_id}"
        )

    return record


def _token_free_url_key(url: str) -> str:
    """Return a stable URL-derived key without Moodle token parameters."""
    parsed = urllib.parse.urlparse(str(url or ""))
    query = urllib.parse.urlencode([
        (key, value)
        for key, value in urllib.parse.parse_qsl(
            parsed.query, keep_blank_values=True,
        )
        if key.lower() not in {"token", "wstoken"}
    ])
    return urllib.parse.urlunparse(
        ("", "", parsed.path, "", query, "")
    ) or str(url or "")


def _dedupe_location_key(file_info: dict) -> str:
    """Return the file location component used for material dedupe."""
    file_url = file_info.get("fileurl", "")
    filepath = file_info.get("filepath", "")
    return "|".join(
        part for part in (str(filepath or ""), _token_free_url_key(file_url))
        if part
    )


# ── Extraction helpers ───────────────────────────────────────────


def extract_materials(
    sections: list[dict],
    course_id: int,
    *,
    base_url: str = "",
    dedupe: bool = True,
) -> list[dict]:
    """Extract material records from Moodle course sections.

    Parameters
    ----------
    sections : list[dict]
        Raw sections from ``core_course_get_contents``.
    course_id : int
        Included in every record for cross-course context.
    base_url : str
        Moodle site root (e.g. ``https://moodle.example.com``).
        When provided, each record includes a ``view_url`` pointing
        to the human-readable module page.
    dedupe : bool
        Suppress duplicates sharing the same ``dedupe_key``.

    Returns
    -------
    list[dict]
        Material records in section order.
    """
    materials: list[dict] = []
    seen: set[str] = set()

    for section in sections:
        for module in section.get("modules", []):
            contents = module.get("contents", [])

            if contents:
                for file_info in contents:
                    # Moodle contents can be type "file", "url", or "content".
                    # We want files and URL entries, not raw HTML bodies.
                    if file_info.get("type", "") not in ("file", "url"):
                        continue

                    record = _build_material(
                        course_id, section, module, file_info,
                        base_url=base_url,
                    )

                    if dedupe and record["dedupe_key"] in seen:
                        continue
                    seen.add(record["dedupe_key"])
                    materials.append(record)

            elif module.get("modname", "") in ("url", "page"):
                # Module has no contents array but is itself a link or page.
                record = _build_material(
                    course_id, section, module,
                    base_url=base_url,
                )

                if dedupe and record["dedupe_key"] in seen:
                    continue
                seen.add(record["dedupe_key"])
                materials.append(record)

    return materials


def get_section_materials(
    sections: list[dict],
    course_id: int,
    week: int | str,
    *,
    base_url: str = "",
    dedupe: bool = True,
) -> list[dict]:
    """Get materials for sections matching *week*.

    Combines :func:`match_section` filtering with :func:`extract_materials`.
    """
    matching = [s for s in sections if match_section(s, week)]
    return extract_materials(matching, course_id, base_url=base_url, dedupe=dedupe)


def search_course_content(
    sections: list[dict],
    query: str,
) -> list[dict]:
    """Search section and module names for *query* (case-insensitive).

    Returns lightweight module-level dicts with section context.
    Useful for agents that need to locate a topic without knowing the
    week number.
    """
    needle = query.lower()
    results: list[dict] = []
    seen_ids: set[int] = set()

    for section in sections:
        sec_name = section.get("name", "")
        sec_hit = needle in sec_name.lower()

        for module in section.get("modules", []):
            mod_id = module.get("id", 0)
            mod_name = module.get("name", "")

            if sec_hit or needle in mod_name.lower():
                if mod_id not in seen_ids:
                    seen_ids.add(mod_id)
                    results.append({
                        "section_name": sec_name,
                        "section_num": section.get("section", 0),
                        "module_id": mod_id,
                        "module_name": mod_name,
                        "module_type": module.get("modname", ""),
                        "module_url": module.get("url", ""),
                    })

    return results


# ── Selection & download ────────────────────────────────────────


def _sanitize_filename(name: str) -> str:
    """Remove or replace characters unsafe for filenames."""
    # Keep alphanumerics, dots, hyphens, underscores
    return re.sub(r"[^\w.\-]", "_", name)


def _available_path(path: Path) -> Path:
    """Return a non-existing path by adding a numeric suffix if needed."""
    if not path.exists():
        return path

    stem = path.stem or "download"
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def select_material(
    materials: list[dict],
    *,
    match: str | None = None,
    index: int | None = None,
) -> dict:
    """Select a single material from a list.

    Parameters
    ----------
    materials : list[dict]
        Material records from :func:`extract_materials` or
        :func:`get_section_materials`.
    match : str, optional
        Case-insensitive substring filter applied to ``file_name`` and
        ``module_name``.  Narrows the candidates before selection.
    index : int, optional
        Zero-based index into the (possibly filtered) list.  When
        provided, selects exactly that entry.

    Returns
    -------
    dict
        A single material record.

    Raises
    ------
    MaterialSelectionError
        If no materials match, or if multiple match without an *index*
        to disambiguate.  The exception's ``.candidates`` attribute
        holds the matching records so callers can present them.
    """
    if not materials:
        raise MaterialSelectionError("No materials available.", [])

    candidates = materials
    if match:
        needle = match.lower()
        candidates = [
            m for m in candidates
            if needle in m.get("file_name", "").lower()
            or needle in m.get("module_name", "").lower()
        ]

    if not candidates:
        raise MaterialSelectionError(
            f"No materials matching '{match}'.",
            [],
        )

    if index is not None:
        if index < 0 or index >= len(candidates):
            raise MaterialSelectionError(
                f"Index {index} out of range (0–{len(candidates) - 1}).",
                candidates,
            )
        return candidates[index]

    if len(candidates) == 1:
        return candidates[0]

    # Multiple matches — ambiguous
    raise MaterialSelectionError(
        f"{len(candidates)} materials match. Use --index to select one, "
        f"or --match to narrow results.",
        candidates,
    )


def candidate_summary(material: dict, index: int) -> dict:
    """Return a token-free summary of a material for candidate lists.

    *index* is the caller's position for the material in its candidate
    list (used so UI/JSON output can reference a specific entry by
    number). No token-bearing fields (``file_url`` in particular) are
    included in the returned dict.
    """
    return {
        "index": index,
        "file_name": material.get("file_name", ""),
        "module_name": material.get("module_name", ""),
        "section_name": material.get("section_name", ""),
        "mime_type": material.get("mime_type", ""),
        "file_size": material.get("file_size", 0),
        "view_url": material.get("view_url", ""),
    }


def download_material(
    client: "MoodleClient",
    material: dict,
    *,
    output_dir: str | Path | None = None,
) -> dict:
    """Download a material file using the authenticated client.

    Uses :meth:`MoodleClient.download_file` so the token is appended
    internally and never exposed in the returned metadata.

    Parameters
    ----------
    client : MoodleClient
        Authenticated client instance.
    material : dict
        A material record (from :func:`extract_materials` etc.).
    output_dir : str or Path, optional
        Directory to save the file.  Defaults to the current working
        directory.

    Returns
    -------
    dict
        Metadata about the downloaded file — ``local_path``,
        ``file_name``, ``module_name``, ``section_name``,
        ``mime_type``, ``file_size``, ``bytes_written``, and
        ``view_url`` (if available).  **No tokens or authenticated
        URLs are included.**

    Raises
    ------
    RuntimeError
        If the download fails (network error, empty response, etc.).
    """
    file_url = material.get("file_url", "")
    if not file_url:
        raise RuntimeError("Material has no file_url — cannot download.")

    file_name = material.get("file_name", "")
    if not file_name:
        # URL-type modules may lack a filename
        file_name = _sanitize_filename(material.get("module_name", "download"))

    data = client.download_file(file_url)
    if data is None:
        raise RuntimeError(f"Download failed for '{file_name}'.")

    dest_dir = Path(output_dir) if output_dir else Path.cwd()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = _available_path(dest_dir / _sanitize_filename(file_name))

    dest_path.write_bytes(data)

    result = {
        "local_path": str(dest_path),
        "file_name": file_name,
        "module_name": material.get("module_name", ""),
        "section_name": material.get("section_name", ""),
        "mime_type": material.get("mime_type", ""),
        "file_size": material.get("file_size", 0),
        "bytes_written": len(data),
    }
    if material.get("view_url"):
        result["view_url"] = material["view_url"]
    return result
