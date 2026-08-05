"""Tests for the per-site interprocess sync lock."""

import json
import time
import os

import pytest

from worsaga import synclock
from worsaga.synclock import LOCK_TTL_SECONDS, SyncLock, lock_path

SITE = "https://moodle.example.edu"
OTHER = "https://moodle.other.example.edu"


@pytest.fixture()
def cache(tmp_path):
    return tmp_path / "cache.db"


class TestLockPath:
    def test_lives_beside_the_cache(self, cache):
        path = lock_path(SITE, cache)
        assert path.parent == cache.parent
        assert path.name.startswith("cache.db.sync-")
        assert path.name.endswith(".lock")

    def test_two_sites_do_not_block_each_other(self, cache):
        assert lock_path(SITE, cache) != lock_path(OTHER, cache)

    def test_two_caches_do_not_block_each_other(self, tmp_path):
        assert lock_path(SITE, tmp_path / "a.db") != lock_path(
            SITE, tmp_path / "b.db"
        )

    def test_site_url_is_not_readable_from_the_filename(self, cache):
        # Other local users can list a directory; they should not learn
        # which university this account belongs to from the lock's name.
        assert "moodle.example.edu" not in lock_path(SITE, cache).name


class TestAcquireRelease:
    def test_acquire_creates_and_release_removes(self, cache):
        lock = SyncLock(SITE, cache)
        assert lock.acquire() is True
        assert lock.path.exists()
        lock.release()
        assert not lock.path.exists()

    def test_lock_records_its_owner(self, cache):
        lock = SyncLock(SITE, cache)
        lock.acquire()
        record = json.loads(lock.path.read_text(encoding="utf-8"))
        assert record["pid"] == os.getpid()
        assert record["site"] == SITE
        assert record["started_at"] > 0
        lock.release()

    def test_a_second_holder_is_refused(self, cache):
        first = SyncLock(SITE, cache)
        assert first.acquire() is True
        second = SyncLock(SITE, cache)
        assert second.acquire() is False
        assert "another Worsaga sync is already running" in second.busy_message()
        assert f"pid {os.getpid()}" in second.busy_message()
        first.release()
        # Once the first releases, the second can take it.
        assert second.acquire() is True
        second.release()

    def test_different_sites_do_not_block(self, cache):
        first = SyncLock(SITE, cache)
        second = SyncLock(OTHER, cache)
        assert first.acquire() is True
        assert second.acquire() is True
        first.release()
        second.release()

    def test_release_without_acquire_is_a_no_op(self, cache):
        SyncLock(SITE, cache).release()  # must not raise

    def test_context_manager_releases_on_exception(self, cache):
        lock = SyncLock(SITE, cache)
        with pytest.raises(ValueError):
            with lock:
                assert lock.path.exists()
                raise ValueError("boom")
        assert not lock.path.exists()


def _plant_lock(cache, *, pid, age_seconds, token="planted"):
    """Write a lock file owned by *pid* that was last active *age* ago."""
    path = lock_path(SITE, cache)
    path.parent.mkdir(parents=True, exist_ok=True)
    when = int(time.time()) - age_seconds
    path.write_text(json.dumps({
        "pid": pid, "started_at": when, "site": SITE, "token": token,
    }), encoding="utf-8")
    os.utime(path, (when, when))
    return path


