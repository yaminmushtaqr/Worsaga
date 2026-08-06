"""Tests for the local full-text search index (textindex module)."""

import json
import sqlite3

import pytest

from worsaga.cli import main
from worsaga.client import MoodleWriteAttemptError
from worsaga.demo import DemoMoodleClient
from worsaga.textindex import (
    TextIndexStore,
    build_text_index,
    default_index_path,
    fts_match_expression,
    material_fingerprint,
    search_text_index,
)
from worsaga.redact import REDACTED

SITE = "https://moodle.example.com"


class _MutableClient:
    """Minimal offline client whose course files can change between builds."""

    base_url = SITE

    def __init__(self):
        self.files = {
            "alpha-notes.txt": b"The alpha topic covers gradient descent.",
            "beta-notes.txt": b"The beta topic covers convex duality.",
        }
        self.module_ids = {"alpha-notes.txt": 100, "beta-notes.txt": 101}
        self.fail_contents = False
        self.raise_write_attempt = False

    def get_courses(self):
        return [{"id": 1, "shortname": "T101", "fullname": "Testing 101"}]

    def get_course_contents(self, course_id):
        if self.raise_write_attempt:
            raise MoodleWriteAttemptError("blocked write")
        if self.fail_contents:
            raise OSError("network down")
        modules = [
            {
                "id": self.module_ids[name],
                "name": name,
                "modname": "resource",
                "contents": [{
                    "type": "file",
                    "filename": name,
                    "filepath": "/",
                    "fileurl": f"{self.base_url}/pluginfile.php/{name}",
                    "filesize": len(data),
                    "timemodified": 1000,
                }],
            }
            for name, data in sorted(self.files.items())
        ]
        return [{"id": 10, "section": 1, "name": "Week 1", "modules": modules}]

    def download_file(self, fileurl):
        return self.files[fileurl.rsplit("/", 1)[-1]]


@pytest.fixture
def index_path(tmp_path):
    return tmp_path / "search.db"


@pytest.fixture
def env_index(index_path, monkeypatch):
    """Point WORSAGA_INDEX_PATH at a temp DB for CLI-level tests."""
    monkeypatch.setenv("WORSAGA_INDEX_PATH", str(index_path))
    return index_path


def _econ_course_id():
    for course in DemoMoodleClient().get_courses():
        if course.get("shortname") == "ECON101":
            return course["id"]
    raise AssertionError("demo dataset must include ECON101")


class TestMatchExpression:
    def test_terms_are_quoted(self):
        assert fts_match_expression("supply demand") == '"supply" "demand"'

    def test_operator_characters_are_neutralized(self):
        expr = fts_match_expression('a* NOT (b:c) -d')
        # Everything survives as quoted literals — no bare operators.
        assert expr == '"a*" "NOT" "(b:c)" "-d"'

    def test_embedded_quotes_are_doubled(self):
        assert fts_match_expression('say "hi"') == '"say" """hi"""'

    def test_empty_query(self):
        assert fts_match_expression("   ") == ""


class TestDefaultIndexPath:
    def test_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WORSAGA_INDEX_PATH", str(tmp_path / "x.db"))
        assert default_index_path() == tmp_path / "x.db"

    def test_default_is_search_db(self, monkeypatch):
        monkeypatch.delenv("WORSAGA_INDEX_PATH", raising=False)
        assert default_index_path().name == "search.db"


