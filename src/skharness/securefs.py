"""Small dirfd-relative filesystem primitives for controller-owned state.

Every configured root is walked from ``/`` one component at a time with
``O_DIRECTORY|O_NOFOLLOW``.  The resulting descriptor, rather than the lexical
path, is the authority for subsequent creates and opens.  Renaming or replacing
an ancestor therefore cannot redirect an operation.  Sensitive files are written
and fsync'd as anonymous ``O_TMPFILE`` inodes before exclusive publication;
durable append uses copy-on-write so hard links to an old inode never receive new
records.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
_FILE_NOFOLLOW = os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
_PR_GET_DUMPABLE = 3
_PR_SET_DUMPABLE = 4
_PROC_FD_LOCK = threading.RLock()


class SecurePathError(OSError):
    """A configured state path could not be securely anchored."""


@contextmanager
def _private_proc_fds():
    """Temporarily deny same-uid peers access to this process's procfs fds."""
    with _PROC_FD_LOCK:
        libc = ctypes.CDLL(None, use_errno=True)
        previous = libc.prctl(_PR_GET_DUMPABLE)
        if previous < 0 or libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0):
            error = ctypes.get_errno() or errno.EPERM
            raise SecurePathError(error, "cannot protect anonymous state file descriptors")
        try:
            yield
        finally:
            if libc.prctl(_PR_SET_DUMPABLE, previous, 0, 0, 0):
                error = ctypes.get_errno() or errno.EPERM
                raise SecurePathError(error, "cannot restore process dumpability")


def _absolute_parts(path: Path | str) -> tuple[Path, tuple[str, ...]]:
    raw = Path(path).expanduser()
    if ".." in raw.parts:
        raise SecurePathError(errno.EINVAL, "state path contains traversal", str(raw))
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    # Do not resolve(): following a symlink here would defeat the component walk.
    parts = tuple(part for part in raw.parts if part not in (raw.anchor, "", "."))
    return raw, parts


