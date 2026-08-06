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
        ["", "v3:deadbeef", "not-a-digest", "abc123", "V2:" + "0" * 64,
         "v2:", "v2:deadbeef", "v2:" + "A" * 64, "v3:" + "a" * 64],
        ids=["empty", "newer-version", "corrupt", "truncated", "wrong-case",
             "tagged-empty", "tagged-truncated", "tagged-uppercase",
             "tagged-newer"],
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

    def test_the_current_shape_must_be_a_whole_tagged_digest(self):
        """A ``v2:`` prefix alone used to be enough to call a row current.

        It made a corrupt or truncated value comparable to a real digest,
        which it can never equal — so the row was reported as a grade
        change nobody had made. Both recognised shapes are matched in
        full, and anything else is adopted quietly.
        """
        from worsaga.sync import _CATEGORIES

        spec = _CATEGORIES["grades"]
        assert spec.fingerprint_state("v2:" + "a" * 64) == "current"
        assert spec.fingerprint_state("v2:") == "unknown"
        assert spec.fingerprint_state("v2:deadbeef") == "unknown"
        assert spec.fingerprint_state("v2:" + "A" * 64) == "unknown"
        assert spec.fingerprint_state("v2:" + "a" * 65) == "unknown"
        assert spec.fingerprint_state("v3:" + "a" * 64) == "unknown"
        assert spec.fingerprint_state("a" * 64) == "legacy"

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


# ── Scrubbing feedback out of a cache that already has it ─────────