class TestTextIndexStore:
    def test_symlinked_index_path_is_refused(self, tmp_path):
        """SQLite would follow the link and write indexed course text
        wherever it points; the store refuses to open it at all."""
        from worsaga.secureio import SecureWriteError

        real = tmp_path / "elsewhere.db"
        real.write_bytes(b"")
        link = tmp_path / "search.db"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            pytest.skip("this environment cannot create symbolic links")
        with pytest.raises(SecureWriteError) as exc:
            TextIndexStore(link)
        assert "symbolic link" in str(exc.value)

    def _put(self, store, *, doc_key="1:doc", pages=None, meta=None,
             fingerprint="fp1"):
        store.upsert_document(
            SITE, doc_key, fingerprint,
            {
                "course_id": 1,
                "course_shortname": "ECON101",
                "section_name": "Week 3",
                "module_name": "Slides",
                "file_name": "week3.pdf",
                "file_type": "pdf",
                "view_url": f"{SITE}/mod/resource/view.php?id=10",
                **(meta or {}),
            },
            pages if pages is not None else [
                (1, "The supply curve shows quantity offered."),
                (2, "Elasticity measures responsiveness to price."),
            ],
        )

    def test_search_round_trip(self, index_path):
        with TextIndexStore(index_path) as store:
            self._put(store)
            hits = store.search(SITE, "elasticity")
        assert len(hits) == 1
        hit = hits[0]
        assert hit["page"] == 2
        assert hit["file_name"] == "week3.pdf"
        assert hit["course_shortname"] == "ECON101"
        assert "[Elasticity]" in hit["snippet"]
        # bm25 idf is 0 for a term in half a two-page corpus; the score
        # only has to be non-negative and ordered, not positive.
        assert hit["score"] >= 0

    def test_stemming_matches_word_forms(self, index_path):
        with TextIndexStore(index_path) as store:
            self._put(store)
            assert store.search(SITE, "measuring")  # porter stem of measures

    def test_site_scoping(self, index_path):
        with TextIndexStore(index_path) as store:
            self._put(store)
            assert store.search("https://other.example.com", "supply") == []

    def test_course_filters(self, index_path):
        with TextIndexStore(index_path) as store:
            self._put(store)
            assert store.search(SITE, "supply", course_id=1)
            assert store.search(SITE, "supply", course_id=2) == []
            assert store.search(SITE, "supply", course_shortname="econ101")
            assert store.search(SITE, "supply", course_shortname="CS210") == []

    def test_reindex_replaces_pages(self, index_path):
        with TextIndexStore(index_path) as store:
            self._put(store)
            self._put(store, pages=[(1, "Entirely new content only.")],
                      fingerprint="fp2")
            assert store.search(SITE, "supply") == []
            assert store.search(SITE, "entirely")
            assert store.get_fingerprint(SITE, "1:doc") == "fp2"

    def test_empty_pages_are_skipped(self, index_path):
        with TextIndexStore(index_path) as store:
            self._put(store, pages=[(1, "  "), (2, "real text")])
            stats = store.stats(SITE)
        assert stats["pages"] == 1

    def test_operator_heavy_query_does_not_crash(self, index_path):
        with TextIndexStore(index_path) as store:
            self._put(store)
            for query in ('"unbalanced', "a NEAR b", "col:val", "(", "-x *"):
                store.search(SITE, query)  # must not raise

    def test_token_values_redacted_in_stored_text(self, index_path):
        with TextIndexStore(index_path) as store:
            self._put(store, pages=[
                (1, "See https://x.example/f.pdf?token=SECRETVALUE now"),
            ], meta={"view_url": f"{SITE}/view.php?wstoken=SECRETVALUE"})
        conn = sqlite3.connect(index_path)
        try:
            page_text = conn.execute("SELECT text FROM pages").fetchone()[0]
            view_url = conn.execute(
                "SELECT view_url FROM documents"
            ).fetchone()[0]
        finally:
            conn.close()
        assert "SECRETVALUE" not in page_text
        assert f"token={REDACTED}" in page_text
        assert "SECRETVALUE" not in view_url

    def test_stats(self, index_path):
        with TextIndexStore(index_path) as store:
            self._put(store)
            self._put(store, doc_key="1:other")
            stats = store.stats(SITE)
        assert stats["documents"] == 2
        assert stats["pages"] == 4
        assert stats["courses"] == 1
        assert stats["last_indexed_at"] is not None

    def test_stats_empty_site(self, index_path):
        with TextIndexStore(index_path) as store:
            stats = store.stats(SITE)
        assert stats == {
            "documents": 0, "pages": 0, "courses": 0,
            "last_indexed_at": None,
        }


