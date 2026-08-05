"""Tests for binding local stores to the authenticated Moodle account.

Covers the shared reconciliation rules and their three call sites: the
sync cache, the full-text index, and the auto-sync record.
"""

import json
import logging
import subprocess
from unittest.mock import patch

import pytest

from worsaga import autosync
from worsaga.autosync import install_autosync
from worsaga.cache import CacheStore
from worsaga.demo import DEMO_USERID, DemoMoodleClient
from worsaga.principal import (
    PrincipalMismatchError,
    assert_principal,
    bind_principal,
    known_principal,
    principal_meta_key,
)
from worsaga.sync import get_recent_changes, run_sync
from worsaga.textindex import (
    TextIndexStore,
    build_text_index,
    search_text_index,
)

SITE = "https://moodle.example.edu"
OTHER_SITE = "https://moodle.other.example"

#: Captured before any test patches it, so one test can put the real
#: implementation back and exercise it.
_REAL_LOCAL_BINDING = autosync._local_binding


class _Principal:
    """Minimal client stand-in with a fixed verified identity."""

    def __init__(self, userid, base_url=SITE):
        self.userid = userid
        self.verified_userid = userid
        self.base_url = base_url


class _LateVerifyingClient(DemoMoodleClient):
    """Identity unknown until the first successful fetch.

    This is what the real client looks like when ``site_info`` fails once
    and a later call succeeds: reading the principal at the top of a run
    would see ``None``, reading it at write time sees the real id.
    """

    def __init__(self):
        super().__init__()
        self._verified = False

    @property
    def verified_userid(self):
        return DEMO_USERID if self._verified else None

    def get_courses(self):
        self._verified = True
        return super().get_courses()


class _NeverVerifiesClient(DemoMoodleClient):
    """Nothing was ever fetched, so no identity was ever established."""

    verified_userid = None


class TestKnownPrincipal:
    def test_returns_an_already_verified_id(self):
        assert known_principal(_Principal(41)) == 41

    def test_none_client(self):
        assert known_principal(None) is None

    def test_stand_in_without_an_identity(self):
        class _Bare:
            base_url = SITE

        assert known_principal(_Bare()) is None

    def test_unusable_id_is_not_a_principal(self):
        assert known_principal(_Principal(0)) is None

    def test_makes_no_request_when_unknown(self):
        class _Unverified:
            verified_userid = None

            @property
            def userid(self):
                raise AssertionError("known_principal must not fetch")

        assert known_principal(_Unverified()) is None

    def test_demo_client_always_knows(self):
        assert known_principal(DemoMoodleClient()) == DEMO_USERID


class TestReconciliationRules:
    def _bind(self, stored, principal, **kwargs):
        return bind_principal(
            stored=stored, principal=principal, site=SITE,
            store_label="sync cache", store_path="/tmp/cache.db",
            remedy="Delete it.", **kwargs,
        )

    def test_unverified_principal_changes_nothing(self):
        assert self._bind(41, None) is None

    def test_unstamped_store_is_adopted(self):
        assert self._bind(None, 41) == 41

    def test_matching_stamp_is_left_alone(self):
        assert self._bind(41, 41) is None

    def test_mismatch_names_both_ids_and_the_remedy(self):
        with pytest.raises(PrincipalMismatchError) as exc:
            self._bind(41, 77)
        message = str(exc.value)
        assert "41" in message and "77" in message
        assert SITE in message
        assert "/tmp/cache.db" in message
        assert "Delete it." in message

    def test_empty_store_adoption_is_silent(self, caplog):
        with caplog.at_level(logging.WARNING):
            self._bind(None, 41, holds_data=False)
        assert caplog.records == []

    def test_legacy_store_adoption_is_announced(self, caplog):
        with caplog.at_level(logging.WARNING):
            self._bind(None, 41, holds_data=True)
        assert len(caplog.records) == 1
        assert "41" in caplog.records[0].getMessage()

    def test_read_check_ignores_an_unstamped_store(self):
        assert_principal(
            stored=None, principal=41, site=SITE, store_label="search index",
            store_path="/tmp/search.db", remedy="Delete it.",
        )

    def test_read_check_refuses_another_account(self):
        with pytest.raises(PrincipalMismatchError):
            assert_principal(
                stored=41, principal=77, site=SITE,
                store_label="search index", store_path="/tmp/search.db",
                remedy="Delete it.",
            )


