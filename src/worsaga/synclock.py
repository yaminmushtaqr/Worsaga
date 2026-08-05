"""One sync at a time per site: an interprocess lock beside the cache.

``worsaga watch`` runs a sync every few minutes. ``worsaga auto-sync``
registers another one with the platform scheduler. A user runs ``worsaga
sync`` by hand while both are installed, and an agent calls ``sync_now``
at the same moment. Nothing in that picture is unusual, and on a large
enrolment two of them overlapping means every course, every forum, and
every gradebook fetched twice concurrently — the exact load spike the rate
coordinator exists to prevent, arriving from a second process where a
per-process limiter cannot see it.

The cache's ``BEGIN IMMEDIATE`` transaction already stops the *writes*
from interleaving, but it does so at the end, after both runs have paid
for the whole fetch. This lock is taken at the start instead, so the
second run finds out before it makes a single request and returns a
structured ``skipped`` result.

Mechanics, deliberately minimal:

- The lock is an ``O_EXCL`` file beside the cache, named for the site so
  two accounts (or two ``WORSAGA_CACHE_PATH`` values) never block each
  other. Creation is the acquisition — no separate check-then-create race,
  and ``O_EXCL`` is the sole arbiter of who owns it.
- It holds the owning process id, the time it was taken, and a random
  **ownership token**. Nothing deletes a lock file whose token is not its
  own: a process that was displaced (its lock judged stale and recovered
  by somebody else) must never go on to delete its successor's lock, which
  is how two syncs end up running at once.
- **Staleness** is judged from liveness first and time second. Where the
  standard library can answer safely — ``os.kill(pid, 0)`` on POSIX, which
  signals nothing and merely reports whether the pid exists — a *live*
  owner keeps its lock however long its sync takes, and a *dead* one loses
  it immediately. On Windows there is no equivalent (``os.kill`` there
  *terminates* the target) and Worsaga takes no dependency on ``psutil``,
  so a Windows lock is only ever recovered on age: :data:`LOCK_TTL_SECONDS`
  since the owner was last heard from. Long syncs stay heard-from through
  :meth:`SyncLock.touch`, which the collection phase calls at each
  category boundary. The known cost of the POSIX rule is pid reuse: a
  recycled pid can keep a dead lock alive. Two hours of a genuinely
  crashed sync is the price, and it is bounded on the platform where
  liveness cannot be checked at all.
- Release deletes the file in a ``finally``, after checking the token. A
  process killed between acquire and release leaves the file behind; the
  liveness check (POSIX) or the TTL (Windows) is what cleans it up.

The lock guards *this machine*. Two machines syncing the same account into
their own caches are independent by design.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import secrets
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

from worsaga.secureio import (
    PRIVATE_FILE_MODE,
    ensure_private_dir,
    open_new_private_file,
)

logger = logging.getLogger(__name__)

#: How long since the owner was last heard from before a lock is assumed
#: abandoned. Only consulted where process liveness cannot be checked
#: (Windows), so it is set well past any plausible sync: a first sync of a
#: very large enrolment over a slow link is the case that must never have
#: its lock stolen out from under it.
LOCK_TTL_SECONDS = 2 * 60 * 60


def lock_path(site: str, cache_path: str | Path) -> Path:
    """Return the lock file for *site* beside *cache_path*.

    Keyed by both: the cache path decides which store is being written,
    and the site digest keeps two accounts sharing one directory from
    blocking each other. The digest (not the URL) keeps the site out of a
    filename that other local users can list.
    """
    cache = Path(cache_path)
    digest = sha256(str(site).encode("utf-8")).hexdigest()[:16]
    return cache.with_name(f"{cache.name}.sync-{digest}.lock")


def _now() -> int:
    return int(time.time())


def _process_liveness_is_knowable() -> bool:
    """Whether this platform can be asked if a pid is running, safely."""
    return os.name == "posix"


def _process_is_gone(pid: int) -> bool:
    """Return True only when *pid* is provably not running.

    Conservative on purpose: "cannot tell" is never "gone", because
    deleting a live process's lock is how two syncs end up running
    together. Windows always answers "cannot tell" — see the module
    docstring.
    """
    if pid <= 0:
        return True
    if not _process_liveness_is_knowable():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        # The process exists and belongs to somebody else.
        return False
    except OSError as exc:
        return exc.errno == errno.ESRCH
    return False


class SyncLock:
    """An exclusive, interprocess lock over one site's sync.

    Also usable for any other short critical section that has to hold
    across processes: pass *path* to place the lock file yourself and
    *ttl_seconds* to set how long an unattended one may sit before it is
    treated as abandoned (see :mod:`worsaga.syncstate`).
    """

    def __init__(
        self,
        site: str,
        cache_path: str | Path,
        *,
        path: str | Path | None = None,
        ttl_seconds: int | None = None,
    ):
        self.site = str(site)
        self.path = Path(path) if path else lock_path(site, cache_path)
        self.ttl_seconds = (
            LOCK_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
        )
        self.holder: dict[str, Any] | None = None
        #: Proof of ownership. Nothing deletes a lock file carrying
        #: somebody else's token, so a process whose lock was recovered
        #: cannot delete the lock of whoever recovered it.
        self._token = secrets.token_hex(8)
        self._held = False
        self._unlocked = False

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self) -> bool:
        """Take the lock, recovering a stale one; False when it is busy.

        On failure :attr:`holder` describes the process that owns it (as
        far as its lock file can be read), so the caller can say who is
        already syncing instead of just refusing.

        Recovery never decides ownership — ``O_EXCL`` creation does. A
        stale file is removed (once, by whoever gets there first) and then
        every contender races to create; exactly one wins and the rest see
        ``FileExistsError`` and report a busy lock.
        """
        outcome = self._create()
        if outcome != "exists":
            return self._settle(outcome)

        holder = self._read_holder()
        if not self._is_stale(holder):
            self.holder = holder
            return False

        logger.info(
            "Recovering an abandoned sync lock at %s (%s).",
            self.path, self._describe(holder),
        )
        if not self._remove_abandoned(holder):
            self.holder = self._read_holder() or holder
            return False

        outcome = self._create()
        if outcome != "exists":
            return self._settle(outcome)
        # Somebody else won the race for the freed lock. That is a busy
        # lock, not an error.
        self.holder = self._read_holder()
        return False

    def release(self) -> None:
        """Delete the lock file, if it is still ours.

        A lock carrying a different token belongs to a process that
        recovered ours after judging it abandoned. Deleting it would hand
        a third process a lock the second one still believes it holds, so
        it is left alone and the situation is logged.
        """
        if self._unlocked:
            self._unlocked = False
            return
        if not self._held:
            return
        self._held = False
        current = self._read_holder()
        if not current:
            return
        if current.get("token") != self._token:
            logger.warning(
                "The sync lock at %s is no longer ours (%s); leaving it in "
                "place. This run was judged abandoned while it was still "
                "working.",
                self.path, self._describe(current),
            )
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(
                "could not remove the sync lock at %s: %s. The next sync "
                "recovers it once this process exits.",
                self.path, exc,
            )

    def touch(self) -> None:
        """Say the owner is still working, for platforms that judge on age.

        Called at each collection-phase boundary. Cheap (one ``utime``),
        best effort, and a no-op when the lock is not held.
        """
        if not self._held:
            return
        try:
            os.utime(self.path, None)
        except OSError as exc:
            logger.debug("could not refresh the sync lock timestamp: %s", exc)

    def _settle(self, outcome: str) -> bool:
        """Record the result of a create attempt that was not ``exists``."""
        if outcome == "created":
            self._held = True
        else:  # "unavailable"
            self._unlocked = True
        return True

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False

    def busy_message(self) -> str:
        """Return the one-line reason this lock could not be taken."""
        return (
            "another Worsaga sync is already running for this site "
            f"({self._describe(self.holder)}); this run was skipped so the "
            "two do not fetch everything twice"
        )

    # ── internals ──────────────────────────────────────────────────

    def _create(self) -> str:
        """Try to create the lock. ``created`` / ``exists`` / ``unavailable``."""
        payload = json.dumps({
            "pid": os.getpid(),
            "started_at": _now(),
            "site": self.site,
            "token": self._token,
        }, sort_keys=True) + "\n"
        try:
            ensure_private_dir(self.path.parent)
            fd = open_new_private_file(self.path, mode=PRIVATE_FILE_MODE)
        except FileExistsError:
            return "exists"
        except OSError as exc:
            # A directory that cannot be created or written is not a
            # reason to refuse to sync; log it and run unlocked, exactly
            # as Worsaga did before the lock existed.
            logger.warning(
                "could not create the sync lock at %s: %s. Running without "
                "it.", self.path, exc,
            )
            self.holder = None
            return "unavailable"
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
        except OSError as exc:
            logger.debug("could not describe the sync lock holder: %s", exc)
        return "created"

    def _remove_abandoned(self, holder: dict[str, Any]) -> bool:
        """Delete the abandoned lock described by *holder*; False if not ours.

        Re-reads immediately before unlinking and only proceeds while the
        file is still the one that was judged abandoned. Without that,
        two contenders can both unlink — the second deleting the fresh
        lock the first has just created — and both end up believing they
        own it.
        """
        current = self._read_holder()
        if not current:
            return True  # somebody else already cleared it
        if current.get("token") != holder.get("token"):
            return False  # replaced since we judged it; not ours to remove
        try:
            self.path.unlink()
        except FileNotFoundError:
            return True
        except OSError as exc:
            logger.warning("could not remove the abandoned sync lock: %s", exc)
            return False
        return True

    def _read_holder(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as handle:
                record = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {"unreadable": True, "mtime": self._mtime()}
        if not isinstance(record, dict):
            return {"unreadable": True, "mtime": self._mtime()}
        return record

    def _mtime(self) -> int:
        try:
            return int(self.path.stat().st_mtime)
        except OSError:
            return 0

    def _last_alive(self, holder: dict[str, Any]) -> int:
        """When the owner was last known to be working.

        The later of the recorded start time and the file's modification
        time, so :meth:`touch` genuinely extends the life of a long sync
        rather than leaving it judged on when it began.
        """
        try:
            started = int(holder.get("started_at") or 0)
        except (TypeError, ValueError):
            started = 0
        return max(started, self._mtime())

    def _is_stale(self, holder: dict[str, Any]) -> bool:
        if not holder:
            # The file vanished between the failed create and the read.
            return True
        try:
            pid = int(holder.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid > 0:
            if _process_is_gone(pid):
                # Provably dead: no waiting, whatever the clock says.
                return True
            if _process_liveness_is_knowable():
                # Provably alive. A first sync of a huge enrolment over a
                # slow link may legitimately run for hours; its lock is
                # not up for grabs while it is still running.
                return False
        # Liveness cannot be established (Windows, or no usable pid), so
        # age is all there is to go on.
        last_alive = self._last_alive(holder)
        if not last_alive:
            # Undatable and unprobeable: nothing can ever judge it, so
            # treat it as abandoned rather than blocking forever.
            return True
        return _now() - last_alive > self.ttl_seconds

    def _describe(self, holder: dict[str, Any] | None) -> str:
        if not holder:
            return "owner unknown"
        if holder.get("unreadable"):
            return "unreadable lock file"
        pid = holder.get("pid")
        last_alive = self._last_alive(holder)
        age = max(0, _now() - last_alive) if last_alive else 0
        parts = []
        if pid:
            parts.append(f"pid {pid}")
        if last_alive:
            parts.append(f"active {age}s ago")
        return ", ".join(parts) or "owner unknown"


__all__ = ["LOCK_TTL_SECONDS", "SyncLock", "lock_path"]
