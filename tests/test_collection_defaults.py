"""Third-party-data collection defaults: what a sync gathers, and what it keeps.

Three invariants live here:

- an unattended run does not collect forums, a foreground one does, and
  ``--categories`` overrides both;
- a category nobody selected is not a category that failed — no change
  events, no tombstones, no effect on the outcome;
- instructor feedback text does not reach the cache unless it was asked
  for, and swapping the fingerprint to a hash does not make every
  existing grade look changed on the first run after the upgrade.
"""

import json
import time

import pytest

from worsaga import notices
from worsaga.cache import CacheStore
from worsaga.sync import (
    SYNC_CATEGORIES,
    UNATTENDED_SYNC_CATEGORIES,
    feedback_hash,
    parse_sync_categories,
    resolve_store_feedback,
    resolve_sync_categories,
    run_sync,
    sync_outcome,
)

NOW = int(time.time())
FUTURE = NOW + 5 * 86400
RECENT = NOW - 2 * 86400

SITE = "https://moodle.example.edu"


class _FakeClient:
    """A client covering every category, with recorded per-category calls."""

    base_url = SITE
    is_demo = True  # keeps run_sync off the lock file and the state dir

    def __init__(self, *, feedback="Good structure, tighten the conclusion."):
        self.feedback = feedback
        # Mutable so a test can re-mark the item between two syncs.
        self.grade_display = "70.00"
        self.calls: list[str] = []
        self.courses = [
            {"id": 101, "shortname": "ECON101", "fullname": "Economics"},
        ]

    def get_courses(self):
        self.calls.append("courses")
        return self.courses

    def enrolled_course_ids(self):
        return frozenset({101})

    @property
    def verified_userid(self):
        return 7

    def get_assignments_by_courses(self, course_ids):
        self.calls.append("assignments")
        return {"courses": [{"id": 101, "shortname": "ECON101", "assignments": [
            {"id": 1, "name": "Problem Set 1", "duedate": FUTURE},
        ]}]}

    def get_quizzes(self, course_ids=None):
        self.calls.append("quizzes")
        return {"quizzes": []}

    def get_course_contents(self, course_id):
        self.calls.append("contents")
        return [{"id": 11, "section": 1, "name": "Week 1", "modules": [
            {"id": 10, "name": "Week 1 slides", "modname": "resource",
             "contents": [{
                 "type": "file", "filename": "week1.pdf", "filepath": "/",
                 "fileurl": f"{SITE}/webservice/pluginfile.php/10/week1.pdf",
                 "filesize": 100, "mimetype": "application/pdf",
                 "timemodified": RECENT,
             }]},
        ]}]

    def get_user_grade_items(self, course_id):
        self.calls.append("grades")
        return {"usergrades": [{"gradeitems": [
            {"id": 9, "itemname": "Problem Set 1",
             "gradeformatted": self.grade_display,
             "percentageformatted": "70.00 %", "grademin": 0, "grademax": 100,
             "feedback": self.feedback},
        ]}]}

    def get_forums_by_courses(self, course_ids):
        self.calls.append("forums")
        return {"forums": [
            {"id": 6, "course": 101, "name": "Announcements", "type": "news"},
        ]}

    def get_forum_discussions(self, forum_id):
        self.calls.append("discussions")
        return {"discussions": [
            {"discussion": 301, "name": "Welcome", "userfullname": "Dr Fake",
             "created": RECENT, "timemodified": RECENT},
        ]}


@pytest.fixture
def cache_path(tmp_path):
    return tmp_path / "cache.db"


def _sync(client, cache_path, **kwargs):
    return run_sync(client, cache_path=cache_path, **kwargs)


# ── The selector ──────────────────────────────────────────────────


class TestParseSyncCategories:
    def test_accepts_a_comma_separated_list_in_canonical_order(self):
        assert parse_sync_categories("grades,deadlines") == (
            "deadlines", "grades",
        )

    def test_accepts_a_sequence(self):
        assert parse_sync_categories(["forums"]) == ("forums",)

    def test_is_case_insensitive_and_deduplicates(self):
        assert parse_sync_categories("GRADES, grades ,Grades") == ("grades",)

    def test_all_means_every_category(self):
        assert parse_sync_categories("all") == SYNC_CATEGORIES

    def test_unknown_name_names_the_valid_ones(self):
        with pytest.raises(ValueError) as excinfo:
            parse_sync_categories("forum")
        message = str(excinfo.value)
        assert "forum" in message
        for name in SYNC_CATEGORIES:
            assert name in message

    def test_empty_selection_is_refused(self):
        with pytest.raises(ValueError):
            parse_sync_categories("  ,  ")