class TestStoredFeedbackScrub:
    """An existing cache is scrubbed on first open, not left as it was.

    Minimising what a *new* sync writes does nothing about the text
    already on disk: a grade row for an item that never appears in
    another snapshot is never rewritten, and every recorded change event
    keeps the before/after feedback it was recorded with — which
    ``worsaga changes`` replays for as long as the cache exists.
    """

    OLD_TEXT = "Your third paragraph is lifted from the set reading."
    EVENT_BEFORE = "Marked provisionally, pending moderation."
    EVENT_AFTER = "Confirmed after moderation - do not quote me."
    #: Marks a payload long enough that scrubbing it leaves the old record
    #: behind in free space until the file is rebuilt.
    SPILLED = "Left in the free pages until the rebuild runs."

    def _write_old_shape(
        self, cache_path, client, *, extra_rows=(), stored_feedback=None,
        markers=(),
    ):
        """Seed a cache exactly as a Worsaga that stored feedback left it.

        Written with raw SQLite on purpose: going through
        :class:`CacheStore` would run the scrub on the way in and there
        would be nothing left to migrate.

        *stored_feedback* replaces the text in the stored payload while
        the fingerprint is still computed from the live text, which is
        exactly what the old pipeline produced for a row the storage
        sanitizer edited on its way in. *markers* seeds ``meta`` rows, for
        the tests about what a marker value means.
        """
        import hashlib
        import sqlite3

        from worsaga.cache import _SCHEMA
        from worsaga.grades import normalize_grade_items

        records = normalize_grade_items(
            client.get_user_grade_items(101),
            course_id=101, course_shortname="ECON101",
        )
        conn = sqlite3.connect(cache_path, isolation_level=None)
        try:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", "1"),
            )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                (f"principal_userid:{SITE}", "7"),
            )
            for key, value in markers:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?)", (key, value),
                )
            for record in records:
                old_fields = (
                    "grade_display", "percentage", "status", "feedback",
                )
                data = {field: record.get(field) for field in old_fields}
                digest = hashlib.sha256(
                    json.dumps(data, sort_keys=True, default=str).encode()
                ).hexdigest()
                stored = dict(record)
                if stored_feedback is not None:
                    stored["feedback"] = stored_feedback
                self._insert_item(
                    conn, "grades",
                    f"{record['course_id']}:{record['item_id']}",
                    digest, json.dumps(stored, sort_keys=True, default=str),
                )
            for category, item_key, fingerprint, payload in extra_rows:
                self._insert_item(
                    conn, category, item_key, fingerprint, payload,
                )
            conn.execute(
                "INSERT INTO changes (site, category, item_key, kind, detail,"
                " detected_at) VALUES (?, ?, ?, ?, ?, ?)",
                (SITE, "grades", "101:9", "grade_updated", json.dumps({
                    "kind": "grade_updated",
                    "course_id": 101,
                    "course_shortname": "ECON101",
                    "title": "Problem Set 1",
                    "before": {"grade_display": "62.00", "percentage": "62.00 %",
                               "status": "graded", "feedback": self.EVENT_BEFORE},
                    "after": {"grade_display": "70.00", "percentage": "70.00 %",
                              "status": "graded", "feedback": self.EVENT_AFTER},
                    "detected_at": NOW - 3600,
                }, sort_keys=True), NOW - 3600),
            )
            conn.execute(
                "INSERT INTO category_syncs (site, category, last_synced_at,"
                " scope) VALUES (?, ?, ?, ?)",
                (SITE, "grades", NOW - 3600, json.dumps([101])),
            )
        finally:
            conn.close()

    def _insert_item(self, conn, category, item_key, fingerprint, payload):
        conn.execute(
            "INSERT INTO items (site, category, item_key, fingerprint,"
            " payload, first_seen_at, last_seen_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (SITE, category, item_key, fingerprint, payload,
             NOW - 3600, NOW - 3600, NOW - 3600),
        )

    def _raw_rows(self, cache_path, category="grades"):
        import sqlite3

        conn = sqlite3.connect(cache_path)
        try:
            return conn.execute(
                "SELECT item_key, fingerprint, payload FROM items"
                " WHERE category = ?", (category,),
            ).fetchall()
        finally:
            conn.close()

    def test_the_text_is_gone_from_the_items_table(self, cache_path):
        client = _FakeClient(feedback=self.OLD_TEXT)
        self._write_old_shape(cache_path, client)

        with CacheStore(cache_path) as cache:
            rows = cache.get_items(SITE, "grades")

        assert rows
        for row in rows.values():
            assert "feedback" not in row["payload"]
            assert row["payload"]["feedback_present"] is True
            assert row["payload"]["feedback_hash"] == feedback_hash(
                self.OLD_TEXT
            )
            assert row["fingerprint"].startswith("v2:")

    def test_the_text_is_gone_from_the_change_history(self, cache_path):
        self._write_old_shape(cache_path, _FakeClient(feedback=self.OLD_TEXT))

        with CacheStore(cache_path) as cache:
            events = cache.get_changes(SITE, since_ts=0)

        rendered = json.dumps(events)
        assert self.EVENT_BEFORE not in rendered
        assert self.EVENT_AFTER not in rendered
        assert events[0]["after"]["feedback_present"] is True
        assert events[0]["after"]["feedback_hash"] == feedback_hash(
            self.EVENT_AFTER
        )
        assert "feedback" not in events[0]["before"]

    def test_the_words_are_gone_from_the_file_itself(self, cache_path):
        """Rewriting the rows is not enough on its own.

        SQLite leaves the old, longer record in the free space of its
        page, so without the rebuild that follows the scrub the words are
        still in the file for anything that reads it as bytes.
        """
        self._write_old_shape(cache_path, _FakeClient(feedback=self.OLD_TEXT))
        assert self.OLD_TEXT.encode() in cache_path.read_bytes()

        with CacheStore(cache_path):
            pass

        raw = cache_path.read_bytes()
        assert self.OLD_TEXT.encode() not in raw
        assert self.EVENT_AFTER.encode() not in raw

    def test_the_replayed_feed_carries_no_feedback_text(self, cache_path):
        from worsaga.sync import get_recent_changes

        self._write_old_shape(cache_path, _FakeClient(feedback=self.OLD_TEXT))
        events = get_recent_changes(SITE, cache_path=cache_path, since_days=30)
        assert events
        assert self.EVENT_AFTER not in json.dumps(events)

    def test_the_replay_drops_a_feedback_field_the_scrub_never_saw(
        self, cache_path,
    ):
        """Belt over the migration's braces, at the surface agents read.

        A detail that somehow still carries the field — written by a
        newer Worsaga, restored from a backup, edited by hand — must not
        reach a caller with the words in it.
        """
        import sqlite3

        from worsaga.sync import get_recent_changes

        self._write_old_shape(cache_path, _FakeClient(feedback=self.OLD_TEXT))
        with CacheStore(cache_path):
            pass  # migrate first, so only the replay guard can catch this

        conn = sqlite3.connect(cache_path, isolation_level=None)
        try:
            conn.execute(
                "INSERT INTO changes (site, category, item_key, kind, detail,"
                " detected_at) VALUES (?, ?, ?, ?, ?, ?)",
                (SITE, "grades", "101:99", "grade_updated", json.dumps({
                    "kind": "grade_updated", "title": "Problem Set 2",
                    "before": {"grade_display": "60.00", "feedback": "planted"},
                    "after": {"grade_display": "70.00",
                              "feedback": "planted after the scrub"},
                    "detected_at": NOW,
                }), NOW),
            )
        finally:
            conn.close()

        events = get_recent_changes(SITE, cache_path=cache_path, since_days=30)
        rendered = json.dumps(events)
        assert "planted" not in rendered
        assert all("feedback" not in (e.get("after") or {}) for e in events)

    def test_a_feedback_only_edit_across_the_upgrade_is_caught_at_once(
        self, cache_path,
    ):
        """Re-fingerprinting is what makes the first sync a real comparison.

        The old row is rewritten into the current shape, so the next sync
        compares like with like instead of adopting a shape it cannot
        read — and an edit made between the two is reported then, not a
        sync later.
        """
        client = _FakeClient(feedback="Marked before the upgrade.")
        self._write_old_shape(cache_path, client)
        client.feedback = "Rewritten after the upgrade."

        result = _sync(client, cache_path, categories="grades")

        assert [c["kind"] for c in result["changes"]] == ["grade_updated"]
        assert result["categories"]["grades"]["migrated"] == 0
        assert result["categories"]["grades"]["updated"] == 1
        assert "Rewritten after" not in json.dumps(result["changes"])

    def test_an_unchanged_gradebook_reports_nothing_after_the_upgrade(
        self, cache_path,
    ):
        client = _FakeClient(feedback=self.OLD_TEXT)
        self._write_old_shape(cache_path, client)

        result = _sync(client, cache_path, categories="grades")

        assert result["changes"] == []
        assert result["categories"]["grades"]["migrated"] == 0
        assert result["categories"]["grades"]["updated"] == 0

    def test_opening_twice_scrubs_once(self, cache_path):
        """The marker is the point: a scrubbed cache is not scrubbed again."""
        self._write_old_shape(cache_path, _FakeClient(feedback=self.OLD_TEXT))
        with CacheStore(cache_path):
            pass

        # A row planted *after* the migration ran survives untouched,
        # which is only true if the second open did not repeat it.
        import sqlite3

        conn = sqlite3.connect(cache_path, isolation_level=None)
        try:
            self._insert_item(
                conn, "grades", "101:planted", "a" * 64,
                json.dumps({"feedback": "planted after the upgrade"}),
            )
        finally:
            conn.close()

        with CacheStore(cache_path):
            pass

        planted = dict(
            (key, (fingerprint, payload))
            for key, fingerprint, payload in self._raw_rows(cache_path)
        )["101:planted"]
        assert planted[0] == "a" * 64
        assert "planted after the upgrade" in planted[1]

    def test_the_opt_in_gets_the_text_back_without_a_change_storm(
        self, cache_path,
    ):
        """Someone who asked for the text keeps it — from the next sync on.

        The scrub is unconditional, because it is about what is already on
        disk rather than about what this run wants. ``--store-feedback``
        re-populates it on the next sync, and because the fingerprint
        covers the hash rather than the words, nothing looks changed.
        """
        client = _FakeClient(feedback="Steady work.")
        self._write_old_shape(cache_path, client)
        with CacheStore(cache_path):
            pass

        result = _sync(
            client, cache_path, categories="grades", store_feedback=True,
        )

        assert result["changes"] == []
        payloads = [row["payload"] for row in _cached_grades(cache_path).values()]
        assert any(row.get("feedback") == "Steady work." for row in payloads)

    def test_a_malformed_row_does_not_stop_the_upgrade(self, cache_path):
        """A row that will not parse is skipped, not fatal and not deleted.

        Read back with raw SQL: an unreadable payload is beyond
        ``get_items`` too, and what this asserts is that opening the store
        survived it and scrubbed everything around it.
        """
        self._write_old_shape(
            cache_path, _FakeClient(feedback=self.OLD_TEXT),
            extra_rows=[("grades", "101:broken", "a" * 64, "{not json at all")],
        )

        with CacheStore(cache_path):
            pass

        rows = dict(
            (key, (fingerprint, payload))
            for key, fingerprint, payload in self._raw_rows(cache_path)
        )
        assert self.OLD_TEXT not in rows["101:9"][1]
        assert rows["101:9"][0].startswith("v2:")
        assert rows["101:broken"] == ("a" * 64, "{not json at all")

    def _markers(self, cache_path):
        import sqlite3

        from worsaga.cache import (
            FEEDBACK_RECLAIM_META_KEY,
            FEEDBACK_SCRUB_META_KEY,
        )

        conn = sqlite3.connect(cache_path)
        try:
            rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        finally:
            conn.close()
        return (
            rows.get(FEEDBACK_SCRUB_META_KEY),
            rows.get(FEEDBACK_RECLAIM_META_KEY),
        )

    def test_a_clean_cache_records_both_phases(self, cache_path):
        """Nothing to scrub means nothing to reclaim, and both are recorded.

        Otherwise a cache that never held any feedback would try to
        rebuild itself on every single open, forever.
        """
        with CacheStore(cache_path):
            pass
        assert self._markers(cache_path) == ("1", "1")

    def test_a_scrubbed_cache_records_both_phases(self, cache_path):
        self._write_old_shape(cache_path, _FakeClient(feedback=self.OLD_TEXT))
        with CacheStore(cache_path):
            pass
        assert self._markers(cache_path) == ("1", "1")

    @pytest.mark.parametrize(
        "marker", ["yes", "", "1.0", "v1", "true"],
        ids=["word", "empty", "float", "tagged", "boolean"],
    )
    def test_a_marker_that_is_not_an_integer_reruns_the_scrub(
        self, cache_path, marker,
    ):
        """A marker nobody can read says nothing about what was done.

        Any non-empty value used to suppress the scrub outright, so one
        corrupt byte left the text on disk permanently. Rerunning is safe:
        the scrub is idempotent.
        """
        from worsaga.cache import FEEDBACK_SCRUB_META_KEY

        self._write_old_shape(
            cache_path, _FakeClient(feedback=self.OLD_TEXT),
            markers=[(FEEDBACK_SCRUB_META_KEY, marker)],
        )

        with CacheStore(cache_path):
            pass

        assert self.OLD_TEXT.encode() not in cache_path.read_bytes()
        assert self._markers(cache_path) == ("1", "1")

    def test_a_marker_from_a_newer_worsaga_is_left_alone(self, cache_path):
        """A build that knows more has been here; do not undo its work."""
        from worsaga.cache import FEEDBACK_SCRUB_META_KEY

        self._write_old_shape(
            cache_path, _FakeClient(feedback=self.OLD_TEXT),
            markers=[(FEEDBACK_SCRUB_META_KEY, "99")],
        )

        with CacheStore(cache_path):
            pass

        assert self._markers(cache_path) == ("99", None)
        rows = dict(
            (key, payload) for key, _, payload in self._raw_rows(cache_path)
        )
        assert self.OLD_TEXT in rows["101:9"]

    def test_an_older_marker_reruns_a_corrected_scrub(self, cache_path):
        """Version bump semantics: a later revision reaches old caches."""
        from worsaga.cache import FEEDBACK_SCRUB_META_KEY

        self._write_old_shape(
            cache_path, _FakeClient(feedback=self.OLD_TEXT),
            markers=[(FEEDBACK_SCRUB_META_KEY, "0")],
        )

        with CacheStore(cache_path):
            pass

        assert self._markers(cache_path) == ("1", "1")
        assert self.OLD_TEXT.encode() not in cache_path.read_bytes()

    def test_a_failed_rebuild_is_retried_on_the_next_open(
        self, cache_path, monkeypatch,
    ):
        """The two phases fail separately, so they are recorded separately.

        A single marker written before the rebuild would remember a
        reclaim that never happened, and the words would stay recoverable
        from the file's free space with nothing left to retry them.
        """
        import sqlite3

        # A row long enough that the shorter, scrubbed record cannot be
        # written over it in place: this is the case where the words
        # survive in the page's free space until the file is rebuilt.
        spilled = json.dumps({
            "item_id": 42, "course_id": 101, "grade_display": "70.00",
            "percentage": "70.00 %", "status": "graded",
            "feedback": self.SPILLED + " " + "marking detail " * 200,
        }, sort_keys=True)
        self._write_old_shape(
            cache_path, _FakeClient(feedback=self.OLD_TEXT),
            extra_rows=[("grades", "101:42", "c" * 64, spilled)],
        )
        real_connect = sqlite3.connect

        class _RefusesToVacuum:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args):
                if sql.strip().upper().startswith("VACUUM"):
                    raise sqlite3.OperationalError("database or disk is full")
                return self._conn.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        monkeypatch.setattr(
            sqlite3, "connect",
            lambda *a, **kw: _RefusesToVacuum(real_connect(*a, **kw)),
        )
        with CacheStore(cache_path) as store:
            rows = store.get_items(SITE, "grades")
        monkeypatch.undo()

        # Phase one landed: the rows no longer carry the text.
        assert all("feedback" not in row["payload"] for row in rows.values())
        assert self._markers(cache_path) == ("1", None)
        # Phase two did not: the words are still in the file's free space.
        assert self.SPILLED.encode() in cache_path.read_bytes()

        # The next open retries the rebuild alone, and finishes the job.
        with CacheStore(cache_path):
            pass
        assert self._markers(cache_path) == ("1", "1")
        assert self.SPILLED.encode() not in cache_path.read_bytes()

    def test_a_second_opener_does_not_declare_the_rebuild_done(
        self, cache_path, monkeypatch,
    ):
        """The interleaving that made an outstanding rebuild disappear.

        One process scrubs the rows and commits, and its rebuild fails.
        A second process — which looked at the markers *before* that
        commit — then takes the write lock, finds nothing left carrying
        feedback, and used to read that as "a clean cache, nothing to
        reclaim", setting both markers. The words stayed in free space
        with nothing left to retry them. The markers are re-read inside
        the transaction now, so only a transaction that finds no
        phase-one marker at all may claim a clean cache.
        """
        import sqlite3

        spilled = json.dumps({
            "item_id": 42, "course_id": 101, "grade_display": "70.00",
            "percentage": "70.00 %", "status": "graded",
            "feedback": self.SPILLED + " " + "marking detail " * 200,
        }, sort_keys=True)
        self._write_old_shape(
            cache_path, _FakeClient(feedback=self.OLD_TEXT),
            extra_rows=[("grades", "101:42", "c" * 64, spilled)],
        )
        real_connect = sqlite3.connect

        class _RefusesToVacuum:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args):
                if sql.strip().upper().startswith("VACUUM"):
                    raise sqlite3.OperationalError("database or disk is full")
                return self._conn.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        # Opener A: scrubs, commits phase one, cannot rebuild.
        monkeypatch.setattr(
            sqlite3, "connect",
            lambda *a, **kw: _RefusesToVacuum(real_connect(*a, **kw)),
        )
        with CacheStore(cache_path):
            pass
        monkeypatch.undo()
        assert self._markers(cache_path) == ("1", None)

        # Opener B: sees an already-scrubbed cache with nothing to do, and
        # must not mistake that for a cache that never needed reclaiming.
        # Its own rebuild fails too, so the marker stays outstanding.
        monkeypatch.setattr(
            sqlite3, "connect",
            lambda *a, **kw: _RefusesToVacuum(real_connect(*a, **kw)),
        )
        with CacheStore(cache_path):
            pass
        monkeypatch.undo()
        assert self._markers(cache_path) == ("1", None)
        assert self.SPILLED.encode() in cache_path.read_bytes()

        # A third open, with a working rebuild, finishes the job.
        with CacheStore(cache_path):
            pass
        assert self._markers(cache_path) == ("1", "1")
        assert self.SPILLED.encode() not in cache_path.read_bytes()

    def test_a_pathological_row_neither_crashes_nor_holds_a_transaction(
        self, cache_path,
    ):
        """Deeply nested JSON raises RecursionError, not a parse error.

        The row is skipped; the open completes; nothing is left holding a
        write transaction on the cache.
        """
        nested = '{"a":' * 4000 + '{"feedback": "deep"}' + "}" * 4000
        self._write_old_shape(
            cache_path, _FakeClient(feedback=self.OLD_TEXT),
            extra_rows=[("grades", "101:deep", "a" * 64, nested)],
        )

        with CacheStore(cache_path) as store:
            assert store._conn.in_transaction is False

        rows = dict(
            (key, payload) for key, _, payload in self._raw_rows(cache_path)
        )
        assert self.OLD_TEXT not in rows["101:9"]
        assert rows["101:deep"] == nested

    def test_an_unexpected_failure_rolls_back_and_defers(
        self, cache_path, monkeypatch,
    ):
        """Anything the scrub raises must leave the cache openable."""
        from worsaga import cache as cache_module

        self._write_old_shape(cache_path, _FakeClient(feedback=self.OLD_TEXT))

        def _explode(payload):
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr(cache_module, "scrub_feedback", _explode)
        with CacheStore(cache_path) as store:
            assert store._conn.in_transaction is False
        monkeypatch.undo()

        # Nothing was recorded, so the next open tries again and succeeds.
        assert self._markers(cache_path) == (None, None)
        with CacheStore(cache_path):
            pass
        assert self._markers(cache_path) == ("1", "1")
        assert self.OLD_TEXT.encode() not in cache_path.read_bytes()

    @pytest.mark.parametrize(
        "feedback",
        [
            "Strong argument ***but*** check the units in Q3.",
            "The marks below are REDACTED until moderation closes.",
            "See the rubric; token=... appears in the sample answer.",
        ],
        ids=["markdown-stars", "the-word-redacted", "looks-like-a-parameter"],
    )
    def test_honest_feedback_that_merely_looks_edited_still_migrates(
        self, cache_path, feedback,
    ):
        """Guessing from the text was wrong; the fingerprint is checked.

        Feedback containing Markdown emphasis, or the word REDACTED, or
        something shaped like a query parameter, is ordinary instructor
        writing. Treating it as sanitizer-edited demoted a clean row to
        the quiet adoption path and swallowed a real feedback edit made
        before the first post-upgrade sync.
        """
        client = _FakeClient(feedback=feedback)
        self._write_old_shape(cache_path, client)

        with CacheStore(cache_path) as cache:
            rows = cache.get_items(SITE, "grades")
        assert rows["101:9"]["fingerprint"].startswith("v2:")

        client.feedback = "Rewritten after moderation."
        result = _sync(client, cache_path, categories="grades")
        assert [c["kind"] for c in result["changes"]] == ["grade_updated"]

    def test_a_sanitised_row_keeps_its_legacy_fingerprint(self, cache_path):
        """Old caches stored feedback the *sanitizer* had already edited.

        Recomputing the legacy fingerprint over the stored payload is what
        detects it: the old pipeline hashed the record it fetched and
        stored a sanitized copy, so for an edited row the two no longer
        agree. Hashing those words produces a value the next sync — which
        hashes what Moodle actually says — can never match, so
        re-fingerprinting would announce a feedback change nobody made.
        Such a row keeps its legacy fingerprint and takes the sync's
        adoption path instead.
        """
        client = _FakeClient(feedback="See https://x/f.php?token=abcdef123456")
        self._write_old_shape(
            cache_path, client,
            stored_feedback="See https://x/f.php?token=REDACTED",
        )
        before = dict(
            (key, fingerprint)
            for key, fingerprint, _ in self._raw_rows(cache_path)
        )

        with CacheStore(cache_path) as cache:
            rows = cache.get_items(SITE, "grades")

        assert "feedback" not in rows["101:9"]["payload"]
        assert rows["101:9"]["fingerprint"] == before["101:9"]
        assert not rows["101:9"]["fingerprint"].startswith("v2:")

    def test_a_sanitised_row_reports_no_change_on_the_next_sync(
        self, cache_path,
    ):
        client = _FakeClient(feedback="See https://x/f.php?token=abcdef123456")
        self._write_old_shape(
            cache_path, client,
            stored_feedback="See https://x/f.php?token=REDACTED",
        )

        result = _sync(client, cache_path, categories="grades")

        assert result["changes"] == []
        assert result["categories"]["grades"]["migrated"] == 1
        assert result["categories"]["grades"]["updated"] == 0

    def test_a_clean_row_still_gets_the_current_fingerprint(self, cache_path):
        """The exception is for edited text only, not for everything."""
        self._write_old_shape(cache_path, _FakeClient(feedback=self.OLD_TEXT))
        with CacheStore(cache_path) as cache:
            rows = cache.get_items(SITE, "grades")
        assert rows["101:9"]["fingerprint"].startswith("v2:")

    def test_other_categories_are_never_touched(self, cache_path):
        """Only grades changed shape, and only grades hold feedback."""
        payload = json.dumps(
            {"name": "Problem Set 1", "feedback": "not a grade row"},
            sort_keys=True,
        )
        self._write_old_shape(
            cache_path, _FakeClient(feedback=self.OLD_TEXT),
            extra_rows=[("deadlines", "assign:1", "b" * 64, payload)],
        )

        with CacheStore(cache_path):
            pass

        rows = self._raw_rows(cache_path, category="deadlines")
        assert rows == [("assign:1", "b" * 64, payload)]


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

    def test_a_contended_lock_shows_the_notice_but_records_nothing(
        self, tmp_path, capsys, monkeypatch,
    ):
        """Showing twice is cheap; a lost record un-says a notice.

        The unserialised read-modify-write used to run anyway, which can
        drop the entry another process has just written for a *different*
        site. Printing without recording is self-healing: this site is
        told again next time.
        """
        from worsaga.synclock import SyncLock

        path = tmp_path / "notices.json"
        other = "https://other.example.edu"
        notices.announce_third_party_collection(other, path=path)
        capsys.readouterr()

        monkeypatch.setattr(SyncLock, "acquire", lambda self: False)
        assert notices.announce_third_party_collection(SITE, path=path) is True
        assert "other people" in capsys.readouterr().err

        assert notices.notice_shown(SITE, path=path) is False
        assert notices.notice_shown(other, path=path) is True

    def test_the_lock_file_sits_beside_the_record(self, tmp_path):
        path = tmp_path / "notices.json"
        assert notices.notices_lock_path(path).parent == path.parent
        assert notices.notices_lock_path(path).name == "notices.lock"