class TestBuildTextIndex:
    def test_build_and_search_demo(self, index_path):
        client = DemoMoodleClient()
        result = build_text_index(client, index_path=index_path)
        assert result["files_indexed"] > 0
        assert result["pages_indexed"] > 0
        assert not result["budget_exhausted"]

        found = search_text_index(
            client.base_url, "elasticity", index_path=index_path,
        )
        assert found["hits"]
        assert any(
            "elasticity" in hit["snippet"].lower() for hit in found["hits"]
        )

    def test_rerun_skips_unchanged(self, index_path):
        client = DemoMoodleClient()
        build_text_index(client, index_path=index_path)
        again = build_text_index(client, index_path=index_path)
        assert again["files_indexed"] == 0
        assert again["files_unchanged"] > 0

    def test_budget_exhaustion_resumes(self, index_path):
        client = DemoMoodleClient()
        first = build_text_index(client, index_path=index_path, max_files=1)
        assert first["files_indexed"] == 1
        assert first["budget_exhausted"]
        assert any("budget" in w for w in first["warnings"])
        second = build_text_index(client, index_path=index_path)
        # The first run's file is skipped as unchanged; the rest proceed.
        assert second["files_unchanged"] >= 1
        assert second["files_indexed"] > 0

    def test_course_scope(self, index_path):
        client = DemoMoodleClient()
        result = build_text_index(
            client, course_id=_econ_course_id(), index_path=index_path,
        )
        assert [c["course_shortname"] for c in result["courses"]] == ["ECON101"]

    def test_week_scope(self, index_path):
        client = DemoMoodleClient()
        result = build_text_index(
            client, course_id=_econ_course_id(), week=3,
            index_path=index_path,
        )
        assert result["files_indexed"] > 0
        found = search_text_index(
            client.base_url, "opportunity cost", index_path=index_path,
        )
        assert found["hits"] == []  # week 1 content not indexed

    def test_unknown_course_raises(self, index_path):
        with pytest.raises(ValueError):
            build_text_index(
                DemoMoodleClient(), course_id=999999, index_path=index_path,
            )

    def test_no_token_leak_in_results(self, index_path):
        client = DemoMoodleClient()
        result = build_text_index(client, index_path=index_path)
        found = search_text_index(
            client.base_url, "elasticity", index_path=index_path,
        )
        for payload in (result, found):
            text = json.dumps(payload).lower()
            assert "file_url" not in text
            assert "wstoken" not in text

    def test_fingerprint_changes_with_metadata(self):
        base = {"dedupe_key": "k", "file_name": "a.pdf",
                "file_size": 10, "time_modified": 100}
        assert material_fingerprint(base) != material_fingerprint(
            {**base, "time_modified": 200}
        )


class TestDeletionReconciliation:
    def test_deleted_file_is_removed_from_index(self, index_path):
        client = _MutableClient()
        build_text_index(client, index_path=index_path)
        assert search_text_index(SITE, "alpha", index_path=index_path)["hits"]

        del client.files["alpha-notes.txt"]
        result = build_text_index(client, index_path=index_path)

        assert result["files_removed"] == 1
        assert search_text_index(SITE, "alpha", index_path=index_path)["hits"] == []
        assert search_text_index(SITE, "beta", index_path=index_path)["hits"]
        assert result["index"]["documents"] == 1

    def test_renamed_file_is_replaced_not_duplicated(self, index_path):
        client = _MutableClient()
        build_text_index(client, index_path=index_path)

        data = client.files.pop("alpha-notes.txt")
        client.files["alpha-renamed.txt"] = data
        client.module_ids["alpha-renamed.txt"] = 100
        result = build_text_index(client, index_path=index_path)

        assert result["files_removed"] == 1
        assert result["files_indexed"] == 1
        assert result["index"]["documents"] == 2

    def test_week_scoped_build_never_removes(self, index_path):
        client = _MutableClient()
        build_text_index(client, index_path=index_path)

        del client.files["alpha-notes.txt"]
        result = build_text_index(client, week=1, index_path=index_path)

        assert result["files_removed"] == 0
        assert result["index"]["documents"] == 2
        assert search_text_index(SITE, "alpha", index_path=index_path)["hits"]

    def test_failed_contents_fetch_never_removes(self, index_path):
        client = _MutableClient()
        build_text_index(client, index_path=index_path)

        client.fail_contents = True
        result = build_text_index(client, index_path=index_path)

        assert result["files_removed"] == 0
        assert result["index"]["documents"] == 2
        assert any("contents fetch failed" in w for w in result["warnings"])

    def test_budget_exhaustion_never_removes_unfetched(self, index_path):
        client = _MutableClient()
        build_text_index(client, index_path=index_path)

        # Touch both files so both need re-fetching, then allow only one.
        for name in list(client.files):
            client.files[name] += b" updated"
        result = build_text_index(client, index_path=index_path, max_files=1)

        assert result["budget_exhausted"]
        assert result["files_removed"] == 0
        assert result["index"]["documents"] == 2


