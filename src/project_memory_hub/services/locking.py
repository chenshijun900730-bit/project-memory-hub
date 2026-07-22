from __future__ import annotations

import errno
import fcntl
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal


@dataclass(frozen=True, slots=True)
class LockOutcome:
    acquired: bool
    status: Literal["acquired", "already_running"]


class ProcessLock:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @contextmanager
    def acquire(self, nonblocking: bool = True) -> Iterator[LockOutcome]:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._path, flags, 0o600)
        except OSError:
            raise PermissionError("reconcile lock rejected") from None
        acquired = False
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
            ):
                raise PermissionError("reconcile lock rejected")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                os.fchmod(descriptor, 0o600)
            operation = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
            try:
                fcntl.flock(descriptor, operation)
            except OSError as error:
                if nonblocking and error.errno in {errno.EACCES, errno.EAGAIN}:
                    yield LockOutcome(False, "already_running")
                    return
                raise
            acquired = True
            yield LockOutcome(True, "acquired")
        finally:
            try:
                if acquired:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    except OSError:
                        pass
            finally:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
