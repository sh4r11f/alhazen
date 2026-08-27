"""Results bundles: an output directory that says how it was made.

The point of a bundle is answering "was this made from the run I think it
was" a year later, so what these pin is the manifest's ability to answer
that.
"""

from __future__ import annotations

import json

import pytest

from alhazen.analysis.results import MANIFEST_NAME, SCHEMA_VERSION, ResultsBundle


def read_manifest(bundle: ResultsBundle) -> dict:
    return json.loads((bundle.out_dir / MANIFEST_NAME).read_text())


class TestOutputDirectory:
    def test_the_directory_is_created(self, tmp_path):
        bundle = ResultsBundle(tmp_path / "nested" / "results")
        assert bundle.out_dir.is_dir()

    def test_an_existing_directory_is_reused(self, tmp_path):
        (tmp_path / "results").mkdir()
        (tmp_path / "results" / "kept.txt").write_text("still here")
        ResultsBundle(tmp_path / "results")
        assert (tmp_path / "results" / "kept.txt").exists()


class TestTables:
    def test_a_table_is_written_and_remembered(self, tmp_path):
        bundle = ResultsBundle(tmp_path / "results")
        path = bundle.write_table("rates.csv", [{"subject": "t01", "rate": 0.8}])
        assert path.read_text().splitlines() == ["subject,rate", "t01,0.8"]
        assert bundle.outputs == ["rates.csv"]

    def test_an_empty_result_still_writes_a_file(self, tmp_path):
        # Writing nothing would be indistinguishable from the analysis never
        # having run — which is the question a bundle exists to answer.
        bundle = ResultsBundle(tmp_path / "results")
        path = bundle.write_table("empty.csv", [])
        assert path.exists()
        assert path.read_text() == ""
        assert bundle.outputs == ["empty.csv"]


class TestInputs:
    def test_a_file_input_is_hashed_and_sized(self, tmp_path):
        source = tmp_path / "trials.csv"
        source.write_text("trial_index\n1\n")
        bundle = ResultsBundle(tmp_path / "results")

        bundle.add_input(source, role="trials")

        (entry,) = bundle.inputs
        assert entry["role"] == "trials"
        assert entry["bytes"] == source.stat().st_size
        assert len(entry["sha256"]) == 64

    def test_the_hash_changes_when_the_input_does(self, tmp_path):
        # "Was this made from the run I think it was" is otherwise a question
        # about filenames, and filenames get renamed.
        source = tmp_path / "trials.csv"
        source.write_text("a")
        first = ResultsBundle(tmp_path / "one")
        first.add_input(source)
        source.write_text("b")
        second = ResultsBundle(tmp_path / "two")
        second.add_input(source)

        assert first.inputs[0]["sha256"] != second.inputs[0]["sha256"]

    def test_a_directory_input_is_listed_rather_than_hashed(self, tmp_path):
        # Hashing a recording's gigabytes to identify it costs more than it
        # is worth.
        recording = tmp_path / "run_g0"
        recording.mkdir()
        (recording / "run_g0_t0.nidq.bin").write_bytes(b"\x00" * 16)
        (recording / "run_g0_t0.nidq.meta").write_text("niSampRate=25000\n")
        bundle = ResultsBundle(tmp_path / "results")

        bundle.add_input(recording, role="recording")

        (entry,) = bundle.inputs
        assert entry["kind"] == "directory"
        assert entry["contents"] == ["run_g0_t0.nidq.bin", "run_g0_t0.nidq.meta"]
        assert "sha256" not in entry

    def test_a_missing_input_is_recorded_as_a_directory_listing_of_nothing(self, tmp_path):
        bundle = ResultsBundle(tmp_path / "results")
        bundle.add_input(tmp_path / "not-here")
        assert bundle.inputs[0]["contents"] == []


class TestManifest:
    def test_the_manifest_records_everything_the_bundle_knows(self, tmp_path):
        source = tmp_path / "trials.csv"
        source.write_text("trial_index\n1\n")
        bundle = ResultsBundle(tmp_path / "results", parameters={"window_ms": 200})
        bundle.add_input(source, role="trials")
        bundle.write_table("rates.csv", [{"rate": 0.8}])

        bundle.write_manifest()

        manifest = read_manifest(bundle)
        assert manifest["schema_version"] == SCHEMA_VERSION
        assert manifest["parameters"] == {"window_ms": 200}
        assert manifest["outputs"] == ["rates.csv"]
        assert manifest["inputs"][0]["role"] == "trials"

    def test_the_version_that_made_it_is_recorded(self, tmp_path):
        from alhazen.version import get_version

        bundle = ResultsBundle(tmp_path / "results")
        bundle.write_manifest()

        assert read_manifest(bundle)["alhazen_version"] == get_version()

    def test_the_written_timestamp_is_utc_and_parseable(self, tmp_path):
        from datetime import datetime

        bundle = ResultsBundle(tmp_path / "results")
        bundle.write_manifest()

        written = datetime.fromisoformat(read_manifest(bundle)["written"])
        assert written.tzinfo is not None

    def test_rewriting_the_manifest_reflects_later_outputs(self, tmp_path):
        bundle = ResultsBundle(tmp_path / "results")
        bundle.write_manifest()
        bundle.write_table("late.csv", [{"a": 1}])
        bundle.write_manifest()

        assert read_manifest(bundle)["outputs"] == ["late.csv"]


class TestExportedFromThePackage:
    def test_it_is_part_of_the_public_analysis_api(self):
        import alhazen.analysis as analysis

        assert analysis.ResultsBundle is ResultsBundle
        assert "ResultsBundle" in analysis.__all__


@pytest.mark.parametrize("rows", [[{"a": 1}], []])
def test_a_bundle_round_trips_end_to_end(tmp_path, rows):
    source = tmp_path / "in.csv"
    source.write_text("x\n1\n")
    bundle = ResultsBundle(tmp_path / "results", parameters={"n": len(rows)})
    bundle.add_input(source)
    bundle.write_table("out.csv", rows)
    manifest_path = bundle.write_manifest()

    reread = json.loads(manifest_path.read_text())
    assert reread["outputs"] == ["out.csv"]
    assert reread["parameters"]["n"] == len(rows)
