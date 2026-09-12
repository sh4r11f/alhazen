"""The written record of a checkout: is what it says true, and is it there?

These run the *shipped* rehearsal config — `examples/rig-rehearsal.yaml`,
every device configured, `--pulse` on — against a real ZeroMQ socket with the
simulated sorter on the other end, because a record that is only written when
hardware is attached is a record nobody has ever read. The thing under test is
the file: what it claims each device did, and that it is written on a failing
checkout as well as a passing one.

The failures are here for the same reason they are in the rehearsal doc: the
record of a rig that did not work is the one worth keeping, and a failing
device that silently dropped out of the file would be the worst possible way
to find that out.
"""

from __future__ import annotations

import re
import shlex
import threading
import time
from pathlib import Path

import pytest
import yaml

from alhazen.cli.main import main
from alhazen.config.loader import load_rig
from alhazen.session.checkout import build_record, differences, read_record
from alhazen.session.checks import check_rig
from alhazen.testing.sorter import SortedSpikePublisher, SorterSim

REHEARSAL_YAML = Path(__file__).parents[2] / "examples" / "rig-rehearsal.yaml"


def live_publisher(**overrides):
    """A publisher on a real socket, on a port nobody else is using."""
    pytest.importorskip("zmq")
    cfg = SorterSim(address="tcp://127.0.0.1:*", heartbeat_period_ms=20.0, **overrides)
    pub = SortedSpikePublisher(cfg)
    pub.bind()
    return pub


class Pumping:
    """Publishes in the background for as long as the block lasts."""

    def __init__(self, pub) -> None:
        self._pub = pub
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._pub.step(time.monotonic())
            time.sleep(0.005)

    def __enter__(self) -> Pumping:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)


def rehearsal_rig(tmp_path, address: str, timeout_ms: float = 1000.0):
    """The shipped rehearsal config, with only the two things a test may not
    take from it changed: where it writes, and a fixed port."""
    shipped = load_rig(REHEARSAL_YAML)
    spikes = shipped.devices.spikes.model_copy(
        update={"address": address, "heartbeat_timeout_ms": timeout_ms}
    )
    return shipped.model_copy(
        update={
            "data_root": tmp_path / "data",
            "devices": shipped.devices.model_copy(update={"spikes": spikes}),
        }
    )


def record_of(tmp_path, rig, pulse: bool = True):
    """Run the real check, write the real files, read the record back."""
    results = check_rig(rig, pulse=pulse)
    record, summary = build_record(REHEARSAL_YAML, results, pulse=pulse).write(
        tmp_path / "checkout.json"
    )
    return read_record(record), summary.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    """One clean checkout of the shipped rehearsal rig, run once: a real
    socket, a real sorter on the other end, --pulse, the files written and
    read back. Module-scoped because it waits on a live stream, and every
    assertion below is about the same run."""
    tmp_path = tmp_path_factory.mktemp("clean")
    pub = live_publisher()
    try:
        with Pumping(pub):
            time.sleep(0.1)  # let the first announcement go out
            return record_of(tmp_path, rehearsal_rig(tmp_path, pub.address))
    finally:
        pub.close()


