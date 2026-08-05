"""Owner-only file and directory primitives for Worsaga's local state.

Everything Worsaga keeps on disk is either a secret (the credentials
file) or personal academic material (the sync cache, the full-text
index, downloaded course files, study packs, the auto-sync record). This
module is the single place that decides how such a file or directory
comes into existence, so no caller has to repeat the permission dance —
and so there is exactly one implementation to audit.

The rules:

- **Owner-only from birth, not from a later chmod.** Files are created
  with their final mode in the ``os.open`` call itself. A ``chmod``
  after the fact leaves a window in which a freshly created file is
  readable by every other local user, and leaves any sidecar the writer
  creates in the meantime (an SQLite rollback journal, for instance)
  untouched.
- **Atomic replacement.** :func:`write_private_file` writes a private
  sibling temp file and ``os.replace``s it over the destination, so the
  destination always holds either the old content or the new content and
  never a mixture, and a concurrent reader never sees a partial file.
  The temp file is fsynced and the containing directory is fsynced where
  the platform allows it, but this is durability on a best-effort basis,
  not a guarantee that a power failure preserves the new content.
- **No writing through a symbolic link.** The destination is inspected
  with ``lstat`` and refused unless it is absent or a regular file, and
  the temp file is created with ``O_EXCL`` (plus ``O_NOFOLLOW`` where
  the platform has it). ``os.replace`` renames over the link itself
  rather than following it, so even a link planted between the check
  and the rename cannot redirect the write. The refusal covers the final
  path component only: the *parent* directories are trusted, because a
  local user who can replace a component of your own home directory has
  already won, and defending it would mean an ``openat``/``O_DIRECTORY``
  descent that buys nothing on a personal machine.
- **Existing directories are left exactly as they are.** Only
  directories this function actually creates get the private mode; a
  directory the user has already set up (and possibly shared on
  purpose) is never chmod-ed underneath them.

On Windows POSIX modes do not exist: ``os.open`` and ``os.mkdir`` accept
the mode argument and honour nothing but the read-only bit, so the same
code path runs unchanged and access control comes from the ACLs these
paths inherit from the user profile directory. Atomicity and the
symlink refusal apply on every platform.

The module also owns :func:`child_env`, the environment handed to
Worsaga's own subprocesses: a child that never needs the API token
should never be able to read it out of its own environment.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

#: Mode for every file Worsaga creates: read/write for the owner only.
PRIVATE_FILE_MODE = 0o600

#: Mode for every directory Worsaga creates: owner-only, and not
#: listable by anyone else (a directory listing of downloaded material
#: is itself a disclosure of what someone studies).
PRIVATE_DIR_MODE = 0o700

#: Environment variables carrying Worsaga's own secret material. They are
#: stripped from every child process environment: schedulers and desktop
#: notifiers need the ordinary environment but never the API token, and a
#: process environment is readable by other processes on some systems.
SECRET_ENV_VARS = ("WORSAGA_TOKEN",)

#: Exclusive creation, never following a final symlink, never translating
#: newlines (``O_BINARY`` matters on Windows, where the C runtime would
#: otherwise open the descriptor in text mode).
_CREATE_FLAGS = (
    os.O_CREAT
    | os.O_EXCL
    | os.O_WRONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_BINARY", 0)
)

#: Attempts to find an unused temp name before giving up. Names carry 12
#: random hex characters, so a single collision is already improbable.
_TEMP_ATTEMPTS = 8


class SecureWriteError(OSError):
    """Raised when a destination cannot be written safely.

    Subclasses :class:`OSError` so the CLI's existing top-level handler
    reports it as a one-line ``Error:`` instead of a traceback.
    """


def ensure_private_dir(
    path: str | Path, *, mode: int = PRIVATE_DIR_MODE,
) -> Path:
    """Create *path* (and missing parents) owner-only, and return it.

    Every directory this call creates is created *with* ``mode`` — not
    chmod-ed afterwards — including the intermediate ones, which
    ``Path.mkdir(parents=True)`` would otherwise create world-readable.
    Directories that already exist are returned untouched: they may be
    shared deliberately, and silently tightening someone's existing
    folder is not this function's business.
    """
    directory = Path(path)
    missing: list[Path] = []
    probe = directory
    while not probe.exists() and probe.parent != probe:
        missing.append(probe)
        probe = probe.parent
    for candidate in reversed(missing):
        try:
            candidate.mkdir(mode=mode)
        except FileExistsError:
            # Created concurrently between the probe and now.
            continue
    if not directory.is_dir():
        # Either the walk ran off the filesystem root or the path exists
        # as something else; let mkdir raise the accurate errno.
        directory.mkdir(mode=mode, parents=True, exist_ok=True)
    return directory


def open_new_private_file(
    path: str | Path, *, mode: int = PRIVATE_FILE_MODE,
) -> int:
    """Create *path* exclusively and return an open file descriptor.

    The caller owns the descriptor and must close it. ``FileExistsError``
    propagates unchanged so callers racing for a name (reserving a
    download destination, picking a temp name) can retry.
    """
    return os.open(path, _CREATE_FLAGS, mode)


def ensure_private_file(
    path: str | Path, *, mode: int = PRIVATE_FILE_MODE,
) -> Path:
    """Make sure *path* exists as an owner-only regular file.

    Used for files a library creates and manages itself — an SQLite
    database, whose connection must open the path directly. Creating the
    file here first means SQLite finds an existing file and inherits its
    mode for the database *and* its rollback journal, rather than
    creating both world-readable and leaving a chmod to catch up.

    A destination that already exists as a symbolic link (or anything
    other than a regular file) raises :class:`SecureWriteError` — the
    caller is about to hand the path to SQLite, which *would* follow it,
    so quietly declining to chmod is not enough. A regular file is
    tightened to *mode* on a best-effort basis (a database written by an
    older Worsaga is still holding course data), and only that step is
    non-fatal: a filesystem without permissions is no reason to refuse to
    open the cache.
    """
    target = Path(path)
    ensure_private_dir(target.parent)
    try:
        os.close(open_new_private_file(target, mode=mode))
        return target
    except FileExistsError:
        pass
    except OSError:
        # Windows reports EACCES rather than EEXIST when the path is an
        # existing directory. Let the destination check give the accurate
        # answer first; if it finds nothing wrong, the original error is
        # the real one.
        _refuse_unsafe_destination(target)
        raise
    _refuse_unsafe_destination(target)
    try:
        os.chmod(target, mode)
    except OSError:
        pass
    return target


def write_private_file(
    path: str | Path,
    data: bytes | str,
    *,
    mode: int = PRIVATE_FILE_MODE,
) -> Path:
    """Write *data* to *path* atomically, owner-only, and return the path.

    Text is encoded UTF-8 and written byte-for-byte (no newline
    translation, so a file written on Windows reads back identically
    everywhere). The bytes land in a private sibling temp file that is
    fsynced and then renamed over the destination, so the destination is
    either the old content or the new content and never a mixture.

    Raises :class:`SecureWriteError` if the destination exists as
    anything other than a regular file — a symlink there would otherwise
    redirect a credentials write to a location the user did not choose.
    """
    dest = Path(path)
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    _refuse_unsafe_destination(dest)
    ensure_private_dir(dest.parent)

    fd, tmp_path = _create_private_temp(dest, mode)
    try:
        try:
            handle = os.fdopen(fd, "wb")
        except BaseException:
            # fdopen did not take ownership of the descriptor, so this is
            # the only place left that can close it.
            os.close(fd)
            raise
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, dest)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    _fsync_directory(dest.parent)
    return dest


def child_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment for a child process with secrets removed.

    Everything else is kept: ``schtasks``, ``launchctl``, ``systemctl``,
    ``powershell``, and ``notify-send`` all need the ordinary
    environment (``PATH``, ``SystemRoot``, ``DISPLAY``,
    ``DBUS_SESSION_BUS_ADDRESS``) to work at all. Only the variables in
    :data:`SECRET_ENV_VARS` are dropped, because no Worsaga child process
    has any use for the Moodle token.
    """
    source = os.environ if env is None else env
    return {
        name: value
        for name, value in source.items()
        if name not in SECRET_ENV_VARS
    }


