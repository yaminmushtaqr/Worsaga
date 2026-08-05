"""Tests for the owner-only file and directory primitives.

POSIX modes cannot be observed on Windows, so the permission tests come
in pairs: one asserts the real mode on POSIX (skipped elsewhere), and one
asserts the mode Worsaga *asks* for by recording the ``os.open`` /
``os.mkdir`` arguments, which is checkable on every platform.
"""

import os
import stat

import pytest

from worsaga import secureio
from worsaga.secureio import (
    PRIVATE_DIR_MODE,
    PRIVATE_FILE_MODE,
    SecureWriteError,
    child_env,
    ensure_private_dir,
    ensure_private_file,
    open_new_private_file,
    write_private_file,
)

posix_only = pytest.mark.skipif(
    os.name == "nt", reason="POSIX permissions are not applicable on Windows"
)


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


class _OpenRecorder:
    """Record the mode of every *creating* os.open the module performs.

    Non-creating opens are ignored: the directory fsync opens a directory
    read-only, where the mode argument is meaningless.
    """

    def __init__(self, monkeypatch, module=secureio):
        self.modes = []
        real_open = os.open

        def spy(path, flags, mode=0o777, **kwargs):
            if flags & os.O_CREAT:
                self.modes.append(mode)
            return real_open(path, flags, mode, **kwargs)

        monkeypatch.setattr(module.os, "open", spy)


class TestWritePrivateFile:
    def test_writes_text_and_returns_path(self, tmp_path):
        dest = tmp_path / "sub" / "creds.json"
        result = write_private_file(dest, '{"a": 1}\n')
        assert result == dest
        assert dest.read_text(encoding="utf-8") == '{"a": 1}\n'

    def test_writes_bytes_verbatim(self, tmp_path):
        dest = tmp_path / "raw.bin"
        write_private_file(dest, b"\x00\x01\n\x02")
        assert dest.read_bytes() == b"\x00\x01\n\x02"

    def test_newlines_are_not_translated(self, tmp_path):
        # Windows regression guard: a descriptor opened in text mode
        # would turn every \n into \r\n and corrupt the byte stream.
        dest = tmp_path / "lines.txt"
        write_private_file(dest, "one\ntwo\n")
        assert dest.read_bytes() == b"one\ntwo\n"

    def test_replaces_existing_content_atomically(self, tmp_path):
        dest = tmp_path / "config.json"
        dest.write_text("old", encoding="utf-8")
        write_private_file(dest, "new")
        assert dest.read_text(encoding="utf-8") == "new"
        # No temp debris beside the destination.
        assert [p.name for p in tmp_path.iterdir()] == ["config.json"]

    def test_failed_write_leaves_no_temp_file(self, tmp_path, monkeypatch):
        dest = tmp_path / "config.json"
        dest.write_text("old", encoding="utf-8")
        monkeypatch.setattr(
            secureio.os, "replace",
            lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")),
        )
        with pytest.raises(PermissionError):
            write_private_file(dest, "new")
        assert dest.read_text(encoding="utf-8") == "old"
        assert [p.name for p in tmp_path.iterdir()] == ["config.json"]

    def test_requests_owner_only_mode(self, tmp_path, monkeypatch):
        recorder = _OpenRecorder(monkeypatch)
        write_private_file(tmp_path / "config.json", "x")
        assert recorder.modes == [PRIVATE_FILE_MODE]

    def test_fdopen_failure_closes_the_descriptor_and_cleans_up(
        self, tmp_path, monkeypatch,
    ):
        """fdopen does not take ownership when it raises, so the raw
        descriptor would leak for the life of the process."""
        closed = []
        real_close = os.close
        monkeypatch.setattr(
            secureio.os, "close",
            lambda fd: (closed.append(fd), real_close(fd))[1],
        )
        monkeypatch.setattr(
            secureio.os, "fdopen",
            lambda *a, **k: (_ for _ in ()).throw(OSError("no memory")),
        )
        dest = tmp_path / "config.json"
        with pytest.raises(OSError):
            write_private_file(dest, "x")
        assert len(closed) == 1
        # And no temp debris survives the failure.
        assert list(tmp_path.iterdir()) == []

    def test_directory_is_fsynced_after_the_rename(
        self, tmp_path, monkeypatch,
    ):
        """Durability of the rename itself, where the platform allows it."""
        synced = []
        real_fsync = os.fsync
        monkeypatch.setattr(
            secureio.os, "fsync",
            lambda fd: (synced.append(fd), real_fsync(fd))[1],
        )
        write_private_file(tmp_path / "config.json", "x")
        # Always the file; the directory too on platforms that permit it.
        assert len(synced) in (1, 2)

    def test_directory_fsync_never_raises(self, tmp_path, monkeypatch):
        """Windows cannot open a directory this way and some filesystems
        return EINVAL; neither may break a successful write."""
        secureio._fsync_directory(tmp_path / "does-not-exist")
        monkeypatch.setattr(
            secureio.os, "fsync",
            lambda fd: (_ for _ in ()).throw(OSError("EINVAL")),
        )
        secureio._fsync_directory(tmp_path)  # must not propagate

    @posix_only
    def test_file_is_owner_only_on_posix(self, tmp_path):
        dest = write_private_file(tmp_path / "config.json", "x")
        assert _mode(dest) == 0o600

    @posix_only
    def test_replacing_a_loose_file_tightens_it(self, tmp_path):
        # The atomic rename carries the temp file's private mode, so a
        # file left world-readable by an older release is fixed by the
        # next write without a separate chmod.
        dest = tmp_path / "config.json"
        dest.write_text("old", encoding="utf-8")
        dest.chmod(0o644)
        write_private_file(dest, "new")
        assert _mode(dest) == 0o600

    def test_refuses_a_symlink_destination(self, tmp_path):
        real = tmp_path / "elsewhere.json"
        real.write_text("original", encoding="utf-8")
        link = tmp_path / "config.json"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            pytest.skip("this environment cannot create symbolic links")
        with pytest.raises(SecureWriteError) as exc:
            write_private_file(link, "secret")
        assert "symbolic link" in str(exc.value)
        assert real.read_text(encoding="utf-8") == "original"

    def test_refuses_a_symlink_destination_via_lstat(
        self, tmp_path, monkeypatch,
    ):
        # Same branch without needing symlink privileges: lstat reports a
        # link, and nothing is written.
        dest = tmp_path / "config.json"
        real_lstat = os.lstat

        class _LinkStat:
            st_mode = stat.S_IFLNK | 0o777

        monkeypatch.setattr(
            secureio.os, "lstat",
            lambda path: _LinkStat() if str(path) == str(dest)
            else real_lstat(path),
        )
        with pytest.raises(SecureWriteError) as exc:
            write_private_file(dest, "secret")
        assert "symbolic link" in str(exc.value)
        assert not dest.exists()

    def test_refuses_a_directory_destination(self, tmp_path):
        dest = tmp_path / "config.json"
        dest.mkdir()
        with pytest.raises(SecureWriteError) as exc:
            write_private_file(dest, "secret")
        assert "not a regular file" in str(exc.value)

    def test_secure_write_error_is_an_oserror(self):
        # The CLI's top-level handler turns OSError into a one-line
        # 'Error:' instead of a traceback.
        assert issubclass(SecureWriteError, OSError)


