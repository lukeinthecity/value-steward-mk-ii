"""Prevent two invocations of run_daily from running at the same time.

Cron does not wait for a prior invocation to finish before starting the next
scheduled one -- each scheduled minute fires independently. run_daily runs in
a retry window (several ticks, 15 minutes apart) rather than at one precise
instant, specifically so a slow-to-publish daily bar gets more than one
chance. That same width is what makes overlap possible: if one invocation
runs long enough, the next tick starts while it is still going, and both can
pass the once-per-day guard before either has recorded that today is done.
See docs/CODE_CHECK_PLAYBOOK.md's "Known open findings" (as of 2026-08-08)
for the incident this closes.

`flock` locks belong to the open file description, not the process, so this
also correctly contends against a second lock attempt from within the same
process -- useful for testing without needing to fork a real subprocess.

If the process holding the lock dies for any reason, the OS releases the lock
automatically. There is no stale-lock file to find and delete by hand, unlike
a PID-file approach.
"""

from __future__ import annotations

import fcntl
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator

logger = logging.getLogger(__name__)


class AlreadyRunningError(RuntimeError):
    """Raised when another invocation already holds the lock."""


@contextmanager
def single_instance(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive, non-blocking lock for the duration of the block.

    Raises AlreadyRunningError immediately if another process (or another
    open file description in this one) already holds it -- never waits, so a
    second cron tick fails fast rather than queuing up behind the first.
    """

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle: IO[str] = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise AlreadyRunningError(
            f"another instance already holds the lock at {lock_path}"
        ) from exc

    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