def _refuse_unsafe_destination(dest: Path) -> None:
    """Raise unless *dest* is absent or an ordinary regular file."""
    try:
        info = os.lstat(dest)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SecureWriteError(
            f"cannot inspect '{dest}' before writing it: {exc}"
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise SecureWriteError(
            f"refusing to write '{dest}': it is a symbolic link, and "
            "following it would write Worsaga's private data to a "
            "location you did not choose. Remove the link (or point "
            "Worsaga at the real path) and try again."
        )
    if not stat.S_ISREG(info.st_mode):
        raise SecureWriteError(
            f"refusing to write '{dest}': it exists and is not a regular "
            "file."
        )


def _fsync_directory(directory: Path) -> None:
    """Best-effort fsync of the directory holding a just-renamed file.

    Without it the *rename* can still be lost to a power failure even
    though the file's own bytes reached the disk. Plenty of platforms
    refuse to open a directory this way — Windows has no equivalent at
    all, and some filesystems return EINVAL — so every failure is
    ignored: this is a durability improvement, never a correctness
    requirement, and the atomicity guarantee does not depend on it.
    """
    try:
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _create_private_temp(dest: Path, mode: int) -> tuple[int, Path]:
    """Create a private temp file beside *dest*; return ``(fd, path)``.

    A sibling (not the system temp directory) so the later
    ``os.replace`` stays on one filesystem and therefore stays atomic.
    """
    last_error: OSError | None = None
    for _ in range(_TEMP_ATTEMPTS):
        candidate = dest.with_name(f"{dest.name}.{secrets.token_hex(6)}.tmp")
        try:
            return open_new_private_file(candidate, mode=mode), candidate
        except FileExistsError as exc:
            last_error = exc
            continue
    raise SecureWriteError(
        f"could not create a private temporary file next to '{dest}'"
    ) from last_error


__all__ = [
    "PRIVATE_DIR_MODE",
    "PRIVATE_FILE_MODE",
    "SECRET_ENV_VARS",
    "SecureWriteError",
    "child_env",
    "ensure_private_dir",
    "ensure_private_file",
    "open_new_private_file",
    "write_private_file",
]
