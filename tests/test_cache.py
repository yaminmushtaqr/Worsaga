"""Tests for the local SQLite cache store."""

import json
from pathlib import Path

import pytest

from worsaga.cache import (
    CacheStore,
    default_cache_path,
    read_last_sync_at,
    sanitize_payload,
)


class TestDefaultCachePath:
    def test_env_override(self, monkeypatch, tmp_path):
        override = tmp_path / "custom" / "cache.db"
        monkeypatch.setenv("WORSAGA_CACHE_PATH", str(override))
        assert default_cache_path() == override

    def test_platform_default(self, monkeypatch):
        monkeypatch.delenv("WORSAGA_CACHE_PATH", raising=False)
        path = default_cache_path()
        assert path.name == "cache.db"
        assert "worsaga" in str(path).lower()


class TestReadLastSyncAt:
    SITE = "https://moodle.example.com"

    def test_missing_cache_returns_none_and_creates_nothing(self, tmp_path):
        path = tmp_path / "brand-new" / "cache.db"
        assert read_last_sync_at(self.SITE, path) is None
        assert not path.exists()
        assert not path.parent.exists()

    def test_reads_existing_value_per_site(self, tmp_path):
        path = tmp_path / "cache.db"
        with CacheStore(path) as store:
            store.record_sync_run(self.SITE, 100, 200, {})
            store.record_sync_run(self.SITE, 300, 400, {})
        assert read_last_sync_at(self.SITE, path) == 400
        assert read_last_sync_at("https://other.example.com", path) is None

    def test_read_only_never_modifies_the_file(self, tmp_path):
        path = tmp_path / "cache.db"
        with CacheStore(path) as store:
            store.record_sync_run(self.SITE, 100, 200, {})
        before = path.read_bytes()
        read_last_sync_at(self.SITE, path)
        assert path.read_bytes() == before

    def test_non_database_file_returns_none(self, tmp_path):
        path = tmp_path / "cache.db"
        path.write_text("not a database")
        assert read_last_sync_at(self.SITE, path) is None


class TestSanitizePayload:
    def test_strips_banned_keys_recursively(self):
        payload = {
            "name": "ok",
            "token": "SECRET",
            "wstoken": "SECRET",
            "file_url": "https://moodle.example.com/pluginfile.php?token=SECRET",
            "nested": {
                "fileurl": "https://moodle.example.com/x?wstoken=SECRET",
                "keep": 1,
                "list": [{"token": "SECRET", "fine": True}],
            },
        }
        cleaned = sanitize_payload(payload)
        text = json.dumps(cleaned)
        assert "SECRET" not in text
        assert cleaned["name"] == "ok"
        assert cleaned["nested"]["keep"] == 1
        assert cleaned["nested"]["list"] == [{"fine": True}]

    def test_non_dict_values_pass_through(self):
        assert sanitize_payload([1, "a", None]) == [1, "a", None]
        assert sanitize_payload("text") == "text"

    def test_any_key_containing_token_is_dropped(self):
        cleaned = sanitize_payload({
            "access_token": "SECRET",
            "refreshToken": "SECRET",
            "sesskey": "SECRET",
            "kept": "ok",
        })
        assert cleaned == {"kept": "ok"}

    def test_token_values_inside_strings_are_redacted(self):
        cleaned = sanitize_payload({
            "url": "https://moodle.example/pluginfile.php?wstoken=SECRET_IN_VALUE&x=1",
            "note": "call with token=SECRET_IN_VALUE please",
            "oauth": "https://x.example/cb?access_token=SECRET_IN_VALUE",
        })
        text = json.dumps(cleaned)
        assert "SECRET_IN_VALUE" not in text
        assert "REDACTED" in cleaned["url"]
        assert "&x=1" in cleaned["url"]

    def test_tuples_and_sets_are_walked(self):
        cleaned = sanitize_payload({
            "pair": ("keep", "url?token=SECRETVAL"),
            "bag": {"plain"},
        })
        assert cleaned["pair"] == ["keep", "url?token=REDACTED"]
        assert cleaned["bag"] == ["plain"]