class TestResolveSyncCategories:
    def test_foreground_default_is_every_category(self):
        assert resolve_sync_categories() == SYNC_CATEGORIES

    def test_unattended_default_leaves_forums_alone(self):
        resolved = resolve_sync_categories(unattended=True)
        assert resolved == UNATTENDED_SYNC_CATEGORIES
        assert "forums" not in resolved

    def test_environment_sets_a_persistent_default(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_SYNC_CATEGORIES", "grades")
        assert resolve_sync_categories() == ("grades",)
        assert resolve_sync_categories(unattended=True) == ("grades",)

    def test_explicit_argument_beats_the_environment(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_SYNC_CATEGORIES", "grades")
        assert resolve_sync_categories("forums") == ("forums",)

    def test_a_bad_environment_value_is_loud(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_SYNC_CATEGORIES", "everything")
        with pytest.raises(ValueError):
            resolve_sync_categories()


class TestResolveStoreFeedback:
    def test_off_by_default(self):
        assert resolve_store_feedback() is False

    def test_environment_opt_in(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_SYNC_STORE_FEEDBACK", "1")
        assert resolve_store_feedback() is True

    def test_explicit_argument_wins(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_SYNC_STORE_FEEDBACK", "1")
        assert resolve_store_feedback(False) is False

    def test_a_non_truthy_value_stays_off(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_SYNC_STORE_FEEDBACK", "maybe")
        assert resolve_store_feedback() is False


# ── What a run actually collects ──────────────────────────────────


class TestUnattendedCollection:
    def test_unattended_run_makes_no_forum_request(self, cache_path):
        client = _FakeClient()
        result = _sync(client, cache_path, unattended=True)
        assert "forums" not in client.calls
        assert "discussions" not in client.calls
        assert result["selected_categories"] == list(
            UNATTENDED_SYNC_CATEGORIES
        )

    def test_foreground_run_still_collects_forums(self, cache_path):
        client = _FakeClient()
        result = _sync(client, cache_path)
        assert "forums" in client.calls
        assert result["categories"]["forums"]["synced"] is True

    def test_explicit_categories_opt_forums_back_in(self, cache_path):
        client = _FakeClient()
        result = _sync(
            client, cache_path, unattended=True, categories="forums,grades",
        )
        assert "forums" in client.calls
        assert result["selected_categories"] == ["grades", "forums"]
        assert result["categories"]["deadlines"]["selected"] is False

    def test_unknown_category_refuses_before_any_request(self, cache_path):
        client = _FakeClient()
        with pytest.raises(ValueError):
            _sync(client, cache_path, categories="forum")
        assert client.calls == []


class TestUnselectedIsNotUnsynced:
    def test_a_full_run_of_selected_categories_is_a_success(self, cache_path):
        result = _sync(
            _FakeClient(), cache_path, unattended=True,
        )
        assert result["outcome"] == "success"

    def test_unselected_categories_are_marked_not_selected(self, cache_path):
        result = _sync(_FakeClient(), cache_path, categories="grades")
        forums = result["categories"]["forums"]
        assert forums["selected"] is False
        assert forums["synced"] is False
        assert forums["items"] == 0

    def test_sync_outcome_ignores_unselected_entries(self):
        categories = {
            "grades": {"synced": True, "selected": True},
            "forums": {"synced": False, "selected": False},
        }
        assert sync_outcome(categories) == "success"

    def test_sync_outcome_still_reads_a_legacy_map(self):
        """A category map without ``selected`` behaves exactly as before."""
        assert sync_outcome({
            "grades": {"synced": True}, "forums": {"synced": False},
        }) == "partial"

    def test_deselecting_a_category_emits_no_change_events(self, cache_path):
        client = _FakeClient()
        _sync(client, cache_path)  # baseline, all four
        second = _sync(client, cache_path, categories="grades")
        assert second["changes"] == []
        assert second["outcome"] == "success"

    def test_deselecting_leaves_the_cached_rows_untouched(self, cache_path):
        client = _FakeClient()
        _sync(client, cache_path)
        with CacheStore(cache_path) as cache:
            before = cache.get_items(SITE, "forums")
            state_before = cache.get_category_state(SITE, "forums")

        _sync(client, cache_path, categories="grades")

        with CacheStore(cache_path) as cache:
            after = cache.get_items(SITE, "forums")
            state_after = cache.get_category_state(SITE, "forums")
        assert set(after) == set(before)
        assert state_after["last_synced_at"] == state_before["last_synced_at"]

    def test_reenabling_resumes_from_the_old_baseline(self, cache_path):
        """A category switched off and back on is not re-baselined.

        The whole point of leaving the rows alone: coming back must not
        report every existing discussion as new, and must not silently
        swallow one that really did appear.
        """
        client = _FakeClient()
        _sync(client, cache_path)
        _sync(client, cache_path, categories="grades")
        third = _sync(client, cache_path, categories="forums")
        assert third["categories"]["forums"]["baseline"] is False
        assert third["changes"] == []


# ── Grade feedback minimisation ───────────────────────────────────


def _cached_grades(cache_path):
    with CacheStore(cache_path) as cache:
        return cache.get_items(SITE, "grades")


class TestFeedbackMinimisation:
    def test_feedback_text_is_not_stored_by_default(self, cache_path):
        client = _FakeClient(feedback="See me about question 3, please.")
        _sync(client, cache_path)
        for row in _cached_grades(cache_path).values():
            assert "feedback" not in row["payload"]
            assert row["payload"]["feedback_present"] is True
            assert row["payload"]["feedback_hash"] == feedback_hash(
                "See me about question 3, please."
            )

    def test_the_text_never_appears_anywhere_in_the_cache_file(self, cache_path):
        secret = "You plagiarised the second paragraph."
        _sync(_FakeClient(feedback=secret), cache_path)
        assert secret.encode() not in cache_path.read_bytes()

    def test_opting_in_stores_the_text(self, cache_path):
        client = _FakeClient(feedback="Nicely argued.")
        _sync(client, cache_path, store_feedback=True)
        payloads = [row["payload"] for row in _cached_grades(cache_path).values()]
        assert any(row.get("feedback") == "Nicely argued." for row in payloads)

    def test_absent_feedback_hashes_to_empty(self, cache_path):
        _sync(_FakeClient(feedback=""), cache_path)
        for row in _cached_grades(cache_path).values():
            assert row["payload"]["feedback_present"] is False
            assert row["payload"]["feedback_hash"] == ""

    def test_a_feedback_edit_is_still_detected(self, cache_path):
        client = _FakeClient(feedback="First pass.")
        _sync(client, cache_path)
        client.feedback = "Revised after moderation."
        result = _sync(client, cache_path)
        kinds = [change["kind"] for change in result["changes"]]
        assert "grade_updated" in kinds

    def test_the_change_event_carries_no_feedback_text(self, cache_path):
        client = _FakeClient(feedback="First pass.")
        _sync(client, cache_path)
        client.feedback = "Do not quote me on this."
        result = _sync(client, cache_path)
        rendered = json.dumps(result["changes"])
        assert "Do not quote me" not in rendered
        assert "First pass" not in rendered
        change = result["changes"][0]
        assert "feedback_hash" in change["after"]
        assert change["after"]["feedback_present"] is True
        assert "feedback" not in change["after"]

    def test_opting_in_still_keeps_the_text_out_of_change_events(
        self, cache_path,
    ):
        """The opt-in widens what is *cached*, never what is replayed.

        Change events are the payload an agent reads back through
        ``get_changes``; keeping them uniformly text-free means no
        configuration can turn that surface into a feed of instructor
        comments.
        """
        client = _FakeClient(feedback="First pass.")
        _sync(client, cache_path, store_feedback=True)
        client.feedback = "Second pass, much better."
        result = _sync(client, cache_path, store_feedback=True)
        assert "Second pass" not in json.dumps(result["changes"])

    def test_switching_the_opt_in_on_does_not_look_like_a_change(
        self, cache_path,
    ):
        client = _FakeClient(feedback="Steady work.")
        _sync(client, cache_path)
        result = _sync(client, cache_path, store_feedback=True)
        assert result["changes"] == []


class TestFingerprintMigration:
    """The first sync after upgrading must not report every grade as changed."""

    def _write_legacy_rows(self, cache_path, client, *, fingerprint=None):
        """Seed the cache the way the pre-hash Worsaga would have.

        *fingerprint* overrides the stored value, so the same helper
        covers the shapes that are not legacy at all: a row written by a
        newer Worsaga, a truncated one, an empty one.
        """
        import hashlib

        from worsaga.grades import normalize_grade_items

        records = normalize_grade_items(
            client.get_user_grade_items(101),
            course_id=101, course_shortname="ECON101",
        )
        with CacheStore(cache_path) as cache:
            cache.bind_principal(SITE, 7)
            for record in records:
                legacy_fields = (
                    "grade_display", "percentage", "status", "feedback",
                )
                data = {f: record.get(f) for f in legacy_fields}
                digest = hashlib.sha256(
                    json.dumps(data, sort_keys=True, default=str).encode()
                ).hexdigest()
                key = f"{record['course_id']}:{record['item_id']}"
                cache.upsert_item(
                    SITE, "grades", key,
                    digest if fingerprint is None else fingerprint,
                    record, now=NOW - 3600,
                )
            cache.set_category_state(
                SITE, "grades", now=NOW - 3600, scope=[101],
            )
            cache.commit()

    def test_upgrading_reports_no_grade_changes(self, cache_path):
        client = _FakeClient(feedback="Well structured.")
        self._write_legacy_rows(cache_path, client)

        result = _sync(client, cache_path, categories="grades")

        stats = result["categories"]["grades"]
        assert result["changes"] == []
        assert stats["updated"] == 0
        assert stats["new"] == 0
        assert stats["migrated"] == 1
        assert stats["baseline"] is False

    def test_the_run_after_the_migration_detects_changes_normally(
        self, cache_path,
    ):
        client = _FakeClient(feedback="Well structured.")
        self._write_legacy_rows(cache_path, client)
        _sync(client, cache_path, categories="grades")

        client.feedback = "Rewritten after the resit."
        result = _sync(client, cache_path, categories="grades")
        assert [c["kind"] for c in result["changes"]] == ["grade_updated"]

    def test_migration_rewrites_the_stored_fingerprint(self, cache_path):
        client = _FakeClient()
        self._write_legacy_rows(cache_path, client)
        _sync(client, cache_path, categories="grades")
        for row in _cached_grades(cache_path).values():
            assert row["fingerprint"].startswith("v2:")

    def test_untouched_categories_keep_their_bare_fingerprints(self, cache_path):
        """Only grades changed shape; the others must not be re-tagged."""
        _sync(_FakeClient(), cache_path)
        with CacheStore(cache_path) as cache:
            rows = cache.get_items(SITE, "deadlines")
        assert rows
        for row in rows.values():
            assert not row["fingerprint"].startswith("v")

    def test_a_real_change_during_migration_is_still_reported(self, cache_path):
        """Migrating and changing are not alternatives.

        The dangerous version of this fix adopts every non-current
        fingerprint silently, which loses a grade that genuinely moved on
        the very run the user upgraded.
        """
        client = _FakeClient(feedback="Well structured.")
        self._write_legacy_rows(cache_path, client)

        # Moodle re-marks the item between the two runs.
        client.grade_display = "88.00"
        result = _sync(client, cache_path, categories="grades")

        stats = result["categories"]["grades"]
        assert [c["kind"] for c in result["changes"]] == ["grade_updated"]
        assert stats["migrated"] == 1
        assert stats["updated"] == 1
        change = result["changes"][0]
        assert change["before"]["grade_display"] == "70.00"
        assert change["after"]["grade_display"] == "88.00"

    def test_a_feedback_only_edit_during_migration_stays_silent(
        self, cache_path,
    ):
        """The one thing migration cannot judge is the field that moved.

        The old row holds the text and the new one holds a hash; they are
        not comparable, so a feedback-only edit on the migrating run is
        adopted rather than guessed at. It is caught on the next run.
        """
        client = _FakeClient(feedback="First pass.")
        self._write_legacy_rows(cache_path, client)
        client.feedback = "Rewritten entirely."

        first = _sync(client, cache_path, categories="grades")
        assert first["changes"] == []
        assert first["categories"]["grades"]["migrated"] == 1

        client.feedback = "Rewritten again."
        second = _sync(client, cache_path, categories="grades")
        assert [c["kind"] for c in second["changes"]] == ["grade_updated"]

    @pytest.mark.parametrize(
        "stored",
        ["", "v3:deadbeef", "not-a-digest", "abc123", "V2:" + "0" * 64],
        ids=["empty", "newer-version", "corrupt", "truncated", "wrong-case"],
    )
    def test_an_unrecognised_fingerprint_is_adopted_without_events(
        self, cache_path, stored,
    ):
        """Anything this build cannot reason about is adopted, never diffed.

        A row written by a *newer* Worsaga is the realistic case: the user
        downgraded, or two versions share a cache. Guessing would mean
        either a change storm or a fabricated diff.
        """
        client = _FakeClient()
        self._write_legacy_rows(cache_path, client, fingerprint=stored)
        # Change the grade too: an unknown shape must stay silent even
        # when the payload really did move, because nothing about the
        # stored fingerprint can be trusted to say so.
        client.grade_display = "99.00"

        result = _sync(client, cache_path, categories="grades")

        assert result["changes"] == []
        assert result["categories"]["grades"]["migrated"] == 1
        assert result["categories"]["grades"]["updated"] == 0

    def test_a_bare_digest_is_the_only_legacy_shape(self):
        """The classifier, directly: exactly 64 lower-case hex characters."""
        from worsaga.sync import _CATEGORIES

        spec = _CATEGORIES["grades"]
        assert spec.fingerprint_state("v2:" + "a" * 64) == "current"
        assert spec.fingerprint_state("a" * 64) == "legacy"
        assert spec.fingerprint_state("a" * 63) == "unknown"
        assert spec.fingerprint_state("a" * 65) == "unknown"
        assert spec.fingerprint_state("A" * 64) == "unknown"
        assert spec.fingerprint_state("") == "unknown"
        assert spec.fingerprint_state(None) == "unknown"

    def test_a_v1_category_treats_every_stored_value_as_current(self):
        """Categories that never changed shape must not migrate anything."""
        from worsaga.sync import _CATEGORIES

        for name in ("deadlines", "files", "forums"):
            spec = _CATEGORIES[name]
            assert spec.fingerprint_state("anything at all") == "current"


# ── The one-time notice ───────────────────────────────────────────


class TestThirdPartyNotice:
    def test_shown_once_then_recorded(self, tmp_path, capsys):
        path = tmp_path / "notices.json"
        assert notices.announce_third_party_collection(SITE, path=path) is True
        first = capsys.readouterr()
        assert "other people" in first.err
        assert first.out == ""

        assert notices.announce_third_party_collection(SITE, path=path) is False
        assert capsys.readouterr().err == ""

    def test_recorded_per_site(self, tmp_path, capsys):
        path = tmp_path / "notices.json"
        notices.announce_third_party_collection(SITE, path=path)
        capsys.readouterr()
        assert notices.announce_third_party_collection(
            "https://other.example.edu", path=path,
        ) is True

    def test_never_in_demo_mode(self, tmp_path, capsys):
        path = tmp_path / "notices.json"
        assert notices.announce_third_party_collection(
            SITE, is_demo=True, path=path,
        ) is False
        assert capsys.readouterr().err == ""
        assert not path.exists()

    def test_quiet_suppresses_without_consuming_it(self, tmp_path, capsys):
        """A notice skipped for silence is still owed, not spent."""
        path = tmp_path / "notices.json"
        assert notices.announce_third_party_collection(
            SITE, quiet=True, path=path,
        ) is False
        assert notices.notice_shown(SITE, path=path) is False
        assert notices.announce_third_party_collection(SITE, path=path) is True

    def test_the_text_is_ascii(self):
        notices.THIRD_PARTY_NOTICE.encode("ascii")

    def test_an_unwritable_record_does_not_raise(self, tmp_path, capsys):
        # A directory where the file should be: the write fails, the
        # notice is still shown, and nothing propagates.
        path = tmp_path / "notices.json"
        path.mkdir()
        assert notices.announce_third_party_collection(SITE, path=path) is True
        assert "other people" in capsys.readouterr().err

    def test_concurrent_announcers_print_exactly_once(self, tmp_path, capsys):
        """Two threads racing must not both show the notice.

        The check, the print, and the record are one critical section for
        this reason: without it both see "not yet shown", both print, and
        the loser's write can drop what the winner recorded.
        """
        import threading

        path = tmp_path / "notices.json"
        results: list[bool] = []
        barrier = threading.Barrier(4)

        def _announce():
            barrier.wait()
            results.append(
                notices.announce_third_party_collection(SITE, path=path)
            )

        threads = [threading.Thread(target=_announce) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(results) == 1
        assert capsys.readouterr().err.count("other people") == 1

    def test_a_second_site_is_not_lost_to_a_concurrent_write(
        self, tmp_path, capsys,
    ):
        """Records for different sites must merge, not overwrite."""
        path = tmp_path / "notices.json"
        notices.announce_third_party_collection(SITE, path=path)
        notices.announce_third_party_collection(
            "https://other.example.edu", path=path,
        )
        capsys.readouterr()
        assert notices.notice_shown(SITE, path=path) is True
        assert notices.notice_shown(
            "https://other.example.edu", path=path,
        ) is True

    def test_the_lock_file_sits_beside_the_record(self, tmp_path):
        path = tmp_path / "notices.json"
        assert notices.notices_lock_path(path).parent == path.parent
        assert notices.notices_lock_path(path).name == "notices.lock"