class SecureDir:
    """An owned directory held open by descriptor.

    Descriptors intentionally remain open for the owning harness/sink lifetime.
    ``/proc/<pid>/fd/<n>`` can then be handed to another local process as a stable
    path to the exact inode, without re-traversing replaceable ancestors.
    """

    def __init__(self, fd: int, lexical: Path) -> None:
        self.fd = fd
        self.lexical = lexical
        # Serialize copy-on-write publication within one controller.  The state
        # directory remains the cross-process authority: collisions and changed
        # inodes are rejected rather than merged or overwritten in place.
        self._write_lock = threading.Lock()

    @classmethod
    def anchor(cls, path: Path | str, *, create: bool = True, mode: int = 0o700) -> "SecureDir":
        lexical, parts = _absolute_parts(path)
        fd = os.open("/", _DIR_FLAGS)
        try:
            for index, component in enumerate(parts):
                final = index == len(parts) - 1
                try:
                    child = os.open(component, _DIR_FLAGS, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, mode if final else 0o700, dir_fd=fd)
                    os.fsync(fd)
                    child = os.open(component, _DIR_FLAGS, dir_fd=fd)
                except OSError as exc:
                    raise SecurePathError(
                        exc.errno or errno.EPERM,
                        f"unsafe state path component {component!r}: {exc.strerror or exc}",
                        str(lexical),
                    ) from exc
                info = os.fstat(child)
                if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid()):
                    os.close(child)
                    raise SecurePathError(
                        errno.EPERM,
                        f"state path component {component!r} has foreign ownership",
                        str(lexical),
                    )
                # A foreign-writable non-sticky ancestor permits replacement by
                # another uid. Root-owned sticky directories (notably /tmp) are
                # valid anchors because sticky semantics prevent such replacement.
                permissions = stat.S_IMODE(info.st_mode)
                if (
                    info.st_uid != os.geteuid()
                    and permissions & 0o022
                    and not permissions & stat.S_ISVTX
                ):
                    os.close(child)
                    raise SecurePathError(
                        errno.EPERM,
                        f"state path component {component!r} is replaceable",
                        str(lexical),
                    )
                os.close(fd)
                fd = child
            result = cls(fd, lexical)
            result._verify_owned_directory(mode=mode)
            return result
        except Exception:
            os.close(fd)
            raise

    def _verify_owned_directory(self, *, mode: int = 0o700) -> None:
        info = os.fstat(self.fd)
        if not stat.S_ISDIR(info.st_mode):
            raise SecurePathError(
                errno.ENOTDIR, "state root is not a directory", str(self.lexical)
            )
        if info.st_uid != os.geteuid():
            raise SecurePathError(
                errno.EPERM, "state root is not owned by this controller", str(self.lexical)
            )
        # The descriptor identifies the inode, so tightening mode cannot be raced
        # through a swapped lexical path.
        if stat.S_IMODE(info.st_mode) != mode:
            os.fchmod(self.fd, mode)
            os.fsync(self.fd)

    @property
    def proc_path(self) -> str:
        return f"/proc/{os.getpid()}/fd/{self.fd}"

    def child_proc_path(self, name: str) -> str:
        self._name(name)
        return f"{self.proc_path}/{name}"

    @staticmethod
    def _name(name: str) -> str:
        if not name or name in (".", "..") or "/" in name or "\x00" in name:
            raise SecurePathError(errno.EINVAL, "invalid state component", name)
        return name

    def stat(self, name: str):
        return os.stat(self._name(name), dir_fd=self.fd, follow_symlinks=False)

    def exists(self, name: str) -> bool:
        try:
            self.stat(name)
            return True
        except FileNotFoundError:
            return False

    def mkdir(self, name: str, *, exclusive: bool = False, mode: int = 0o700) -> "SecureDir":
        name = self._name(name)
        try:
            os.mkdir(name, mode, dir_fd=self.fd)
            os.fsync(self.fd)
        except FileExistsError:
            if exclusive:
                raise
        child = os.open(name, _DIR_FLAGS, dir_fd=self.fd)
        result = SecureDir(child, self.lexical / name)
        result._verify_owned_directory(mode=mode)
        return result

    def rmdir(self, name: str) -> None:
        os.rmdir(self._name(name), dir_fd=self.fd)
        os.fsync(self.fd)

    def unlink(self, name: str) -> None:
        os.unlink(self._name(name), dir_fd=self.fd)
        os.fsync(self.fd)

    @staticmethod
    def _verify_published_file(fd: int, *, mode: int, links: int = 1) -> None:
        """Validate an already published controller file without mutating it."""
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SecurePathError(errno.EINVAL, "controller state file is not regular")
        if info.st_uid != os.geteuid():
            raise SecurePathError(errno.EPERM, "controller state file has foreign owner")
        if info.st_nlink != links:
            raise SecurePathError(errno.EMLINK, "controller state file has unexpected hard links")
        if stat.S_IMODE(info.st_mode) != mode:
            raise SecurePathError(errno.EPERM, "controller state file has unsafe mode")

    @staticmethod
    def _verify_file(fd: int, *, mode: int) -> None:
        """Compatibility validation boundary for an existing final file."""
        SecureDir._verify_published_file(fd, mode=mode)

    @staticmethod
    def _verify_unlinked_file(fd: int, *, mode: int) -> None:
        """Validate an anonymous inode before any sensitive bytes are written."""
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SecurePathError(errno.EINVAL, "controller state file is not regular")
        if info.st_uid != os.geteuid():
            raise SecurePathError(errno.EPERM, "controller state file has foreign owner")
        if info.st_nlink != 0:
            raise SecurePathError(errno.EMLINK, "anonymous controller state file is linkable")
        if stat.S_IMODE(info.st_mode) != mode:
            raise SecurePathError(errno.EPERM, "controller state file has unsafe mode")

    def _new_unlinked_file(self, *, mode: int) -> int:
        """Create an inode with no directory entry.

        ``O_TMPFILE`` is deliberately required.  Falling back to a named empty
        temporary would restore the hard-link check/use gap this class exists to
        close, so unsupported filesystems fail closed.
        """
        tmpfile = getattr(os, "O_TMPFILE", 0)
        if not tmpfile:
            raise SecurePathError(errno.ENOTSUP, "anonymous state files are unsupported")
        try:
            fd = os.open(".", os.O_RDWR | os.O_CLOEXEC | tmpfile, mode, dir_fd=self.fd)
        except OSError as exc:
            raise SecurePathError(
                exc.errno or errno.ENOTSUP,
                f"anonymous state file create failed: {exc.strerror or exc}",
                str(self.lexical),
            ) from exc
        # Deliberately do not perform a user-space link-count check here.  The
        # kernel has just returned a fresh O_TMPFILE inode with no directory
        # entry; checking and then writing would itself recreate the former
        # verification/write scheduling boundary.  Metadata and link count are
        # checked only after bytes have been written and fsync'd, immediately
        # before publication.
        return fd

    @staticmethod
    def _write_all(fd: int, data: bytes, *, message: str) -> None:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(errno.EIO, message)
            view = view[written:]

    def _publish_exclusive(self, fd: int, name: str, *, mode: int) -> None:
        """Give a fully written anonymous inode its final name exactly once."""
        name = self._name(name)
        self._verify_unlinked_file(fd, mode=mode)
        # AT_EMPTY_PATH is not exposed by Python's os.link.  Following this proc
        # descriptor symlink invokes linkat on the held anonymous inode; the
        # destination remains dirfd-relative and O_EXCL-like (never overwritten).
        os.link(
            f"/proc/self/fd/{fd}",
            name,
            dst_dir_fd=self.fd,
            follow_symlinks=True,
        )
        os.fsync(self.fd)
        self._verify_published_file(fd, mode=mode)

    def create_unlinked_file(self, *, mode: int = 0o600) -> int:
        """Return an anonymous durable file for a live descriptor-only stream."""
        fd = self._new_unlinked_file(mode=mode)
        self._verify_unlinked_file(fd, mode=mode)
        os.fsync(fd)
        return fd

    def write_exclusive(self, name: str, data: bytes, *, mode: int = 0o600) -> int:
        """Durably write bytes before publishing their exclusive final name.

        Same-uid peers are denied access to this process's procfs descriptors
        from before inode creation until contents, mode, and final name are set.
        The returned descriptor is read-only, so no sensitive append can follow.
        """
        with self._write_lock, _private_proc_fds():
            fd = self._new_unlinked_file(mode=mode)
            try:
                self._write_all(fd, data, message="short controller-state write")
                os.fsync(fd)
                self._publish_exclusive(fd, name, mode=mode)
                read_fd = os.open(f"/proc/self/fd/{fd}", os.O_RDONLY | os.O_CLOEXEC)
                self._verify_published_file(read_fd, mode=mode)
                os.close(fd)
                return read_fd
            except Exception:
                os.close(fd)
                raise

    @staticmethod
    def _read_all(fd: int) -> bytes:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    def _same_published_file(self, name: str, fd: int, *, mode: int, links: int = 1) -> None:
        """Reject a replaced path or an unexpected hard link."""
        self._verify_published_file(fd, mode=mode, links=links)
        expected = os.fstat(fd)
        actual = self.stat(name)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise SecurePathError(errno.ESTALE, "controller state file was replaced")
        if actual.st_nlink != links:
            raise SecurePathError(errno.EMLINK, "controller state file has unexpected hard links")

    def append_durable(self, name: str, data: bytes, *, mode: int = 0o600) -> None:
        """Append privately, then publish by durable copy-on-write."""
        with _private_proc_fds():
            self._append_durable(name, data, mode=mode)

    def _append_durable(self, name: str, data: bytes, *, mode: int) -> None:
        """Append by durable copy-on-write publication, never in-place mutation.

        Before removing the old final name, a deterministic backup hard link is
        fsync'd.  The new inode is linked to the final name only after all old and
        new bytes are fsync'd.  Consequently a crash always leaves the old record
        at either the final or backup name, and a concurrent final-path insertion
        makes publication fail rather than overwrite it.  A hard link made to the
        old inode at any point can receive only the old bytes.
        """
        name = self._name(name)
        backup = self._name(f".{name}.old")
        with self._write_lock:
            # Recover only the unambiguous pre-publication failure state: final
            # and backup are the same singly useful old inode. A missing final or
            # differing inodes remains fail-closed for explicit operator review.
            if self.exists(backup):
                if not self.exists(name):
                    raise SecurePathError(errno.EEXIST, "audit copy-on-write backup exists")
                final_info = self.stat(name)
                backup_info = self.stat(backup)
                if (final_info.st_dev, final_info.st_ino) != (
                    backup_info.st_dev,
                    backup_info.st_ino,
                ):
                    raise SecurePathError(errno.EEXIST, "ambiguous audit copy-on-write backup")
                os.unlink(backup, dir_fd=self.fd)
                os.fsync(self.fd)
                if self.stat(name).st_nlink != 1:
                    raise SecurePathError(errno.EMLINK, "audit recovery found outside hard links")
            try:
                old_fd = os.open(name, os.O_RDONLY | _FILE_NOFOLLOW, dir_fd=self.fd)
            except FileNotFoundError:
                old_fd = -1

            if old_fd < 0:
                fd = self._new_unlinked_file(mode=mode)
                try:
                    self._write_all(fd, data, message="short audit write")
                    os.fsync(fd)
                    self._publish_exclusive(fd, name, mode=mode)
                finally:
                    os.close(fd)
                return

            try:
                # This retained boundary intentionally validates pre-existing
                # files.  No write to old_fd occurs after it.
                self._verify_file(old_fd, mode=mode)
                old_data = self._read_all(old_fd)
                replacement_fd = self._new_unlinked_file(mode=mode)
                try:
                    self._write_all(
                        replacement_fd,
                        old_data + data,
                        message="short audit copy-on-write",
                    )
                    os.fsync(replacement_fd)

                    # Preserve the old durable record before making final absent.
                    os.link(
                        f"/proc/self/fd/{old_fd}",
                        backup,
                        dst_dir_fd=self.fd,
                        follow_symlinks=True,
                    )
                    os.fsync(self.fd)
                    self._same_published_file(name, old_fd, mode=mode, links=2)
                    os.unlink(name, dir_fd=self.fd)
                    os.fsync(self.fd)

                    # A hard link added after _same_published_file is harmless:
                    # it aliases only old_fd, which is never written. Continue so
                    # the mandatory record can still be published.
                    # Exclusive publication rejects a concurrent path replacement.
                    self._publish_exclusive(replacement_fd, name, mode=mode)
                    os.unlink(backup, dir_fd=self.fd)
                    os.fsync(self.fd)
                    self._verify_published_file(replacement_fd, mode=mode)
                finally:
                    os.close(replacement_fd)
            finally:
                os.close(old_fd)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