class TestWhatTheRecordSaysEachDeviceDid:
    """One passing checkout, read back device by device."""

    def test_it_records_a_pass_for_every_device_the_framework_has(self, clean):
        record, _ = clean

        assert record["ok"] is True
        assert set(record["devices"]) == {
            "config",
            "monitor",
            "data_root",
            "eyetracker",
            "reward",
            "sync",
            "recording",
            "spikes",
        }

    def test_the_reward_pulse_is_commanded_and_measured_not_just_fired(self, clean):
        """The difference the record exists for: "fired one 50 ms pulse" is
        what was asked for, and the measured duration is what happened."""
        record, _ = clean
        reward = record["devices"]["reward"]["evidence"]

        assert reward["pulsed"] is True
        assert reward["commanded_ms"] == 50.0
        assert isinstance(reward["measured_ms"], float)
        # Which line it would have come out of, so the record can be checked
        # against the wiring without the config file in the other hand.
        assert (reward["device"], reward["channel"]) == ("Dev1", "ao0")
        # Honest about what a simulated backend did: nothing was played out,
        # so the measured duration is nowhere near the commanded one — and
        # that is the correct reading, not a fault.
        assert reward["simulated"] is True

    def test_every_sync_line_is_named_with_what_was_sent_on_it(self, clean):
        record, _ = clean
        lines = record["devices"]["sync"]["evidence"]["lines"]

        assert [line["line"] for line in lines] == [
            "Dev1/port0/line0",
            "Dev1/port0/line1",
            "Dev1/port0/line2",
        ]
        assert [line["events"] for line in lines] == [["TRIAL_START"], ["STIM_ON"], ["REWARD"]]
        for line in lines:
            assert line["pulsed"] is True
            assert line["commanded_ms"] == 2.0
            assert isinstance(line["measured_ms"], float)

    def test_it_records_what_the_recorder_returned(self, clean):
        record, _ = clean
        recording = record["devices"]["recording"]["evidence"]

        # None is the recorder saying nothing is wrong — kept verbatim,
        # because "OK" alone does not say which directory it looked at.
        assert recording["returned"] is None
        # By parts, not by suffix: the record writes the path as this machine
        # spells it, and the rig that matters most here is the Windows one.
        assert Path(recording["data_dir"]).parts[-2:] == ("data-rehearsal", "recording")
        # And the pairing this exists to expose: the simulated recorder
        # reported nothing wrong about a directory that is not there. On the
        # lab rig the spikeglx backend would have said so; here the OK is the
        # backend's, not the share's, and only the record shows the
        # difference.
        assert recording["exists"] is False

    def test_it_records_the_tracker_and_the_measured_spikes_lag(self, clean):
        record, _ = clean
        tracker = record["devices"]["eyetracker"]["evidence"]
        spikes = record["devices"]["spikes"]["evidence"]

        # mouse_sim is never constructed, so the record says so rather than
        # claiming a connection: None is "not asked", not "failed".
        assert tracker["backend"] == "mouse_sim" and tracker["connected"] is None
        assert spikes["publishing"] is True and spikes["units_announced"] is True
        assert spikes["units"] > 0
        assert isinstance(spikes["lag_ms"], float)
        assert spikes["address"].startswith("tcp://127.0.0.1:")

    def test_it_records_the_rig_file_the_revision_and_when(self, clean):
        """Without these three the numbers are unattributable: a measurement
        with no rig file, no code version and no date cannot be compared
        against anything."""
        record, _ = clean

        assert record["rig_file"].endswith("rig-rehearsal.yaml")
        assert record["provenance"]["alhazen_git_describe"]
        assert record["provenance"]["alhazen_version"]
        assert record["created"].endswith("+00:00")
        assert record["schema_version"] == 1
        assert record["pulse"] is True

    def test_the_summary_beside_it_is_readable_by_a_person(self, clean):
        record, summary = clean

        assert "pre-session checkout — PASS" in summary
        assert "pulse commanded 50.0 ms, measured" in summary
        assert "Dev1/port0/line1: sent 2.0 ms, measured" in summary
        assert "carries STIM_ON" in summary
        # The same sentence the console prints, for the same reason: a file
        # of eight OK lines must not read as a fully verified rig.
        assert "display: untested" in summary
        assert record["untested"]


class TestWhenADeviceFails:
    def test_a_failing_device_and_a_passing_one_are_both_in_the_record(self, tmp_path):
        """A silent sorter: nothing is on the endpoint at all. The record has
        to keep the failure *and* what the devices that worked did — the
        checkout of a broken rig is the one worth reading later."""
        pub = live_publisher(fault="silent")
        try:
            record, summary = record_of(tmp_path, rehearsal_rig(tmp_path, pub.address, 500.0))
        finally:
            pub.close()

        assert record["ok"] is False
        spikes = record["devices"]["spikes"]
        assert spikes["ok"] is False
        assert "is the real-time sorter running and publishing?" in spikes["detail"]
        # How far it got, not only that it stopped: nothing published at all.
        assert spikes["evidence"]["connected"] is True
        assert spikes["evidence"]["publishing"] is False
        assert spikes["evidence"]["units_announced"] is False
        assert spikes["evidence"]["lag_ms"] is None
        assert spikes["evidence"]["listened_s"] >= 0.5
        # And the devices that worked are still measured beside it.
        assert record["devices"]["reward"]["ok"] is True
        assert record["devices"]["reward"]["evidence"]["commanded_ms"] == 50.0
        assert record["devices"]["sync"]["evidence"]["lines"][0]["pulsed"] is True
        assert "pre-session checkout — FAIL" in summary

    def test_the_other_spikes_failure_is_a_different_record_not_the_same_one(self, tmp_path):
        """announce_once: the sorter IS alive and publishing, and never
        re-announces units. Distinguishing the two is the whole value of this
        check, so the record must distinguish them too — a reader comparing
        two failed checkouts learns nothing from a pair of identical files."""
        pub = live_publisher(fault="announce_once")
        try:
            with Pumping(pub):
                time.sleep(0.2)  # the single announcement goes out and is missed
                record, _ = record_of(tmp_path, rehearsal_rig(tmp_path, pub.address, 500.0))
        finally:
            pub.close()

        spikes = record["devices"]["spikes"]
        assert spikes["ok"] is False
        assert "never re-announced units" in spikes["detail"]
        assert spikes["evidence"]["publishing"] is True
        assert spikes["evidence"]["units_announced"] is False