class TestCacheStore:
    def _store(self, tmp_path) -> CacheStore:
        return CacheStore(tmp_path / "cache.db")

    def test_creates_parent_directory(self, tmp_path):
        path = tmp_path / "deep" / "dir" / "cache.db"
        with CacheStore(path):
            pass
        assert path.is_file()

    def test_upsert_and_get_items_roundtrip(self, tmp_path):
        with self._store(tmp_path) as store:
            store.upsert_item(
                "https://site", "files", "k1", "fp1",
                {"file_name": "a.pdf", "file_size": 10}, now=100,
            )
            store.commit()
            items = store.get_items("https://site", "files")
        assert items["k1"]["fingerprint"] == "fp1"
        assert items["k1"]["payload"]["file_name"] == "a.pdf"

    def test_items_isolated_by_site_and_category(self, tmp_path):
        with self._store(tmp_path) as store:
            store.upsert_item("site-a", "files", "k", "fp", {}, now=1)
            store.upsert_item("site-b", "files", "k", "fp", {}, now=1)
            store.upsert_item("site-a", "grades", "k", "fp", {}, now=1)
            store.commit()
            assert len(store.get_items("site-a", "files")) == 1
            assert len(store.get_items("site-b", "files")) == 1
            assert len(store.get_items("site-a", "grades")) == 1
            assert store.get_items("site-c", "files") == {}

    def test_upsert_sanitizes_payload(self, tmp_path):
        path = tmp_path / "cache.db"
        with CacheStore(path) as store:
            store.upsert_item(
                "site", "files", "k1", "fp",
                {"file_name": "a.pdf", "file_url": "https://x?token=SECRETVALUE",
                 "wstoken": "SECRETVALUE"},
                now=100,
            )
            store.record_change(
                "site", "files", "k1",
                {"kind": "new_file", "after": {"fileurl": "x?token=SECRETVALUE"}},
                now=100,
            )
            store.record_sync_run(
                "site", 100, 101, {"token": "SECRETVALUE", "ok": True},
            )
            store.commit()
        raw = Path(path).read_bytes()
        assert b"SECRETVALUE" not in raw
        assert b"file_url" not in raw
        assert b"fileurl" not in raw
        assert b"wstoken" not in raw

    def test_change_recording_and_filters(self, tmp_path):
        with self._store(tmp_path) as store:
            store.record_change(
                "site", "deadlines", "assignment:1",
                {"kind": "new_deadline", "title": "PS3"}, now=100,
            )
            store.record_change(
                "site", "grades", "101:9",
                {"kind": "grade_updated", "title": "PS2"}, now=200,
            )
            store.commit()

            all_changes = store.get_changes("site")
            assert [c["kind"] for c in all_changes] == [
                "grade_updated", "new_deadline",
            ]
            assert all_changes[0]["category"] == "grades"
            assert all_changes[0]["item_key"] == "101:9"

            assert [
                c["kind"] for c in store.get_changes("site", category="deadlines")
            ] == ["new_deadline"]
            assert [
                c["kind"] for c in store.get_changes("site", since_ts=150)
            ] == ["grade_updated"]
            assert store.get_changes("other-site") == []

    def test_embedded_token_value_never_reaches_disk(self, tmp_path):
        path = tmp_path / "cache.db"
        with CacheStore(path) as store:
            store.upsert_item(
                "site", "files", "k1", "fp",
                {"url": "https://moodle.example/x?wstoken=SECRETVALUE"},
                now=100,
            )
        assert b"SECRETVALUE" not in Path(path).read_bytes()

    def test_category_state_roundtrip(self, tmp_path):
        with self._store(tmp_path) as store:
            assert store.get_category_state("site", "grades") is None
            store.set_category_state("site", "grades", now=100, scope=[2, 1])
            state = store.get_category_state("site", "grades")
            assert state == {"last_synced_at": 100, "scope": [1, 2]}
            store.set_category_state("site", "grades", now=200, scope=None)
            state = store.get_category_state("site", "grades")
            assert state == {"last_synced_at": 200, "scope": None}
            assert store.get_category_state("other", "grades") is None

    def test_cache_file_is_owner_only_on_posix(self, tmp_path):
        import os
        import stat

        if os.name == "nt":
            pytest.skip("POSIX permissions are not applicable on Windows")
        path = tmp_path / "cache.db"
        with CacheStore(path):
            pass
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

    def test_last_sync_at(self, tmp_path):
        with self._store(tmp_path) as store:
            assert store.last_sync_at("site") is None
            store.record_sync_run("site", 100, 110, {})
            store.record_sync_run("site", 200, 210, {})
            store.commit()
            assert store.last_sync_at("site") == 210
            assert store.last_sync_at("other") is None
