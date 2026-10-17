"""The catalogue store: atomic records, immutable versions, verified bundles, one lock."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from tests.unit.platform_support import add_version, experiment

from alhazen.experiment_platform import bundles
from alhazen.experiment_platform.errors import BundleError, InvalidRequest, NotFound, StoreError
from alhazen.experiment_platform.store import FileStore


@pytest.fixture()
def store(tmp_path: Path) -> FileStore:
    return FileStore(tmp_path / "store", create=True)


def test_a_new_store_is_private_and_reopens(tmp_path: Path, store: FileStore):
    import os

    if os.name == "posix":
        assert (store.root.stat().st_mode & 0o777) == 0o700
    assert FileStore(store.root).listings() == {}


def test_a_folder_that_is_not_a_store_is_refused(tmp_path: Path):
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "notes.txt").write_text("x")
    with pytest.raises(StoreError, match="not empty"):
        FileStore(tmp_path / "other", create=True)
    with pytest.raises(StoreError, match="not an Experiment Platform store"):
        FileStore(tmp_path / "other")


def test_a_newer_store_format_is_refused(store: FileStore):
    (store.root / "platform-store.json").write_text(
        json.dumps({"schema": "alhazen-experiment-platform-store", "version": 99})
    )
    with pytest.raises(StoreError, match="Upgrade alhazen"):
        FileStore(store.root)


def test_versions_are_written_once(store: FileStore):
    bid = add_version(store, "lab", "dots", experiment())
    record = store.version("lab", "dots", bid)
    assert store.put_version("lab", "dots", bid, record) is False  # same content: no-op
    with pytest.raises(StoreError, match="never rewritten"):
        store.put_version("lab", "dots", bid, {**record, "docs": {"methods": "changed"}})


def test_a_tampered_bundle_is_refused_on_read(store: FileStore):
    bid = add_version(store, "lab", "dots", experiment())
    chunk = store.root / "bundles" / bid / "chunks" / "0000.json"
    entries = json.loads(chunk.read_text())
    entries[0][1] = entries[0][1] + "# injected\n"
    chunk.write_text(json.dumps(entries))
    with pytest.raises(BundleError, match="digest|id"):
        store.bundle(None, bid)


def test_ids_are_checked_before_they_become_paths(store: FileStore):
    with pytest.raises(InvalidRequest):
        store.listing("../etc", "dots")
    with pytest.raises(InvalidRequest):
        store.fork("lab", "../../x")
    with pytest.raises(NotFound):
        store.art("../platform-store.json")


def test_art_is_content_addressed(store: FileStore):
    name = store.put_art("dots", b"\x89PNG", ".png")
    assert name.startswith("dots-") and store.art(name) == b"\x89PNG"
    assert store.put_art("dots", b"\x89PNG", ".png") == name
    with pytest.raises(StoreError, match="PNG"):
        store.put_art("dots", b"x", ".exe")


def test_copying_a_bundle_to_a_user_verifies_it(store: FileStore):
    bid = add_version(store, "lab", "dots", experiment())
    assert store.copy_bundle(None, "u1", bid) is True
    assert store.copy_bundle(None, "u1", bid) is False
    assert [e[0] for e in store.bundle("u1", bid).entries()] == sorted(experiment())


def test_a_bundle_id_cannot_be_reused_for_other_files(store: FileStore):
    entries, excluded = bundles.split_blobs([("run.py", b"1", 0)])
    built = bundles.build_bundle(entries, excluded, "x")
    store.put_bundle(None, built)
    other = bundles.Bundle(built.id, dict(built.meta, sha256="0" * 64), built.chunks)
    with pytest.raises((StoreError, BundleError)):
        store.put_bundle(None, other)


def test_the_lock_serialises_read_modify_write(store: FileStore):
    store.put_fork("u1", "f1", {"count": 0})

    def bump() -> None:
        for _ in range(20):
            with store.lock():
                value = store.fork("u1", "f1")
                store.put_fork("u1", "f1", {"count": value["count"] + 1})

    threads = [threading.Thread(target=bump) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.fork("u1", "f1") == {"count": 120}


def test_backup_is_a_complete_copy(tmp_path: Path, store: FileStore):
    add_version(store, "lab", "dots", experiment())
    copy = store.backup(tmp_path / "backup")
    assert FileStore(copy).listings().keys() == {"lab"}
    with pytest.raises(StoreError, match="exists"):
        store.backup(tmp_path / "backup")
