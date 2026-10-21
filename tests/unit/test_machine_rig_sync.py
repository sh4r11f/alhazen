"""Syncing this machine's rig configs with the platform (cli/machine_rig_sync.py), against
an in-memory platform that applies rigsync's rules, and end to end against a real local
platform over HTTP."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from alhazen.cli import machine_rig as mr
from alhazen.cli.machine_rig_sync import RigSync, rig_sync_view
from alhazen.experiment_platform import rigsync
from alhazen.experiment_platform.library import LibraryError, RigsUnsupported

RIG = Path(__file__).parents[2] / "examples/minimal_fixation/rig-sim.yaml"


class FakePlatform:
    """A platform's rig routes in memory: GET the record, POST a sync."""

    def __init__(self) -> None:
        self.record: dict | None = None
        self.now = 0
        self.down: Exception | None = None
        self.pushes: list[dict] = []

    def rig_record(self, mid: str) -> dict | None:
        if self.down:
            raise self.down
        return self.record

    def rig_sync(self, mid: str, push: dict) -> dict:
        if self.down:
            raise self.down
        self.now += 1
        self.pushes.append(push)
        self.record, results = rigsync.apply_push(self.record, mid, push, self.now)
        return {"record": self.record, "results": results}

    # The person, on the platform's pages.
    def edit(
        self, cid: str, text: str, name: str | None = None, based_on: str | None = None
    ) -> None:
        config = self.record["configs"].get(cid)
        self.record = rigsync.owner_put(
            self.record,
            self.record["id"],
            cid,
            name=name or config["name"],
            text=text,
            base=config["revision"] if config else 0,
            based_on=based_on or (config["sha256"] if config else None),
            now=self.now,
        )


class Source:
    label = "Lab platform"
    machine = "Booth 2"


def engine(tmp_path: Path, platform: FakePlatform | None) -> tuple[mr.MachineRigs, RigSync]:
    rigs = mr.MachineRigs(tmp_path / "state")
    sync = RigSync(
        rigs,
        (lambda: (Source(), platform)) if platform is not None else (lambda: None),
        templates=lambda: [{"id": "alhazen/lab", "label": "alhazen/lab", "text": "a: 1\n"}],
        schema=lambda: {"title": "RigConfig"},
        version="3.6.0",
    )
    return rigs, sync


def with_config(rigs: mr.MachineRigs, name: str = "booth") -> dict:
    return rigs.setup(name, RIG, shared=None, template={"label": "t"})


def edited(text: str, distance: float) -> str:
    return text.replace("distance_cm: 60.0", f"distance_cm: {distance}")


def test_without_a_connection_everything_stays_local(tmp_path):
    rigs, sync = engine(tmp_path, None)
    with_config(rigs)
    assert sync.sync()["state"] == "not-connected"
    config = rigs.status()["configs"][0]
    assert rig_sync_view(config, {}, sync)["state"] == "not-synced"


@pytest.mark.parametrize(
    ("error", "state"),
    [(RigsUnsupported("old platform"), "unsupported"), (LibraryError("unreachable"), "offline")],
)
def test_a_platform_that_cannot_sync_leaves_everything_local(tmp_path, error, state):
    platform = FakePlatform()
    platform.down = error
    rigs, sync = engine(tmp_path, platform)
    with_config(rigs)
    got = sync.sync()
    assert got["state"] == state and str(error) in got["error"]
    assert rigs.require().values["monitor"]["distance_cm"] == 60.0


def test_the_first_sync_sends_every_config_its_default_schema_and_templates(tmp_path):
    platform = FakePlatform()
    rigs, sync = engine(tmp_path, platform)
    booth = with_config(rigs)
    assert sync.sync()["state"] == "synced"
    record = platform.record
    assert record["id"] == rigs.machine_id() and record["label"] == "Booth 2"
    assert record["default"] == booth["id"] and record["alhazen"] == "3.6.0"
    assert record["configs"][booth["id"]]["running"]["sha256"] == booth["sha256"]
    assert record["rigSchema"] and "alhazen/lab" in record["templates"]
    entry = rigs.index()["configs"][booth["id"]]["sync"]
    assert entry == {"platform_revision": 1, "sha256": booth["sha256"]}
    # Nothing changed: the next sync writes nothing and sends no templates again.
    sync.sync()
    assert platform.pushes[-1]["writes"] == [] and "templates" not in platform.pushes[-1]


