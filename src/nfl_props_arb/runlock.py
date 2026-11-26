"""One trader per log: a second process on the same log exits instead of running.

Two processes appending to one log on Windows can clobber each other's lines,
and a scheduled trigger can fire while a hand-started run of the same trader
is still going (2026-11-26: a one-off 30 h paper run across Thanksgiving, with
the task's 6 pm Thursday trigger due mid-run). The lock is an OS byte-range
lock on `<log>.lock`, released by the OS if the process dies, so a crash never
leaves a stale lock behind.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def lock_path(log_path: Path | str) -> Path:
    return Path(log_path).with_suffix(".lock")


@contextmanager
def single_instance(path: Path | str) -> Iterator[bool]:
    """Hold an exclusive lock on `path` for the block. Yields False if another holds it."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fh = p.open("a+")
    locked = False
    try:
        fh.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            yield False
            return
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        yield True
    finally:
        if locked:
            fh.seek(0)
            if sys.platform == "win32":
                import msvcrt

                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        fh.close()