class TestCacheBinding:
    def test_stamp_is_site_keyed(self, tmp_path):
        path = tmp_path / "cache.db"
        with CacheStore(path) as store:
            store.bind_principal(SITE, 41)
            store.bind_principal(OTHER_SITE, 77)
            store.commit()
        with CacheStore(path) as store:
            assert store.get_principal(SITE) == 41
            assert store.get_principal(OTHER_SITE) == 77

    def test_same_principal_round_trip(self, tmp_path):
        path = tmp_path / "cache.db"
        with CacheStore(path) as store:
            store.bind_principal(SITE, 41)
            store.commit()
        with CacheStore(path) as store:
            store.bind_principal(SITE, 41)
            assert store.get_principal(SITE) == 41

    def test_cross_principal_write_is_refused(self, tmp_path):
        path = tmp_path / "cache.db"
        with CacheStore(path) as store:
            store.bind_principal(SITE, 41)
            store.commit()
        with CacheStore(path) as store:
            with pytest.raises(PrincipalMismatchError) as exc:
                store.bind_principal(SITE, 77)
        assert str(path) in str(exc.value)
        assert "WORSAGA_CACHE_PATH" in str(exc.value)

    def test_stamp_lives_in_the_meta_table(self, tmp_path):
        path = tmp_path / "cache.db"
        with CacheStore(path) as store:
            store.bind_principal(SITE, 41)
            store.commit()
            row = store._conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                (principal_meta_key(SITE),),
            ).fetchone()
        assert row[0] == "41"