def test_a_change_here_is_sent_and_one_there_is_taken(tmp_path):
    platform = FakePlatform()
    rigs, sync = engine(tmp_path, platform)
    booth = with_config(rigs)
    sync.sync()
    rigs.save_text(booth["id"], edited(booth["text"], 61.0), booth["sha256"], by="test")
    sync.sync()
    assert platform.record["configs"][booth["id"]]["revision"] == 2
    assert "distance_cm: 61.0" in platform.record["configs"][booth["id"]]["text"]
    platform.edit(booth["id"], edited(booth["text"], 62.0))
    sync.sync()
    assert rigs.require().values["monitor"]["distance_cm"] == 62.0
    assert platform.record["configs"][booth["id"]]["state"] == "synced"
    assert rigs.history(booth["id"])["saves"][0]["by"] == "platform"


def test_a_platform_edit_that_cannot_run_is_refused_once(tmp_path):
    platform = FakePlatform()
    rigs, sync = engine(tmp_path, platform)
    booth = with_config(rigs)
    sync.sync()
    platform.edit(
        booth["id"],
        booth["text"] + "devices:\n  sync:\n    backend: simulated\n"
        "    event_lines:\n      FIX_ON: line0\n",
    )
    sync.sync()
    config = platform.record["configs"][booth["id"]]
    assert config["state"] == "rejected" and "names events" in config["rejection"]["reason"]
    assert config["running"]["sha256"] == booth["sha256"]  # what still runs here
    assert rigs.require().sha256 == booth["sha256"]
    view = rig_sync_view(
        rigs.status()["configs"][0], rigs.index()["configs"][booth["id"]]["sync"], sync
    )
    assert view["state"] == "rejected" and "names events" in view["rejected"]["reason"]
    sync.sync()
    assert platform.pushes[-1]["acks"] == []  # refused once, not every sync
    # A change made here afterwards replaces the refused draft.
    rigs.save_text(booth["id"], edited(booth["text"], 58.0), booth["sha256"], by="test")
    sync.sync()
    assert platform.record["configs"][booth["id"]]["state"] == "synced"
    assert "distance_cm: 58.0" in platform.record["configs"][booth["id"]]["text"]


def test_both_sides_changed_is_a_conflict_settled_here(tmp_path):
    platform = FakePlatform()
    rigs, sync = engine(tmp_path, platform)
    booth = with_config(rigs)
    sync.sync()
    platform.edit(booth["id"], edited(booth["text"], 70.0))
    rigs.save_text(booth["id"], edited(booth["text"], 50.0), booth["sha256"], by="test")
    sync.sync()
    config = platform.record["configs"][booth["id"]]
    assert (
        "distance_cm: 70.0" in config["text"] and "distance_cm: 50.0" in config["conflict"]["text"]
    )
    assert rigs.require().values["monitor"]["distance_cm"] == 50.0  # nothing overwritten here
    view = rig_sync_view(
        rigs.status()["configs"][0], rigs.index()["configs"][booth["id"]]["sync"], sync
    )
    assert view["state"] == "conflict" and "70.0" in view["conflict"]["platform_text"]
    sync.resolve(booth["id"], "platform")
    assert rigs.require().values["monitor"]["distance_cm"] == 70.0
    config = platform.record["configs"][booth["id"]]
    assert config["state"] == "synced" and config["conflict"] is None


def test_a_conflict_kept_here_overwrites_the_platforms_version(tmp_path):
    platform = FakePlatform()
    rigs, sync = engine(tmp_path, platform)
    booth = with_config(rigs)
    sync.sync()
    platform.edit(booth["id"], edited(booth["text"], 70.0))
    rigs.save_text(booth["id"], edited(booth["text"], 50.0), booth["sha256"], by="test")
    sync.sync()
    sync.resolve(booth["id"], "machine")
    config = platform.record["configs"][booth["id"]]
    assert "distance_cm: 50.0" in config["text"] and config["state"] == "synced"
    assert config["conflict"] is None


