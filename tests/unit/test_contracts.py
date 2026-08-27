"""The compatibility contracts, pinned against a recorded baseline.

CHANGELOG.md promises three things that live on disk and therefore outlast any
one version of alhazen. A promise nothing checks is folklore, and this file is
what turns these three into gates — the same way `lint-imports` does for the
layering contract:

1. **RNG streams are append-only.** `spawn_streams` splits the session seed by
   position in `STREAMS`, so removing or reordering a name changes what every
   past seed produces. A study that re-runs its own seeds to reproduce a figure
   would get different trials and never be told why.
2. **Reserved events may be added, never removed.** An analysis reads these
   names out of data recorded years earlier.
3. **The run-directory layout is fixed within a major version.** Every script
   anyone has written to find a run's trials file depends on the name.

Also pinned: the on-disk schema version numbers, which may only ever go up. A
decrement would make new code claim to write an older format than it does, and
the readers' compatibility checks would wave it through.

**Updating the baseline.** Adding a stream, an event or a schema bump is
allowed and expected — edit `tests/fixtures/contracts.json` in the same commit
as the change, and say so in CHANGELOG.md. If a test here fails on something
you meant to *remove*, that is the contract working: it needs a major version
and a migration. See docs/versioning.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alhazen.analysis import results
from alhazen.core.events import RESERVED_EVENTS
from alhazen.core.rng import STREAMS, spawn_streams
from alhazen.data import manifest
from alhazen.data.paths import SessionPaths
from alhazen.devices import recording
from alhazen.scenes import model
from alhazen.session import database
from alhazen.training import state

BASELINE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "contracts.json").read_text(encoding="utf-8")
)

# Where each on-disk schema version is declared. Kept here rather than in the
# JSON because the baseline records *numbers*; this records where to look when
# one of them needs bumping.
SCHEMA_VERSIONS = {
    "database": database.SCHEMA_VERSION,
    "results_bundle": results.SCHEMA_VERSION,
    "run_manifest": manifest.MANIFEST_SCHEMA_VERSION,
    "recording_pointer": recording.POINTER_SCHEMA_VERSION,
    "training_state": state.SCHEMA_VERSION,
    "scene_format": model.SUPPORTED_VERSION,
}


class TestRngStreams:
    def test_the_recorded_streams_are_still_a_prefix(self):
        # A prefix, not a subset: position is what SeedSequence.spawn splits
        # on, so inserting a name anywhere but the end silently re-rolls every
        # stream after it.
        recorded = BASELINE["rng_streams"]
        assert list(STREAMS[: len(recorded)]) == recorded, (
            "STREAMS is append-only. Removing or reordering a stream changes what every "
            "existing seed produces. Append instead, and update tests/fixtures/contracts.json."
        )

    def test_every_recorded_stream_still_spawns(self):
        # The names are only half the contract; the other half is that a seed
        # actually yields a generator under each one.
        streams = spawn_streams(1234)
        for name in BASELINE["rng_streams"]:
            assert name in streams

    def test_one_seed_gives_one_set_of_streams(self):
        # Not a compatibility check but the reason the ordering one matters:
        # the same seed must produce the same numbers, run to run.
        first = spawn_streams(99)["task"].random(5).tolist()
        second = spawn_streams(99)["task"].random(5).tolist()
        assert first == second


class TestReservedEvents:
    def test_no_recorded_event_was_dropped(self):
        missing = set(BASELINE["reserved_events"]) - RESERVED_EVENTS
        assert not missing, (
            f"RESERVED_EVENTS lost {sorted(missing)}. An analysis reads these names out of "
            f"data recorded years ago; they may be added, never removed."
        )


class TestRunLayout:
    def test_the_filenames_are_unchanged(self, tmp_path):
        # Built through the real SessionPaths so this tracks the code that
        # writes runs, not a second copy of the names.
        paths = SessionPaths(run_dir=tmp_path, base="{base}")
        actual = {
            "trials": paths.trials_path.name,
            "events": paths.events_path.name,
            "frames": paths.frames_path.name,
            "paradigm": paths.paradigm_path.name,
            "snapshot": paths.snapshot_path.name,
            "manifest": paths.manifest_path.name,
            "log": paths.log_path.name,
            "figures": paths.figures_dir.name,
        }
        assert actual == BASELINE["run_layout"], (
            "The run-directory layout changes only in a major version, with a migration. "
            "Every script anyone wrote to find a run's files depends on these names."
        )


class TestSchemaVersions:
    @pytest.mark.parametrize("name", sorted(BASELINE["schema_versions"]))
    def test_no_schema_version_went_backwards(self, name):
        recorded = BASELINE["schema_versions"][name]
        assert SCHEMA_VERSIONS[name] >= recorded, (
            f"the {name} schema version went from {recorded} to {SCHEMA_VERSIONS[name]}. "
            f"Readers gate on this number; lowering it makes new files claim to be old ones."
        )

    def test_every_schema_version_is_recorded(self):
        # A new on-disk format that nothing pins is a contract by accident.
        # Adding it here is what makes it a contract on purpose.
        assert set(SCHEMA_VERSIONS) == set(BASELINE["schema_versions"]), (
            "a schema version was added or removed without updating tests/fixtures/contracts.json"
        )