class TestSyncGuard:
    def _cache(self, tmp_path):
        return tmp_path / "cache.db"

    def test_first_sync_stamps_the_cache(self, tmp_path):
        cache_path = self._cache(tmp_path)
        client = DemoMoodleClient()
        run_sync(client, cache_path=cache_path)
        with CacheStore(cache_path) as store:
            assert store.get_principal(client.base_url) == DEMO_USERID

    def test_second_sync_by_the_same_account_succeeds(self, tmp_path):
        cache_path = self._cache(tmp_path)
        client = DemoMoodleClient()
        run_sync(client, cache_path=cache_path)
        result = run_sync(client, cache_path=cache_path)
        assert result["site"] == client.base_url

    def test_second_account_is_refused_with_both_ids(self, tmp_path):
        cache_path = self._cache(tmp_path)
        client = DemoMoodleClient()
        run_sync(client, cache_path=cache_path)

        class _Impostor(DemoMoodleClient):
            userid = 999
            verified_userid = 999

        with pytest.raises(PrincipalMismatchError) as exc:
            run_sync(_Impostor(), cache_path=cache_path)
        message = str(exc.value)
        assert str(DEMO_USERID) in message and "999" in message

    def test_refusal_writes_nothing(self, tmp_path):
        cache_path = self._cache(tmp_path)
        client = DemoMoodleClient()
        first = run_sync(client, cache_path=cache_path)

        class _Impostor(DemoMoodleClient):
            userid = 999
            verified_userid = 999

        with pytest.raises(PrincipalMismatchError):
            run_sync(_Impostor(), cache_path=cache_path)
        with CacheStore(cache_path) as store:
            assert store.get_principal(client.base_url) == DEMO_USERID
            assert store.last_sync_at(client.base_url) is not None
        # The refused run recorded no second sync run.
        assert first["site"] == client.base_url

    def test_legacy_cache_is_adopted_once_with_a_notice(
        self, tmp_path, caplog,
    ):
        cache_path = self._cache(tmp_path)
        client = DemoMoodleClient()
        # Simulate a cache written before account binding: real rows, no
        # stamp.
        run_sync(client, cache_path=cache_path)
        with CacheStore(cache_path) as store:
            store.begin_immediate()
            store._conn.execute(
                "DELETE FROM meta WHERE key = ?",
                (principal_meta_key(client.base_url),),
            )
            store.commit()

        with caplog.at_level(logging.WARNING, logger="worsaga.principal"):
            run_sync(client, cache_path=cache_path)
        notices = [
            record for record in caplog.records
            if "now bound to Moodle user id" in record.getMessage()
        ]
        assert len(notices) == 1
        assert str(DEMO_USERID) in notices[0].getMessage()

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="worsaga.principal"):
            run_sync(client, cache_path=cache_path)
        assert caplog.records == []

    def test_identity_verified_late_is_still_checked(self, tmp_path):
        """The regression this ordering exists for.

        The client's identity is unknown when the run starts and known by
        the time it writes. Capturing the principal early saw ``None``
        and skipped the guard, so one account's data landed in another
        account's store; captured at write time the mismatch is caught.
        """
        cache_path = self._cache(tmp_path)
        # A cache belonging to somebody else.
        with CacheStore(cache_path) as store:
            store.bind_principal(DemoMoodleClient().base_url, 999)
            store.commit()

        client = _LateVerifyingClient()
        assert client.verified_userid is None
        with pytest.raises(PrincipalMismatchError) as exc:
            run_sync(client, cache_path=cache_path)
        assert "999" in str(exc.value) and str(DEMO_USERID) in str(exc.value)
        with CacheStore(cache_path) as store:
            assert store.get_principal(client.base_url) == 999
            assert store.last_sync_at(client.base_url) is None

    def test_identity_verified_late_stamps_a_fresh_store(self, tmp_path):
        cache_path = self._cache(tmp_path)
        client = _LateVerifyingClient()
        result = run_sync(client, cache_path=cache_path)
        assert result["categories"]["grades"]["synced"] is True
        with CacheStore(cache_path) as store:
            assert store.get_principal(client.base_url) == DEMO_USERID

    def test_unverified_run_writes_nothing_into_a_bound_cache(
        self, tmp_path, caplog,
    ):
        """A total outage must not crash, and must not write either."""
        cache_path = self._cache(tmp_path)
        client = DemoMoodleClient()
        run_sync(client, cache_path=cache_path)
        with CacheStore(cache_path) as store:
            before = store.last_sync_at(client.base_url)

        with caplog.at_level(logging.WARNING, logger="worsaga.sync"):
            result = run_sync(_NeverVerifiesClient(), cache_path=cache_path)

        assert all(
            not stats["synced"] for stats in result["categories"].values()
        )
        assert any(
            "could not be attributed" in warning
            for warning in result["warnings"]
        )
        assert any(
            "no rows and no run record were written" in rec.getMessage()
            for rec in caplog.records
        )
        with CacheStore(cache_path) as store:
            # No new run row, and the stamp is untouched.
            assert store.last_sync_at(client.base_url) == before
            assert store.get_principal(client.base_url) == DEMO_USERID

    def test_unverified_run_may_still_write_an_unbound_cache(self, tmp_path):
        # Nothing to mix with, so recording the failed run is harmless.
        cache_path = self._cache(tmp_path)
        result = run_sync(_NeverVerifiesClient(), cache_path=cache_path)
        assert result["categories"]["grades"]["synced"] is True
        with CacheStore(cache_path) as store:
            assert store.get_principal(DemoMoodleClient().base_url) is None

    def test_offline_change_reads_are_unguarded(self, tmp_path):
        cache_path = self._cache(tmp_path)
        client = DemoMoodleClient()
        run_sync(client, cache_path=cache_path)
        # No client, no identity — reading recorded changes still works.
        changes = get_recent_changes(
            client.base_url, cache_path=cache_path, since_days=3650,
        )
        assert isinstance(changes, list)

    def test_bind_reports_whether_writing_may_proceed(self, tmp_path):
        path = tmp_path / "cache.db"
        with CacheStore(path) as store:
            # Unstamped store, no identity: nothing to mix with.
            assert store.bind_principal(SITE, None) is True
            assert store.bind_principal(SITE, 41) is True
            store.commit()
        with CacheStore(path) as store:
            assert store.bind_principal(SITE, 41) is True
            # Stamped store, no identity: unattributable, so no writing.
            assert store.bind_principal(SITE, None) is False