def test_a_conflict_settled_on_the_platform_is_taken_here(tmp_path):
    platform = FakePlatform()
    rigs, sync = engine(tmp_path, platform)
    booth = with_config(rigs)
    sync.sync()
    platform.edit(booth["id"], edited(booth["text"], 70.0))
    rigs.save_text(booth["id"], edited(booth["text"], 50.0), booth["sha256"], by="test")
    sync.sync()
    platform.record = rigsync.owner_resolve(
        platform.record, platform.record["id"], booth["id"], keep="platform", now=99
    )
    sync.sync()
    assert rigs.require().values["monitor"]["distance_cm"] == 70.0
    assert platform.record["configs"][booth["id"]]["state"] == "synced"


def test_configs_made_renamed_and_deleted_on_either_side(tmp_path):
    platform = FakePlatform()
    rigs, sync = engine(tmp_path, platform)
    booth = with_config(rigs)
    sync.sync()
    # Made on the platform: created here with its id.
    platform.edit("cnew0000001", RIG.read_text(), name="eyelink", based_on="")
    sync.sync()
    assert rigs.require("eyelink").id == "cnew0000001"
    assert platform.record["configs"]["cnew0000001"]["state"] == "synced"
    # A name already taken here is refused.
    platform.edit("cnew0000002", RIG.read_text(), name="spare", based_on="")
    rigs.setup("spare", RIG, shared=None, template={})
    sync.sync()
    assert platform.record["configs"]["cnew0000002"]["state"] == "rejected"
    # Renamed on the platform: renamed here.
    text = rigs.require("eyelink").text
    platform.edit("cnew0000001", text, name="tracker")
    sync.sync()
    assert rigs.require("tracker").id == "cnew0000001"
    # Default chosen on the platform: taken here.
    platform.record = rigsync.owner_default(
        platform.record,
        platform.record["id"],
        "cnew0000001",
        base=platform.record["defaultRev"],
        now=50,
    )
    sync.sync()
    assert rigs.default_id() == "cnew0000001"
    # Deleted on the platform: moved to trash here, and the record dropped.
    platform.record = rigsync.owner_delete(
        platform.record,
        platform.record["id"],
        booth["id"],
        base=platform.record["configs"][booth["id"]]["revision"],
        now=60,
    )
    sync.sync()
    assert booth["id"] not in rigs.index()["configs"]
    assert booth["id"] not in platform.record["configs"]
    # Deleted here: deleted there.
    tracker = rigs.require("tracker")
    spare = rigs.require("spare")
    sync.sync()
    removed = rigs.delete(spare.id, spare.sha256, by="test")
    sync.deleted_here(removed)
    sync.sync()
    assert spare.id not in platform.record["configs"] and tracker.id in platform.record["configs"]


def test_a_real_local_platform_end_to_end(tmp_path):
    from alhazen.experiment_platform.library import LibraryClient, LibrarySource
    from alhazen.experiment_platform.local import LocalPlatform
    from alhazen.experiment_platform.store import FileStore

    store = FileStore(tmp_path / "store", create=True)
    platform = LocalPlatform(store)
    platform.start()
    try:
        source = LibrarySource.from_url(platform.url, label="Local platform", machine="Booth 2")
        rigs = mr.MachineRigs(tmp_path / "state")
        sync = RigSync(rigs, lambda: (source, LibraryClient(source)), schema=lambda: {"x": 1})
        booth = with_config(rigs)
        assert sync.sync()["state"] == "synced", sync.state()
        record = platform.service and LibraryClient(source).rig_record(rigs.machine_id())
        assert record["configs"][booth["id"]]["sha256"] == booth["sha256"]
        values = yaml.safe_load(booth["text"])
        assert values["monitor"]["distance_cm"] == 60.0
    finally:
        platform.shutdown()
