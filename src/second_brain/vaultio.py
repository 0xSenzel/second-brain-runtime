"""Vault I/O primitives: atomic writes, the single-writer lock, and the git journal.

One process writes the vault. These three pieces are what let that hold up under a crash
(atomic writes), a second writer by mistake (the lock), and a bad day (the journal, which
makes every change a revertable commit without ever touching what the user has not
committed themselves).
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterable
from pathlib import Path

LOCK_FILE = ".second-brain/write.lock"

_GIT_IDENTITY = ("-c", "user.name=second-brain", "-c", "user.email=second-brain@localhost")


def atomic_write(path: Path, text: str) -> None:
    """Replace `path` with `text` so a reader never sees a half-written note.

    The temp file lives beside the target because os.replace is only atomic within one
    filesystem, and the fsync makes the bytes durable before the name flips.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def atomic_move(src: Path, dst: Path) -> None:
    """Move a file, creating parents, and refuse to overwrite anything.

    A silent overwrite is how an archive step would destroy an earlier archive, so a clash
    is an error the caller must resolve by choosing another name.
    """
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise FileExistsError(f"Refusing to overwrite {dst}")
    # link + unlink fails if dst appeared in the meantime, unlike os.replace.
    try:
        os.link(src, dst)
    except OSError:
        # Filesystems without hard links: fall back to a rename after the check above.
        os.rename(src, dst)
        return
    src.unlink()


class VaultLock:
    """Exclusive write lock on a vault, re-entrant within one thread.

    flock is per open file, so two threads that each open the lock file exclude each other
    too. Re-entrancy exists because a public write method calls other write helpers.
    """

    _local = threading.local()

    def __init__(self, vault: Path, timeout: float = 30):
        self.path = Path(vault) / LOCK_FILE
        self.timeout = timeout

    def _held(self) -> dict[str, list]:
        if not hasattr(self._local, "held"):
            self._local.held = {}
        return self._local.held

    def __enter__(self) -> VaultLock:
        held = self._held()
        key = str(self.path)
        if key in held:
            held[key][1] += 1
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")  # noqa: SIM115 - closed in __exit__
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise TimeoutError(
                        f"Could not take the vault write lock within {self.timeout}s; "
                        "another writer is running."
                    ) from None
                time.sleep(0.02)
        held[key] = [handle, 1]
        return self

    def __exit__(self, *exc: object) -> None:
        held = self._held()
        key = str(self.path)
        entry = held[key]
        entry[1] -= 1
        if entry[1] == 0:
            del held[key]
            try:
                fcntl.flock(entry[0], fcntl.LOCK_UN)
            finally:
                entry[0].close()


class GitJournal:
    """Record each vault write as a git commit of exactly the files it touched.

    Never pushes, never stages anything the caller did not name, and never raises: the
    note is already on disk, so a missing git binary must not turn a good write into an
    error. Problems come back as warnings instead.
    """

    def __init__(self, vault: Path):
        self.vault = Path(vault)

    def enabled(self) -> bool:
        if os.environ.get("SECOND_BRAIN_GIT_AUTOCOMMIT", "1") == "0":
            return False
        return (self.vault / ".git").exists()

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-c", f"safe.directory={self.vault}", *_GIT_IDENTITY, *args],
            cwd=self.vault,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def commit(self, message: str, paths: Iterable[Path]) -> list[str]:
        if not self.enabled():
            return []
        relative: list[str] = []
        for path in paths:
            path = Path(path)
            candidate = path if path.is_absolute() else self.vault / path
            try:
                relative.append(candidate.relative_to(self.vault).as_posix())
            except ValueError:
                return [f"git journal skipped a path outside the vault: {path}"]
        relative = list(dict.fromkeys(relative))
        if not relative:
            return []
        try:
            # `add -A -- <path>` stages creations, edits and deletions of tracked files.
            # A path that never existed and is not tracked makes it fail, so each one is
            # staged on its own and the failures are ignored.
            for rel in relative:
                self._git("add", "-A", "--", rel)
            changed = self._git("diff", "--cached", "--no-renames", "--name-only", "-z", "--", *relative)
            names = [name for name in changed.stdout.split("\0") if name]
            if not names:
                return []
            # `-- <paths>` limits the commit to these files even if the user has other
            # changes staged.
            result = self._git("commit", "-q", "-m", message, "--", *names)
            if result.returncode != 0:
                return [f"git commit failed: {(result.stderr or result.stdout).strip()}"]
        except (OSError, subprocess.SubprocessError) as error:
            return [f"git journal unavailable: {error}"]
        return []