class TestIndexGuard:
    def test_build_stamps_the_index(self, tmp_path):
        index_path = tmp_path / "search.db"
        client = DemoMoodleClient()
        build_text_index(client, index_path=index_path, max_files=2)
        with TextIndexStore(index_path) as store:
            assert store.get_principal(client.base_url) == DEMO_USERID

    def test_second_account_cannot_add_to_the_index(self, tmp_path):
        index_path = tmp_path / "search.db"
        build_text_index(
            DemoMoodleClient(), index_path=index_path, max_files=2,
        )

        class _Impostor(DemoMoodleClient):
            userid = 999
            verified_userid = 999

        with pytest.raises(PrincipalMismatchError) as exc:
            build_text_index(_Impostor(), index_path=index_path, max_files=2)
        assert "WORSAGA_INDEX_PATH" in str(exc.value)

    def test_search_with_a_verified_identity_is_checked(self, tmp_path):
        index_path = tmp_path / "search.db"
        client = DemoMoodleClient()
        build_text_index(client, index_path=index_path, max_files=2)
        with pytest.raises(PrincipalMismatchError):
            search_text_index(
                client.base_url, "the", index_path=index_path, principal=999,
            )

    def test_offline_search_is_unguarded(self, tmp_path):
        index_path = tmp_path / "search.db"
        client = DemoMoodleClient()
        build_text_index(client, index_path=index_path, max_files=2)
        result = search_text_index(
            client.base_url, "the", index_path=index_path,
        )
        assert result["site"] == client.base_url

    def test_unverified_build_writes_nothing_into_a_bound_index(
        self, tmp_path, caplog,
    ):
        index_path = tmp_path / "search.db"
        client = DemoMoodleClient()
        build_text_index(client, index_path=index_path, max_files=2)
        with TextIndexStore(index_path) as store:
            before = store.stats(client.base_url)["documents"]

        with caplog.at_level(logging.WARNING, logger="worsaga.textindex"):
            result = build_text_index(
                _NeverVerifiesClient(), index_path=index_path, max_files=2,
            )
        assert result["files_indexed"] == 0
        assert any(
            "could not be attributed" in warning
            for warning in result["warnings"]
        )
        assert any(
            "nothing was indexed" in rec.getMessage()
            for rec in caplog.records
        )
        with TextIndexStore(index_path) as store:
            assert store.stats(client.base_url)["documents"] == before
            assert store.get_principal(client.base_url) == DEMO_USERID

    def test_identity_verified_late_is_still_checked(self, tmp_path):
        index_path = tmp_path / "search.db"
        with TextIndexStore(index_path) as store:
            store.bind_principal(DemoMoodleClient().base_url, 999)
        with pytest.raises(PrincipalMismatchError):
            build_text_index(
                _LateVerifyingClient(), index_path=index_path, max_files=2,
            )

    def test_search_does_not_adopt_an_unstamped_index(self, tmp_path):
        index_path = tmp_path / "search.db"
        with TextIndexStore(index_path):
            pass
        search_text_index(SITE, "anything", index_path=index_path, principal=41)
        with TextIndexStore(index_path) as store:
            assert store.get_principal(SITE) is None


