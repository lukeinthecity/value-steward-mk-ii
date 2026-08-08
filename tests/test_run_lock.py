"""Tests for the single-instance file lock.

flock locks belong to the open file description, not the process, so a
second acquisition attempt from within this same test process correctly
contends against the first -- no subprocess needed to test this for real.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vs2.data.run_lock import AlreadyRunningError, single_instance


def test_acquires_and_releases_cleanly(tmp_path: Path) -> None:
    lock_path = tmp_path / "run.lock"
    with single_instance(lock_path):
        pass  # no error means the lock was acquired and released


def test_creates_the_lock_files_parent_directory(tmp_path: Path) -> None:
    lock_path = tmp_path / "nested" / "run.lock"
    with single_instance(lock_path):
        pass
    assert lock_path.exists()


def test_second_acquisition_while_first_is_held_fails_fast(tmp_path: Path) -> None:
    lock_path = tmp_path / "run.lock"
    with single_instance(lock_path):
        with pytest.raises(AlreadyRunningError, match="already holds"):
            with single_instance(lock_path):
                pass  # pragma: no cover -- must never be reached


def test_lock_is_available_again_after_the_first_holder_releases(tmp_path: Path) -> None:
    lock_path = tmp_path / "run.lock"
    with single_instance(lock_path):
        pass
    # Should not raise -- the first holder released on exit.
    with single_instance(lock_path):
        pass


def test_lock_is_released_even_if_the_held_block_raises(tmp_path: Path) -> None:
    lock_path = tmp_path / "run.lock"

    with pytest.raises(ValueError, match="boom"):
        with single_instance(lock_path):
            raise ValueError("boom")

    # If the finally-release didn't run, this would raise AlreadyRunningError.
    with single_instance(lock_path):
        pass


def test_two_different_lock_paths_do_not_contend(tmp_path: Path) -> None:
    with single_instance(tmp_path / "a.lock"):
        with single_instance(tmp_path / "b.lock"):
            pass  # different files, no contention