class TestEnsurePrivateDir:
    def test_creates_nested_directories(self, tmp_path):
        target = tmp_path / "a" / "b" / "c"
        assert ensure_private_dir(target) == target
        assert target.is_dir()

    def test_requests_owner_only_mode_for_every_level(
        self, tmp_path, monkeypatch,
    ):
        modes = []
        real_mkdir = os.mkdir
        monkeypatch.setattr(
            secureio.os, "mkdir",
            lambda path, mode=0o777, **kw: (
                modes.append(mode), real_mkdir(path, mode, **kw),
            )[1],
        )
        ensure_private_dir(tmp_path / "a" / "b")
        # Both the intermediate and the leaf, not just the leaf, which is
        # all Path.mkdir(parents=True) would have applied the mode to.
        assert modes == [PRIVATE_DIR_MODE, PRIVATE_DIR_MODE]

    @posix_only
    def test_created_directories_are_owner_only_on_posix(self, tmp_path):
        target = ensure_private_dir(tmp_path / "a" / "b")
        assert _mode(target) == 0o700
        assert _mode(tmp_path / "a") == 0o700

    @posix_only
    def test_existing_directory_is_left_alone(self, tmp_path):
        existing = tmp_path / "shared"
        existing.mkdir()
        existing.chmod(0o755)
        ensure_private_dir(existing)
        assert _mode(existing) == 0o755

    def test_is_idempotent(self, tmp_path):
        target = tmp_path / "a" / "b"
        ensure_private_dir(target)
        assert ensure_private_dir(target) == target