class TestAutosyncRecordGuard:
    """The record is bound opportunistically — install never goes online."""

    def _record(self):
        return json.loads(
            autosync.autosync_record_path().read_text(encoding="utf-8")
        )

    @pytest.fixture(autouse=True)
    def _isolated_record(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            autosync, "autosync_record_path",
            lambda: tmp_path / "autosync.json",
        )
        monkeypatch.setenv("WORSAGA_CACHE_PATH", str(tmp_path / "cache.db"))
        # No live or demo client: the local binding resolves to nothing
        # unless a test passes one explicitly.
        monkeypatch.setattr(autosync, "_local_binding", lambda: (None, ""))

    def _ok(self, stdout=""):
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=stdout, stderr="",
        )

    def test_install_stamps_the_record(self, monkeypatch):
        monkeypatch.setattr(autosync.sys, "platform", "win32")
        monkeypatch.setattr(autosync.shutil, "which", lambda name: None)
        with patch.object(
            autosync, "_run",
            return_value=self._ok(f'"\\{autosync.WINDOWS_TASK_NAME}"'),
        ):
            result = install_autosync(30, principal=41, site=SITE)
        assert result["installed"] is True
        record = self._record()
        assert record["principal_userid"] == 41
        assert record["site"] == SITE

    def test_same_account_reinstall_succeeds(self, monkeypatch):
        monkeypatch.setattr(autosync.sys, "platform", "win32")
        monkeypatch.setattr(autosync.shutil, "which", lambda name: None)
        with patch.object(
            autosync, "_run",
            return_value=self._ok(f'"\\{autosync.WINDOWS_TASK_NAME}"'),
        ):
            install_autosync(30, principal=41, site=SITE)
            result = install_autosync(45, principal=41, site=SITE)
        assert result["installed"] is True
        assert self._record()["interval_minutes"] == 45

    def test_other_account_is_refused_and_changes_nothing(self, monkeypatch):
        monkeypatch.setattr(autosync.sys, "platform", "win32")
        monkeypatch.setattr(autosync.shutil, "which", lambda name: None)
        with patch.object(
            autosync, "_run",
            return_value=self._ok(f'"\\{autosync.WINDOWS_TASK_NAME}"'),
        ):
            install_autosync(30, principal=41, site=SITE)

        def _explode(args):
            raise AssertionError("the scheduler must not be touched")

        with patch.object(autosync, "_run", _explode):
            result = install_autosync(45, principal=77, site=SITE)
        assert result["installed"] is False
        assert "41" in result["error"] and "77" in result["error"]
        assert self._record()["interval_minutes"] == 30

    def test_a_different_site_is_not_a_conflict(self, monkeypatch):
        monkeypatch.setattr(autosync.sys, "platform", "win32")
        monkeypatch.setattr(autosync.shutil, "which", lambda name: None)
        with patch.object(
            autosync, "_run",
            return_value=self._ok(f'"\\{autosync.WINDOWS_TASK_NAME}"'),
        ):
            install_autosync(30, principal=41, site=SITE)
            result = install_autosync(30, principal=77, site=OTHER_SITE)
        assert result["installed"] is True
        assert self._record()["principal_userid"] == 77
        assert self._record()["site"] == OTHER_SITE

    def test_site_change_drops_a_stale_binding(self, monkeypatch):
        """Moving to another Moodle must not carry the old user id over."""
        monkeypatch.setattr(autosync.sys, "platform", "win32")
        monkeypatch.setattr(autosync.shutil, "which", lambda name: None)
        with patch.object(
            autosync, "_run",
            return_value=self._ok(f'"\\{autosync.WINDOWS_TASK_NAME}"'),
        ):
            install_autosync(30, principal=41, site=SITE)
            # Same machine, new site, identity not verified this run.
            monkeypatch.setattr(
                autosync, "_local_binding", lambda: (None, OTHER_SITE),
            )
            result = install_autosync(45)
        assert result["installed"] is True
        record = self._record()
        assert record["site"] == OTHER_SITE
        assert "principal_userid" not in record

    def test_unverified_install_keeps_a_binding_for_the_same_site(
        self, monkeypatch,
    ):
        monkeypatch.setattr(autosync.sys, "platform", "win32")
        monkeypatch.setattr(autosync.shutil, "which", lambda name: None)
        with patch.object(
            autosync, "_run",
            return_value=self._ok(f'"\\{autosync.WINDOWS_TASK_NAME}"'),
        ):
            install_autosync(30, principal=41, site=SITE)
            monkeypatch.setattr(
                autosync, "_local_binding", lambda: (None, SITE),
            )
            result = install_autosync(45)
        assert result["installed"] is True
        record = self._record()
        assert record["principal_userid"] == 41
        assert record["site"] == SITE

    def test_live_install_records_the_site_without_a_principal(
        self, monkeypatch,
    ):
        """The real no-network path: site known from config, id unknown."""
        monkeypatch.setattr(autosync.sys, "platform", "win32")
        monkeypatch.setattr(autosync.shutil, "which", lambda name: None)
        monkeypatch.setattr(
            autosync, "_local_binding",
            lambda: (autosync.known_principal(_Principal(0)), SITE),
        )
        with patch.object(
            autosync, "_run",
            return_value=self._ok(f'"\\{autosync.WINDOWS_TASK_NAME}"'),
        ):
            result = install_autosync(30)
        assert result["installed"] is True
        record = self._record()
        assert record["site"] == SITE
        assert "principal_userid" not in record

    def test_live_install_stamps_when_identity_is_known(self, monkeypatch):
        monkeypatch.setattr(autosync.sys, "platform", "win32")
        monkeypatch.setattr(autosync.shutil, "which", lambda name: None)
        client = DemoMoodleClient()
        # The real _local_binding, against a client that already knows its
        # own identity — what demo mode and a warmed-up live client both
        # look like. No network is involved in either.
        monkeypatch.setattr(autosync, "_local_binding", _REAL_LOCAL_BINDING)
        monkeypatch.setattr(autosync, "_local_client", lambda: client)
        with patch.object(
            autosync, "_run",
            return_value=self._ok(f'"\\{autosync.WINDOWS_TASK_NAME}"'),
        ):
            result = install_autosync(30)
        assert result["installed"] is True
        record = self._record()
        assert record["principal_userid"] == DEMO_USERID
        assert record["site"] == client.base_url

    def test_legacy_record_adoption_is_announced(self, monkeypatch, caplog):
        monkeypatch.setattr(autosync.sys, "platform", "win32")
        monkeypatch.setattr(autosync.shutil, "which", lambda name: None)
        with patch.object(
            autosync, "_run",
            return_value=self._ok(f'"\\{autosync.WINDOWS_TASK_NAME}"'),
        ):
            install_autosync(30)
            assert "principal_userid" not in self._record()
            with caplog.at_level(
                logging.WARNING, logger="worsaga.principal",
            ):
                install_autosync(30, principal=41, site=SITE)
        notices = [
            record for record in caplog.records
            if "now bound to Moodle user id" in record.getMessage()
        ]
        assert len(notices) == 1
        assert self._record()["principal_userid"] == 41
