"""Keep Windows from sleeping while a trader streams.

A scheduled task set to wake the PC does not keep it awake: the idle timer puts
it back to sleep mid-run (on this laptop, 3 minutes on battery), and nothing is
watched while it sleeps. `SetThreadExecutionState` with ES_CONTINUOUS |
ES_SYSTEM_REQUIRED holds the system awake until the flag is cleared or the
process exits. The display may still turn off, and closing the lid still
sleeps -- that is the lid action, which no program can override.

A no-op off Windows.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _set(flags: int) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes

    try:
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except (AttributeError, OSError):
        return False


@contextmanager
def keep_awake() -> Iterator[bool]:
    """Hold the system awake for the block. Yields whether Windows accepted it."""
    ok = _set(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    try:
        yield ok
    finally:
        if ok:
            _set(ES_CONTINUOUS)