class TestStaleRecovery:
    def test_a_lock_past_the_ttl_with_no_live_owner_is_recovered(self, cache):
        _plant_lock(cache, pid=424242, age_seconds=LOCK_TTL_SECONDS + 60)
        with pytest.MonkeyPatch.context() as patcher:
            # POSIX would prove the pid is gone; Windows cannot probe at
            # all and recovers on age. Pin the probe so both platforms
            # exercise the same recovery path.
            patcher.setattr(synclock, "_process_is_gone", lambda pid: False)
            patcher.setattr(
                synclock, "_process_liveness_is_knowable", lambda: False,
            )
            lock = SyncLock(SITE, cache)
            assert lock.acquire() is True
        lock.release()

    def test_a_lock_owned_by_a_dead_process_is_recovered_at_once(self, cache):
        # Fresh timestamp: only the liveness check can justify recovery.
        _plant_lock(cache, pid=424242, age_seconds=5)
        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(synclock, "_process_is_gone", lambda pid: True)
            lock = SyncLock(SITE, cache)
            assert lock.acquire() is True
        lock.release()

    def test_a_live_owner_keeps_its_lock_past_the_ttl(self, cache):
        # The regression this guards: a legitimate multi-hour first sync
        # having its lock stolen just because it took a long time.
        _plant_lock(cache, pid=4242, age_seconds=LOCK_TTL_SECONDS * 3)
        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(synclock, "_process_is_gone", lambda pid: False)
            patcher.setattr(
                synclock, "_process_liveness_is_knowable", lambda: True,
            )
            lock = SyncLock(SITE, cache)
            assert lock.acquire() is False
        assert "pid 4242" in lock.busy_message()

    def test_a_live_lock_within_the_ttl_is_not_recovered(self, cache):
        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(synclock, "_process_is_gone", lambda pid: False)
            first = SyncLock(SITE, cache)
            assert first.acquire() is True
            second = SyncLock(SITE, cache)
            assert second.acquire() is False
        first.release()

    def test_touch_keeps_a_long_sync_from_being_judged_abandoned(self, cache):
        _plant_lock(cache, pid=424242, age_seconds=LOCK_TTL_SECONDS + 60)
        holder = SyncLock(SITE, cache)
        holder._held = True  # pretend this process planted it
        holder._token = "planted"
        holder.touch()
        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(synclock, "_process_is_gone", lambda pid: False)
            patcher.setattr(
                synclock, "_process_liveness_is_knowable", lambda: False,
            )
            other = SyncLock(SITE, cache)
            assert other.acquire() is False
        holder.release()

    def test_an_unreadable_lock_within_the_ttl_still_blocks(self, cache):
        path = lock_path(SITE, cache)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{corrupt", encoding="utf-8")
        lock = SyncLock(SITE, cache)
        # Fresh mtime, no readable pid: not provably abandoned, so it is
        # respected until the TTL. Failing open here would be how two
        # syncs end up running together.
        assert lock.acquire() is False
        assert "unreadable" in lock.busy_message()

    def test_an_unreadable_lock_past_the_ttl_is_recovered(self, cache):
        path = lock_path(SITE, cache)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{corrupt", encoding="utf-8")
        old = int(time.time()) - LOCK_TTL_SECONDS - 60
        os.utime(path, (old, old))
        lock = SyncLock(SITE, cache)
        assert lock.acquire() is True
        lock.release()


class TestOwnershipToken:
    def test_the_lock_records_a_token(self, cache):
        lock = SyncLock(SITE, cache)
        lock.acquire()
        record = json.loads(lock.path.read_text(encoding="utf-8"))
        assert record["token"] == lock._token
        assert len(record["token"]) >= 8
        lock.release()

    def test_release_never_deletes_somebody_elses_lock(self, cache):
        displaced = SyncLock(SITE, cache)
        assert displaced.acquire() is True
        # Its lock is judged abandoned and recovered by a successor.
        lock_path(SITE, cache).unlink()
        successor = SyncLock(SITE, cache)
        assert successor.acquire() is True

        displaced.release()
        # The successor is still holding a lock nobody has deleted: this
        # is the failure mode where a third process would be admitted
        # while the second still believes it is syncing.
        assert lock_path(SITE, cache).exists()
        record = json.loads(lock_path(SITE, cache).read_text(encoding="utf-8"))
        assert record["token"] == successor._token
        successor.release()
        assert not lock_path(SITE, cache).exists()

    def test_release_leaves_a_foreign_lock_alone_and_says_so(self, cache, caplog):
        lock = SyncLock(SITE, cache)
        lock.acquire()
        _plant_lock(cache, pid=999, age_seconds=1, token="somebody-else")
        with caplog.at_level("WARNING", logger="worsaga.synclock"):
            lock.release()
        assert lock_path(SITE, cache).exists()
        assert "no longer ours" in caplog.text

    def test_two_contenders_over_one_abandoned_lock_yield_one_owner(self, cache):
        _plant_lock(cache, pid=424242, age_seconds=LOCK_TTL_SECONDS + 60)
        with pytest.MonkeyPatch.context() as patcher:
            # Only the planted owner is dead; the contenders are this
            # (live) process, so neither may recover the other's lock.
            patcher.setattr(
                synclock, "_process_is_gone", lambda pid: pid == 424242,
            )
            first = SyncLock(SITE, cache)
            second = SyncLock(SITE, cache)
            results = [first.acquire(), second.acquire()]
        # O_EXCL is the arbiter, not the removal: exactly one wins, and
        # the loser reports a busy lock rather than an error.
        assert sorted(results) == [False, True]
        winner = first if results[0] else second
        record = json.loads(lock_path(SITE, cache).read_text(encoding="utf-8"))
        assert record["token"] == winner._token
        winner.release()

    def test_a_loser_of_the_recovery_race_does_not_delete_the_winner(
        self, cache,
    ):
        _plant_lock(cache, pid=424242, age_seconds=LOCK_TTL_SECONDS + 60,
                    token="abandoned")
        loser = SyncLock(SITE, cache)
        holder = loser._read_holder()
        assert holder["token"] == "abandoned"

        # Meanwhile the winner recovers it and takes the lock.
        winner = SyncLock(SITE, cache)
        lock_path(SITE, cache).unlink()
        assert winner.acquire() is True

        # The loser now acts on the judgement it made a moment ago. A
        # plain unlink here would delete the winner's fresh lock and let
        # the loser create its own - two owners.
        assert loser._remove_abandoned(holder) is False
        assert lock_path(SITE, cache).exists()
        record = json.loads(lock_path(SITE, cache).read_text(encoding="utf-8"))
        assert record["token"] == winner._token
        winner.release()


class TestCustomLockPlacement:
    def test_an_explicit_path_and_ttl_are_honoured(self, tmp_path):
        target = tmp_path / "state.lock"
        lock = SyncLock("anything", tmp_path / "unused.db",
                        path=target, ttl_seconds=5)
        assert lock.path == target
        assert lock.ttl_seconds == 5
        assert lock.acquire() is True
        assert target.exists()
        lock.release()
        assert not target.exists()


class TestLivenessProbe:
    def test_this_process_is_never_reported_gone(self):
        assert synclock._process_is_gone(os.getpid()) is False

    def test_a_nonsense_pid_is_gone(self):
        assert synclock._process_is_gone(0) is True
        assert synclock._process_is_gone(-1) is True

    @pytest.mark.skipif(os.name != "posix", reason="POSIX probe only")
    def test_posix_detects_a_missing_process(self):
        assert synclock._process_is_gone(4_194_303) is True

    @pytest.mark.skipif(os.name == "posix", reason="Windows fallback only")
    def test_windows_never_guesses(self):
        # os.kill(pid, 0) terminates on Windows, so liveness is never
        # probed there; recovery is the TTL alone.
        assert synclock._process_is_gone(424242) is False


class TestDegradedFilesystem:
    def test_an_uncreatable_lock_does_not_stop_a_sync(self, tmp_path, monkeypatch):
        def _refuse(*args, **kwargs):
            raise PermissionError("read-only volume")

        monkeypatch.setattr(synclock, "open_new_private_file", _refuse)
        lock = SyncLock(SITE, tmp_path / "cache.db")
        # Syncing without the lock is exactly what Worsaga did before it
        # existed; refusing to sync at all would be a regression.
        assert lock.acquire() is True
        lock.release()