class TestComparingTwoCheckouts:
    def test_a_record_read_back_compares_against_the_next_one(self, tmp_path):
        """Why the format is machine-readable: today's checkout held up
        against last week's, leaf by leaf, without knowing in advance which
        number was going to move."""
        rig = rehearsal_rig(tmp_path, "tcp://127.0.0.1:1")  # never reached; spikes will fail
        first = build_record("configs/rig-lab.yaml", check_rig(rig, pulse=True), pulse=True)
        before = read_record(first.write(tmp_path / "monday.json")[0])
        after = read_record(first.write(tmp_path / "tuesday.json")[0])
        # The rig was rewired between the two: one line lost its event.
        after["devices"]["sync"]["evidence"]["lines"][1]["events"] = []
        after["devices"]["spikes"]["evidence"]["lag_ms"] = 41.0

        changed = differences(before, after)

        assert "sync.evidence.lines[1].events: ['STIM_ON'] -> []" in changed
        assert "spikes.evidence.lag_ms: None -> 41.0" in changed
        assert differences(before, before) == []

    def test_records_from_different_schema_versions_refuse_to_be_compared(self, tmp_path):
        rig = rehearsal_rig(tmp_path, "tcp://127.0.0.1:1")
        record = build_record("configs/rig-lab.yaml", check_rig(rig), pulse=False).to_dict()
        older = {**record, "schema_version": 0}

        assert differences(older, record) == [
            "schema_version: 0 -> 1 "
            "(records from different schema versions are not comparable leaf by leaf)"
        ]


def cli_rig(tmp_path, **devices) -> Path:
    """A rig file for the CLI tests. Hand-built rather than the shipped
    example, because these are about the option and the exit code; what the
    shipped config records is the subject of the classes above."""
    path = tmp_path / "rig.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "monitor": {
                    "width_px": 800,
                    "height_px": 600,
                    "width_cm": 40,
                    "distance_cm": 57,
                    "refresh_rate_hz": 120,
                },
                "display": {"backend": "simulated"},
                "data_root": str(tmp_path / "data"),
                "devices": {
                    "reward": {"backend": "simulated", "device": "Dev1", "channel": "ao0"},
                    "sync": {
                        "backend": "simulated",
                        "pulse_ms": 2.0,
                        "event_lines": {"TRIAL_START": "Dev1/port0/line0"},
                    },
                    **devices,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


class TestTheCommandTheExperimenterTypes:
    """`--record` through the CLI, which is the only way anyone will use it."""

    def test_it_writes_both_files_and_says_where(self, tmp_path, capsys):
        destination = tmp_path / "checkouts" / "2026-09-12.json"

        code = main(
            ["check-rig", "--rig", str(cli_rig(tmp_path)), "--pulse", "--record", str(destination)]
        )

        out = capsys.readouterr().out
        assert code == 0
        assert destination.exists() and destination.with_suffix(".txt").exists()
        assert f"record:  {destination}" in out
        # The per-device lines are unchanged by the record: this adds
        # evidence, it does not replace what the experimenter reads.
        assert "OK   reward:" in out and "display: untested" in out
        assert read_record(destination)["devices"]["reward"]["evidence"]["measured_ms"] is not None

    def test_the_command_the_doc_gives_still_works(self, tmp_path, capsys):
        """The doc names one exact command. This runs *that* command — its
        own tokens, off the page — so it cannot rot into an option that no
        longer exists while every test here stays green."""
        doc = (Path(__file__).parents[2] / "docs" / "pre-session-checkout.md").read_text(
            encoding="utf-8"
        )
        commands = re.findall(r"^```bash\n(.*?)^```", doc, re.MULTILINE | re.DOTALL)
        typed = [
            line
            for block in commands
            for line in block.splitlines()
            if line.startswith("alhazen check-rig") and "--record" in line
        ]
        assert typed, "the checkout doc no longer gives a check-rig command with --record"
        argv = shlex.split(typed[0])[1:]
        # Two things belong to the rig and not to the command: which config,
        # and where the record lands. Everything else is typed as written.
        argv[argv.index("--rig") + 1] = str(cli_rig(tmp_path))
        argv[argv.index("--record") + 1] = str(tmp_path / "from-the-doc.json")

        code = main(argv)

        assert code == 0
        assert "--pulse" in argv  # the doc must not quietly stop firing hardware
        assert (tmp_path / "from-the-doc.json").exists()
        assert (tmp_path / "from-the-doc.txt").exists()

    def test_the_record_is_written_when_the_checkout_fails(self, tmp_path, capsys):
        """The case that decides whether this is worth anything: a FAIL still
        leaves the file, and the exit code is the one the checks decided."""
        share = tmp_path / "acquisition-host-share"  # never mounted
        rig = cli_rig(
            tmp_path, recording={"backend": "spikeglx", "data_dir": str(share), "run_glob": "*_g0"}
        )
        destination = tmp_path / "checkout.json"

        code = main(["check-rig", "--rig", str(rig), "--record", str(destination)])

        assert code == 1
        record = read_record(destination)
        assert record["ok"] is False
        # The failing device, with what the recorder actually returned...
        recording = record["devices"]["recording"]
        assert recording["ok"] is False
        assert "is not reachable" in recording["evidence"]["returned"]
        assert recording["evidence"]["exists"] is False
        # ...and a passing device beside it, still carrying its measurement.
        assert record["devices"]["sync"]["ok"] is True
        assert record["devices"]["sync"]["evidence"]["lines"][0]["line"] == "Dev1/port0/line0"