class TestEnsurePrivateFile:
    def test_creates_an_empty_file(self, tmp_path):
        path = ensure_private_file(tmp_path / "data" / "cache.db")
        assert path.is_file()
        assert path.read_bytes() == b""

    def test_keeps_existing_content(self, tmp_path):
        path = tmp_path / "cache.db"
        path.write_bytes(b"sqlite")
        ensure_private_file(path)
        assert path.read_bytes() == b"sqlite"

    def test_requests_owner_only_mode(self, tmp_path, monkeypatch):
        recorder = _OpenRecorder(monkeypatch)
        ensure_private_file(tmp_path / "cache.db")
        assert recorder.modes == [PRIVATE_FILE_MODE]

    @posix_only
    def test_new_file_is_owner_only_on_posix(self, tmp_path):
        path = ensure_private_file(tmp_path / "cache.db")
        assert _mode(path) == 0o600

    @posix_only
    def test_existing_loose_file_is_tightened(self, tmp_path):
        path = tmp_path / "cache.db"
        path.write_bytes(b"sqlite")
        path.chmod(0o644)
        ensure_private_file(path)
        assert _mode(path) == 0o600

    def test_existing_file_is_chmodded_to_owner_only(
        self, tmp_path, monkeypatch,
    ):
        # Platform-independent view of the tighten-on-open branch: what
        # matters is that 0600 is requested for the existing database.
        path = tmp_path / "cache.db"
        path.write_bytes(b"sqlite")
        calls = []
        monkeypatch.setattr(
            secureio.os, "chmod", lambda p, mode: calls.append((str(p), mode)),
        )
        ensure_private_file(path)
        assert calls == [(str(path), PRIVATE_FILE_MODE)]

    def test_refuses_a_symlinked_destination(self, tmp_path):
        """Declining to chmod is not enough: the caller hands this path
        to SQLite, which *would* follow the link."""
        real = tmp_path / "elsewhere.db"
        real.write_bytes(b"sqlite")
        link = tmp_path / "cache.db"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            pytest.skip("this environment cannot create symbolic links")
        with pytest.raises(SecureWriteError) as exc:
            ensure_private_file(link)
        assert "symbolic link" in str(exc.value)

    def test_refuses_a_symlinked_destination_via_lstat(
        self, tmp_path, monkeypatch,
    ):
        path = tmp_path / "cache.db"
        path.write_bytes(b"sqlite")
        chmods = []
        monkeypatch.setattr(
            secureio.os, "chmod", lambda p, mode: chmods.append(p),
        )

        class _LinkStat:
            st_mode = stat.S_IFLNK | 0o777

        monkeypatch.setattr(secureio.os, "lstat", lambda p: _LinkStat())
        with pytest.raises(SecureWriteError):
            ensure_private_file(path)
        assert chmods == []

    def test_refuses_a_directory_destination(self, tmp_path):
        path = tmp_path / "cache.db"
        path.mkdir()
        with pytest.raises(SecureWriteError) as exc:
            ensure_private_file(path)
        assert "not a regular file" in str(exc.value)

    def test_unchmodable_file_is_not_fatal(self, tmp_path, monkeypatch):
        path = tmp_path / "cache.db"
        path.write_bytes(b"sqlite")
        monkeypatch.setattr(
            secureio.os, "chmod",
            lambda *a, **k: (_ for _ in ()).throw(PermissionError("nope")),
        )
        assert ensure_private_file(path) == path


class TestOpenNewPrivateFile:
    def test_creates_exclusively(self, tmp_path):
        path = tmp_path / "placeholder"
        os.close(open_new_private_file(path))
        assert path.is_file()
        with pytest.raises(FileExistsError):
            open_new_private_file(path)

    @posix_only
    def test_placeholder_is_owner_only_on_posix(self, tmp_path):
        path = tmp_path / "placeholder"
        os.close(open_new_private_file(path))
        assert _mode(path) == 0o600


class TestCreateFlags:
    def test_creation_is_exclusive_and_write_only(self):
        flags = secureio._CREATE_FLAGS
        assert flags & os.O_CREAT
        assert flags & os.O_EXCL
        assert flags & os.O_WRONLY

    def test_uses_every_hardening_flag_the_platform_offers(self):
        flags = secureio._CREATE_FLAGS
        for name in ("O_NOFOLLOW", "O_BINARY"):
            available = getattr(os, name, None)
            if available:
                assert flags & available, f"{name} is available but unused"


class TestChildEnv:
    def test_drops_the_token(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_TOKEN", "supersecret")
        env = child_env()
        assert "WORSAGA_TOKEN" not in env
        assert "supersecret" not in "".join(env.values())

    def test_keeps_everything_else(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_TOKEN", "supersecret")
        monkeypatch.setenv("WORSAGA_URL", "https://moodle.example.edu")
        env = child_env()
        # Everything except the token survives verbatim. (Windows
        # normalizes environment names to upper case in os.environ, so
        # the comparison is made against os.environ's own keys.)
        expected = {
            name: value for name, value in os.environ.items()
            if name != "WORSAGA_TOKEN"
        }
        assert env == expected
        # Spot-check the variables a scheduler or notifier needs to run.
        for name in ("PATH", "SYSTEMROOT", "HOME", "USERPROFILE", "DISPLAY"):
            if name in expected:
                assert env[name] == expected[name]
        assert env["WORSAGA_URL"] == "https://moodle.example.edu"

    def test_accepts_an_explicit_mapping(self):
        env = child_env({"WORSAGA_TOKEN": "x", "PATH": "/usr/bin"})
        assert env == {"PATH": "/usr/bin"}

    def test_returns_a_copy(self, monkeypatch):
        monkeypatch.setenv("WORSAGA_URL", "https://moodle.example.edu")
        env = child_env()
        env["WORSAGA_URL"] = "mutated"
        assert os.environ["WORSAGA_URL"] == "https://moodle.example.edu"