class TestSafetyInvariants:
    def test_write_attempt_error_propagates(self, index_path):
        client = _MutableClient()
        client.raise_write_attempt = True
        with pytest.raises(MoodleWriteAttemptError):
            build_text_index(client, index_path=index_path)

    def test_build_response_is_sanitized(self, index_path):
        client = _MutableClient()
        courses = [{"id": 1, "shortname": "T101 token=SECRET1234",
                    "fullname": "Testing"}]
        client.get_courses = lambda: courses
        result = build_text_index(client, index_path=index_path)
        text = json.dumps(result)
        assert "SECRET1234" not in text
        assert f"token={REDACTED}" in text


class TestCliSurface:
    def test_index_then_search_text(self, env_index, capsys):
        main(["--demo", "index", "-q"])
        out = capsys.readouterr().out
        assert "Indexed" in out

        main(["--demo", "search-text", "elasticity"])
        out = capsys.readouterr().out
        assert "match(es)" in out
        assert "ECON101" in out

    def test_search_text_json(self, env_index, capsys):
        main(["--demo", "index", "-q"])
        capsys.readouterr()
        main(["--demo", "--json", "search-text", "elasticity", "--limit", "5"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["hits"]
        assert len(payload["hits"]) <= 5
        text = json.dumps(payload).lower()
        assert "file_url" not in text
        assert "wstoken" not in text

    def test_search_text_course_filter(self, env_index, capsys):
        main(["--demo", "index", "-q"])
        capsys.readouterr()
        main(["--demo", "--json", "search-text", "elasticity",
              "--course", "CS210"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["hits"] == []

    def test_search_text_before_index(self, env_index, capsys):
        main(["--demo", "search-text", "anything"])
        out = capsys.readouterr().out
        assert "worsaga index" in out

    def test_index_json(self, env_index, capsys):
        main(["--demo", "--json", "index"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["files_indexed"] > 0
        assert payload["index"]["documents"] == payload["files_indexed"]


class TestMcpSurface:
    def test_build_search_and_export(self, env_index):
        from unittest.mock import patch

        from worsaga import mcp_server

        client = DemoMoodleClient()
        with patch.object(mcp_server, "_get_client", return_value=client):
            built = mcp_server.build_search_index()
            assert built["files_indexed"] > 0

            found = mcp_server.search_text("elasticity", limit=3)
            assert found["hits"]
            assert len(found["hits"]) <= 3
            text = json.dumps(found).lower()
            assert "file_url" not in text
            assert "wstoken" not in text

    def test_search_text_unknown_index(self, env_index):
        from unittest.mock import patch

        from worsaga import mcp_server

        client = DemoMoodleClient()
        with patch.object(mcp_server, "_get_client", return_value=client):
            found = mcp_server.search_text("nothing indexed yet")
            assert found["hits"] == []
            assert found["index"]["documents"] == 0

    def test_numeric_course_filter_keeps_search_text_offline(self, env_index):
        """search_text documents "no network" -- a numeric filter must honour it.

        The index is built enrolment-scoped, so filtering it by id cannot
        widen what it holds; resolving the id against the course list would
        turn a local query into a Moodle round-trip.
        """
        from unittest.mock import patch

        from worsaga import mcp_server

        course_id = _econ_course_id()
        with patch.object(mcp_server, "_get_client", return_value=DemoMoodleClient()):
            mcp_server.build_search_index()

        class _OfflineClient(DemoMoodleClient):
            base_url = DemoMoodleClient.base_url

            def get_courses(self):
                raise AssertionError("search_text must not contact Moodle")

        with patch.object(mcp_server, "_get_client", return_value=_OfflineClient()):
            found = mcp_server.search_text("elasticity", course_id=course_id)
            assert found["hits"]
            # An id that was never indexed is simply zero local hits.
            missing = mcp_server.search_text("elasticity", course_id=999999)
            assert missing["hits"] == []
