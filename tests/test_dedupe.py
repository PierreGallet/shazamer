"""Sharing bytes between sets that hold the same audio.

Re-analysing a mix downloads it again and keeps both copies. Measured on the
live install: 325 MB of set audio, 144 MB of it two byte-identical pairs.
"""
import os

import pytest

from src.core.dedupe import deduplicate, file_digest, link_if_duplicate

pytestmark = pytest.mark.anyio


def _write(folder, name, content, when=None):
    path = folder / name
    path.write_bytes(content)
    if when is not None:
        os.utime(path, (when, when))
    return path


def test_identical_files_end_up_sharing_one_inode(tmp_path):
    a = _write(tmp_path, "a.webm", b"same" * 5000, when=1000)
    b = _write(tmp_path, "b.webm", b"same" * 5000, when=2000)
    c = _write(tmp_path, "c.webm", b"different" * 5000)

    linked, reclaimed = deduplicate(tmp_path)

    assert linked == 1
    assert reclaimed == 20000
    assert a.stat().st_ino == b.stat().st_ino, "the bytes are still duplicated"
    assert c.stat().st_ino != a.stat().st_ino
    # And nothing was lost: both names still read the same content.
    assert a.read_bytes() == b.read_bytes() == b"same" * 5000


def test_the_oldest_copy_is_the_one_kept(tmp_path):
    """So the surviving inode is the one other things already point at."""
    old = _write(tmp_path, "z.webm", b"x" * 100, when=1000)
    new = _write(tmp_path, "a.webm", b"x" * 100, when=9000)
    old_inode = old.stat().st_ino

    deduplicate(tmp_path)
    assert new.stat().st_ino == old_inode


def test_running_it_twice_changes_nothing(tmp_path):
    _write(tmp_path, "a.webm", b"y" * 100, when=1000)
    _write(tmp_path, "b.webm", b"y" * 100, when=2000)

    first = deduplicate(tmp_path)
    second = deduplicate(tmp_path)

    assert first == (1, 100)
    assert second == (0, 0), "already-shared files were counted again"


def test_deleting_one_name_leaves_the_other_readable(tmp_path):
    """The property hard links are chosen for.

    Pointing two rows at one path would make deleting one set silently empty
    another set's player. Here the kernel counts the references and the bytes
    go when the last name does.
    """
    a = _write(tmp_path, "a.webm", b"keep me" * 100, when=1000)
    b = _write(tmp_path, "b.webm", b"keep me" * 100, when=2000)
    deduplicate(tmp_path)

    a.unlink()
    assert b.exists()
    assert b.read_bytes() == b"keep me" * 100


def test_a_new_file_links_to_an_existing_twin(tmp_path):
    existing = _write(tmp_path, "old.webm", b"twin" * 500, when=1000)
    fresh = _write(tmp_path, "new.webm", b"twin" * 500, when=2000)

    assert link_if_duplicate(tmp_path, fresh) is True
    assert fresh.stat().st_ino == existing.stat().st_ino


def test_a_new_file_with_no_twin_is_left_alone(tmp_path):
    _write(tmp_path, "old.webm", b"one" * 500)
    fresh = _write(tmp_path, "new.webm", b"two" * 500)
    before = fresh.stat().st_ino

    assert link_if_duplicate(tmp_path, fresh) is False
    assert fresh.stat().st_ino == before
    assert fresh.read_bytes() == b"two" * 500


def test_a_file_that_cannot_be_read_is_not_a_crash(tmp_path):
    assert file_digest(tmp_path / "absent.webm") is None
    assert deduplicate(tmp_path / "absent") == (0, 0)


def test_a_half_finished_link_never_replaces_the_original(tmp_path,
                                                          monkeypatch):
    """A failure partway must leave the audio, not nothing.

    It is the thing this whole feature exists to preserve, so the link is
    built under a temporary name and renamed over the target.
    """
    import src.core.dedupe as dedupe

    _write(tmp_path, "a.webm", b"z" * 100, when=1000)
    b = _write(tmp_path, "b.webm", b"z" * 100, when=2000)

    def explode(*_args):
        raise OSError("no links today")

    monkeypatch.setattr(dedupe.os, "link", explode)
    linked, _ = deduplicate(tmp_path)

    assert linked == 0
    assert b.exists() and b.read_bytes() == b"z" * 100
    assert not list(tmp_path.glob("*.linking")), "left a temporary file behind"
