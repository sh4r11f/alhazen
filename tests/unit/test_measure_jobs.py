"""Measure rig as selectable jobs: the registry, the runner and its honesty.

Everything here runs with no hardware: jobs are stand-ins or the built-in
jobs with their device ends replaced by instrument stand-ins (a dispenser
that counts deliveries, a SpikeGLX connection that serves a known stream, a
tracker whose calibration result is chosen). What is checked is what could be
quietly wrong: an order that follows the clicks, a device opened for a job
that could not run, a cancelled or unavailable job reported as passed, a
reward train delivered without arming or delivered twice.
"""

from __future__ import annotations

import json
import math
import sys
import types
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from alhazen.config.models import (
    EyeTrackerConfig,
    MonitorConfig,
    RewardHwConfig,
    RigConfig,
    SpikeSourceConfig,
)
from alhazen.config.reward_calibration import load_reward_calibration
from alhazen.display.screen import Screen
from alhazen.modes import measure_builtin as builtin
from alhazen.modes import measure_stats as stats
from alhazen.modes.measure import Measurement
from alhazen.modes.measure_jobs import (
    BLOCKED,
    CANCELLED,
    ERROR,
    FAILED,
    MEASURED,
    PASSED,
    UNAVAILABLE,
    WAITING,
    Devices,
    JobUnavailable,
    MeasurementJob,
    OperatorCancelled,
    StatusFile,
    catalog,
    installed_jobs,
    parse_inputs,
    plan,
    run_jobs,
)

MONITOR = MonitorConfig(
    width_px=1920, height_px=1080, width_cm=52.0, distance_cm=57.0, refresh_rate_hz=60.0
)
SCREEN = Screen.from_monitor(MONITOR)


def rig(**devices) -> RigConfig:
    return RigConfig(monitor=MONITOR, data_root="data", devices=devices)


class Operator:
    """Answers from a script; records what it was asked."""

    def __init__(self, numbers=(), confirms=()):
        self.numbers = list(numbers)
        self.confirms = list(confirms)
        self.asked = []

    def ask_number(self, key, prompt, *, unit, low, high):
        self.asked.append(key)
        value = self.numbers.pop(0)
        if value == "ESC":
            raise OperatorCancelled(f"stopped at {key}")
        return value

    def confirm(self, key, prompt):
        self.asked.append(key)
        return self.confirms.pop(0)

    def tell(self, prompt):
        self.asked.append(prompt)


def job(key, order, result=None, *, needs=(), requires=(), unavailable=None, run=None):
    def default_run(ctx):
        if isinstance(result, BaseException):
            raise result
        return result

    kwargs = {}
    if unavailable is not None:
        kwargs["unavailable"] = lambda _rig, _inputs: unavailable
    return MeasurementJob(
        key,
        "Group",
        key,
        "a job",
        order,
        run or default_run,
        needs=frozenset(needs),
        requires=tuple(requires),
        **kwargs,
    )


def run(
    ordered,
    tmp_path,
    *,
    devices=None,
    operator=None,
    inputs=None,
    status=None,
    rig_cfg=None,
    subject=None,
):
    rig_file = tmp_path / "rig-test.yaml"
    rig_file.write_text("monitor: {}\n", encoding="utf-8")
    return run_jobs(
        rig_cfg or rig(),
        str(rig_file),
        ordered,
        devices=devices or Devices({}),
        operator=operator or Operator(),
        inputs=inputs,
        output_dir=tmp_path / "out",
        status=status,
        echo=lambda _line: None,
        subject=subject,
    )


OK = Measurement("ok thing", "fine", True, {"value": 1})
BAD = Measurement("bad thing", "wrong", False)
FACT = Measurement("fact", "a distribution", None)


class TestPlan:
    JOBS = {
        "a.first": job("a.first", 10, OK),
        "b.second": job("b.second", 20, OK),
        "c.third": job("c.third", 30, OK, requires=["a.first"]),
    }

    def test_jobs_run_in_their_own_order_not_the_order_ticked(self):
        ordered = plan(self.JOBS, ["b.second", "c.third", "a.first"])
        assert [j.key for j in ordered] == ["a.first", "b.second", "c.third"]

    def test_an_empty_selection_is_refused_rather_than_running_everything(self):
        with pytest.raises(ValueError, match="at least one"):
            plan(self.JOBS, [])

    def test_an_unknown_or_repeated_key_is_refused_by_name(self):
        with pytest.raises(ValueError, match="no measurement called z.nope"):
            plan(self.JOBS, ["a.first", "z.nope"])
        with pytest.raises(ValueError, match="selected twice"):
            plan(self.JOBS, ["a.first", "a.first"])

    def test_a_prerequisite_must_be_selected_with_its_dependant(self):
        with pytest.raises(ValueError, match="c.third needs a.first"):
            plan(self.JOBS, ["c.third"])

    def test_a_key_must_be_namespaced_plain_text(self):
        with pytest.raises(ValueError, match="namespace"):
            job("Bad Key", 1, OK)
        with pytest.raises(ValueError, match="namespace"):
            job("nodot", 1, OK)


class TestRegistry:
    class Point:
        def __init__(self, name, loaded):
            self.name, self.value, self._loaded = name, f"pkg:{name}", loaded

        def load(self):
            if isinstance(self._loaded, BaseException):
                raise self._loaded
            return self._loaded

    def test_alhazen_ships_its_own_jobs_in_a_stable_order(self):
        jobs = installed_jobs(lambda: [])
        keys = [entry["key"] for entry in catalog(jobs)]
        assert keys[0] == "monitor.refresh"
        assert {
            "monitor.geometry",
            "monitor.luminance",
            "monitor.colour",
            "tracker.calibration",
            "tracker.accuracy",
            "reward.connection",
            "reward.volume",
            "neural.stream",
            "input.keys",
            "input.mouse",
        } <= set(keys)
        assert keys.index("tracker.calibration") < keys.index("tracker.accuracy")

    def test_a_package_adds_jobs_through_its_entry_point(self):
        extra = job("pkg.bead", 90, OK)
        jobs = installed_jobs(lambda: [self.Point("pkg", lambda: [extra])])
        assert jobs["pkg.bead"] is extra
        assert catalog(jobs)[-1]["key"] == "pkg.bead"

    def test_a_key_already_taken_is_refused_naming_both(self):
        clash = job("monitor.refresh", 1, OK)
        with pytest.raises(ValueError, match="registers 'monitor.refresh'.*alhazen"):
            installed_jobs(lambda: [self.Point("pkg", lambda: [clash])])

    def test_a_provider_that_breaks_is_named_not_skipped(self):
        with pytest.raises(ValueError, match="'pkg'.*failed to load"):
            installed_jobs(lambda: [self.Point("pkg", ImportError("no module"))])
        with pytest.raises(ValueError, match="not a MeasurementJob"):
            installed_jobs(lambda: [self.Point("pkg", lambda: ["text"])])

    def test_the_catalog_carries_no_callables(self):
        listed = catalog(installed_jobs(lambda: []))
        json.dumps(listed)  # what the dashboard receives is plain JSON
        accuracy = next(e for e in listed if e["key"] == "tracker.accuracy")
        assert accuracy["requires"] == ["tracker.calibration"]
        subjects = {entry["key"]: entry["subject"] for entry in listed}
        assert subjects["tracker.calibration"] == subjects["tracker.accuracy"] == "required"
        assert subjects["input.keys"] == "optional"
        assert {k for k, v in subjects.items() if v == "none"} >= {
            "monitor.refresh",
            "monitor.geometry",
            "monitor.luminance",
            "reward.volume",
            "reward.connection",
            "neural.stream",
            "input.mouse",
        }


class TestInputs:
    def test_pairs_are_parsed_and_malformed_ones_refused(self):
        assert parse_inputs(["monitor.geometry.distance_cm=57.5"]) == {
            "monitor.geometry.distance_cm": "57.5"
        }
        for bad in ["distance=57", "monitor.geometry.distance_cm", "monitor.geometry.distance_cm="]:
            with pytest.raises(ValueError):
                parse_inputs([bad])
        with pytest.raises(ValueError, match="twice"):
            parse_inputs(["a.b=1", "a.b=2"])


class TestRun:
    def test_each_result_gets_its_honest_state(self, tmp_path):
        ordered = [job("a.ok", 1, OK), job("b.bad", 2, BAD), job("c.fact", 3, FACT)]
        report = run(ordered, tmp_path)
        assert [r.state for r in report.records] == [PASSED, FAILED, MEASURED]
        assert report.ok is False

    def test_an_unavailable_job_never_opens_its_device_and_is_not_a_pass(self, tmp_path):
        opened = []
        devices = Devices({"reward": lambda stack: opened.append("reward")})
        ordered = [
            job("a.ok", 1, OK),
            job("r.vol", 2, OK, needs=["reward"], unavailable="no reward here"),
        ]
        report = run(ordered, tmp_path, devices=devices)
        assert report.records[1].state == UNAVAILABLE
        assert report.records[1].summary == "no reward here"
        assert opened == []
        assert report.ok is False
        assert [m["name"] for m in report.as_dict()["measurements"]] == ["ok thing"]

    def test_a_job_that_raises_is_an_error_and_the_rest_still_run(self, tmp_path):
        report = run([job("a.boom", 1, RuntimeError("cable")), job("b.ok", 2, OK)], tmp_path)
        assert report.records[0].state == ERROR and "cable" in report.records[0].summary
        assert report.records[1].state == PASSED

    def test_a_job_whose_prerequisite_produced_nothing_is_blocked(self, tmp_path):
        ordered = [
            job("t.cal", 1, OperatorCancelled("aborted")),
            job("t.acc", 2, OK, requires=["t.cal"]),
        ]
        report = run(ordered, tmp_path)
        assert [r.state for r in report.records] == [CANCELLED, BLOCKED]

    def test_a_job_that_finds_it_cannot_measure_is_unavailable(self, tmp_path):
        report = run([job("n.stream", 1, JobUnavailable("acquisition not running"))], tmp_path)
        assert report.records[0].state == UNAVAILABLE

    def test_a_stop_cancels_the_rest_keeps_the_finished_and_releases_devices(self, tmp_path):
        closed = []

        def display(stack: ExitStack):
            stack.callback(closed.append, "display")
            return object()

        def uses_display(ctx):
            ctx.devices.get("display")
            raise KeyboardInterrupt

        devices = Devices({"display": display})
        ordered = [
            job("a.ok", 1, OK),
            job("b.stop", 2, run=uses_display, needs=["display"]),
            job("c.never", 3, OK),
        ]
        report = run(ordered, tmp_path, devices=devices)
        assert report.stopped is True
        assert [r.state for r in report.records] == [PASSED, CANCELLED, CANCELLED]
        assert closed == ["display"]
        assert report.provenance["released"] == ["display"]

    def test_devices_are_opened_once_however_many_jobs_use_them(self, tmp_path):
        opened = []
        devices = Devices({"display": lambda stack: opened.append(1) or "window"})

        def use(ctx):
            ctx.devices.get("display")
            return OK

        run([job("a.one", 1, run=use), job("b.two", 2, run=use)], tmp_path, devices=devices)
        assert opened == [1]

    def test_the_status_file_shows_the_queue_and_the_wait_for_the_operator(self, tmp_path):
        seen = []
        status_path = tmp_path / "status.json"

        class Watching(Operator):
            def ask_number(self, key, prompt, *, unit, low, high):
                seen.append(json.loads(status_path.read_text()))
                return 57.0

        def asks(ctx):
            ctx.number("distance_cm", "eye to screen", unit="cm", low=10, high=400)
            return OK

        report = run(
            [job("m.geo", 1, run=asks), job("z.after", 2, OK)],
            tmp_path,
            operator=Watching(),
            status=StatusFile(status_path),
        )
        during = seen[0]
        assert during["current"] == "m.geo"
        assert during["jobs"][0]["state"] == WAITING
        assert during["jobs"][0]["waiting_for"] == "eye to screen (cm)"
        assert during["jobs"][1]["state"] == "queued"
        final = json.loads(status_path.read_text())
        assert final["done"] == final["total"] == 2
        assert report.records[0].detail["operator_inputs"]["distance_cm"] == {
            "value": 57.0,
            "unit": "cm",
            "source": "operator",
        }

    def test_a_given_input_is_used_instead_of_asking(self, tmp_path):
        operator = Operator()

        def asks(ctx):
            return Measurement("d", str(ctx.number("distance_cm", "p", unit="cm", low=1, high=400)))

        report = run(
            [job("m.geo", 1, run=asks)],
            tmp_path,
            operator=operator,
            inputs={"m.geo.distance_cm": "61.5"},
        )
        assert operator.asked == []
        assert (
            report.records[0].detail["operator_inputs"]["distance_cm"]["source"]
            == "--measure-input"
        )

    @pytest.mark.parametrize("value", ["nan", "inf", "-3", "abc", "1e9"])
    def test_a_reading_that_is_not_plausible_is_refused_not_measured(self, tmp_path, value):
        def asks(ctx):
            ctx.number("distance_cm", "p", unit="cm", low=1, high=400)
            return OK

        report = run([job("m.geo", 1, run=asks)], tmp_path, inputs={"m.geo.distance_cm": value})
        assert report.records[0].state == ERROR

    def test_the_report_keeps_the_old_fields_and_adds_provenance(self, tmp_path):
        report = run([job("a.ok", 1, OK)], tmp_path)
        saved = json.loads(report.save(tmp_path / "m" / "rig_1.json").read_text())
        assert saved["rig"].endswith("rig-test.yaml") and saved["ok"] is True
        assert saved["measurements"][0] == {
            "name": "ok thing",
            "ok": True,
            "summary": "fine",
            "value": 1,
            "duration_s": pytest.approx(0, abs=1),
        }
        assert saved["schema"] == 2 and saved["selected"] == ["a.ok"]
        assert saved["provenance"]["rig_config"]["monitor"]["distance_cm"] == 57.0
        assert len(saved["provenance"]["rig_sha256"]) == 64

    def test_a_report_never_replaces_an_earlier_one(self, tmp_path):
        first = run([job("a.ok", 1, OK)], tmp_path).save(tmp_path / "m" / "rig.json")
        second = run([job("a.ok", 1, OK)], tmp_path).save(tmp_path / "m" / "rig.json")
        assert first != second and first.exists() and second.exists()


class TestStatistics:
    def test_geometry_separates_declared_and_measured(self):
        bar_px = SCREEN.deg2px(10.0)
        declared_bar_cm = bar_px * 52.0 / 1920
        result = stats.geometry_check(
            width_px=1920,
            declared_width_cm=52.0,
            declared_distance_cm=57.0,
            bar_px=bar_px,
            measured_bar_cm=declared_bar_cm,
            measured_distance_cm=57.0,
        )
        assert result["ok"] and result["px_per_deg_error"] == pytest.approx(0, abs=1e-12)
        far = stats.geometry_check(
            width_px=1920,
            declared_width_cm=52.0,
            declared_distance_cm=57.0,
            bar_px=bar_px,
            measured_bar_cm=declared_bar_cm,
            measured_distance_cm=60.0,
        )
        assert far["declared"]["distance_cm"] == 57.0 and far["measured"]["distance_cm"] == 60.0
        assert far["px_per_deg_error"] == pytest.approx(60 / 57 - 1, rel=1e-3)
        assert not far["ok"]

    @pytest.mark.parametrize("bad", [0.0, -1.0, math.nan, math.inf])
    def test_geometry_refuses_an_implausible_reading(self, bad):
        with pytest.raises(ValueError):
            stats.geometry_check(
                width_px=1920,
                declared_width_cm=52,
                declared_distance_cm=57,
                bar_px=100,
                measured_bar_cm=bad,
                measured_distance_cm=57,
            )

    def test_gaze_quality_keeps_bias_precision_and_gain_apart(self):
        one = SCREEN.deg2px(1.0)
        targets = [(0.0, 0.0), (8 * one, 0.0), (0.0, 8 * one), (-8 * one, -8 * one)]
        # Every sample 0.5° right of its target, alternating ±0.1° in y.
        samples = [[(x + 0.5 * one, y + s * 0.1 * one) for s in (1, -1, 1, -1)] for x, y in targets]
        q = stats.gaze_quality(targets, samples, SCREEN)
        assert q["accuracy_mean_dva"] == pytest.approx(0.5, rel=1e-6)
        assert q["per_target"][0]["bias_dva"] == pytest.approx([0.5, 0.0], abs=1e-9)
        assert q["precision_rms_s2s_median_dva"] == pytest.approx(0.2, rel=1e-6)
        assert q["per_target"][0]["dispersion_sd_dva"] == pytest.approx(0.1, rel=1e-6)
        assert q["gain"]["x"] == pytest.approx(1.0) and q["gain"]["y"] == pytest.approx(1.0)
        assert q["offset_dva"]["x"] == pytest.approx(0.5, rel=1e-6)

    def test_gain_below_one_is_a_compressed_calibration(self):
        targets = [(-200.0, 0.0), (0.0, 0.0), (200.0, 0.0)]
        samples = [[(0.9 * x, 0.0)] * 3 for x, _y in targets]
        q = stats.gaze_quality(targets, samples, SCREEN)
        assert q["gain"]["x"] == pytest.approx(0.9)
        assert q["gain"]["y"] is None  # one y position: no slope to fit

    def test_gaze_quality_refuses_a_target_with_no_samples(self):
        with pytest.raises(ValueError, match="no finite gaze samples"):
            stats.gaze_quality([(0.0, 0.0)], [[]], SCREEN)

    def test_reward_volume_from_mass_and_declared_density(self):
        r = stats.reward_volume(net_mass_g=2.5, density_g_per_ml=1.0, n_pulses=100, pulse_ms=50)
        assert r["volume_ul"] == pytest.approx(2500.0)
        assert r["ul_per_pulse"] == pytest.approx(25.0)
        assert r["ul_per_ms_open"] == pytest.approx(0.5)
        for bad in (dict(net_mass_g=-1), dict(density_g_per_ml=0), dict(n_pulses=0)):
            args = dict(net_mass_g=2.5, density_g_per_ml=1.0, n_pulses=100, pulse_ms=50) | bad
            with pytest.raises(ValueError):
                stats.reward_volume(**args)

    def test_pointer_gain_flags_acceleration(self):
        g = stats.pointer_gain([400, 400, 400], [600, 600, 600], 10.0)
        assert g["slow"]["mean_px_per_cm"] == 40.0
        assert g["fast_over_slow"] == pytest.approx(1.5)
        assert g["acceleration_suspected"]
        assert not stats.pointer_gain([400], [404], 10.0)["acceleration_suspected"]

    def test_stream_rate_and_noise_in_counts(self):
        flow = stats.stream_rate(1000, 61000, 2.0, 30000.0)
        assert flow["observed_hz"] == 30000.0 and flow["rate_error"] == 0.0
        assert not stats.stream_rate(5, 5, 1.0, 30000.0)["advancing"]
        block = np.zeros((100, 3), dtype=np.int16)
        block[:, 0] = np.tile([10, -10], 50)
        noise = stats.channel_noise(block)
        assert noise["rms_counts"][0] == pytest.approx(10.0)
        assert noise["flat_channels"] == 2 and noise["units"] == "int16 ADC counts"
        with pytest.raises(ValueError):
            stats.channel_noise(np.zeros((1, 3)))

    def test_luminance_readings_must_be_finite_and_non_negative(self):
        ok = stats.luminance_summary([0, 0.5, 1], [0.5, 20, 100])
        assert ok["monotonic"] and ok["contrast_ratio"] == pytest.approx(200)
        with pytest.raises(ValueError):
            stats.luminance_summary([0, 0.5, 1], [0.5, -1, 100])
        with pytest.raises(ValueError):
            stats.luminance_summary([0, 0.5, 1], [0.5, math.nan, 100])


# ----------------------------------------------------------------------
# The built-in jobs, with instrument stand-ins
# ----------------------------------------------------------------------


def builtin_job(key):
    return installed_jobs(lambda: [])[key]


class Dispenser:
    def __init__(self, fail=None):
        self.deliveries = []
        self.closed = False
        self.fail = fail

    def deliver(self, pulses):
        self.deliveries.append(pulses)
        if self.fail:
            raise self.fail

    def close(self):
        self.closed = True


NIDAQ = RewardHwConfig(backend="nidaq", device="Dev1", channel="ao0")


class TestRewardVolume:
    def _run(self, tmp_path, dispenser, operator, inputs=None):
        devices = Devices({"reward": lambda stack: (stack.callback(dispenser.close), dispenser)[1]})
        return run(
            [builtin_job("reward.volume")],
            tmp_path,
            devices=devices,
            operator=operator,
            inputs=inputs or {},
            rig_cfg=rig(reward=NIDAQ),
        )

    def test_nothing_is_delivered_without_arming(self, tmp_path):
        dispenser = Dispenser()
        report = self._run(tmp_path, dispenser, Operator([2, 50, 50, 200], [False]))
        assert report.records[0].state == CANCELLED
        assert dispenser.deliveries == []
        assert not (tmp_path / "rig-test.reward.yaml").exists()

    def test_armed_trains_are_delivered_once_and_measured_from_the_beaker(self, tmp_path):
        dispenser = Dispenser()
        operator = Operator([4, 25, 50, 200, 2.5], [True])
        report = self._run(tmp_path, dispenser, operator)
        record = report.records[0]
        assert record.state == MEASURED
        # Four trains of 25 pulses: 100 pulses, 2.5 mL read off the beaker.
        assert len(dispenser.deliveries) == 4
        assert all(d.n_pulses == 25 and d.pulse_ms == 50 for d in dispenser.deliveries)
        assert record.detail["ul_per_pulse"] == pytest.approx(25.0)
        assert record.detail["operator_inputs"]["volume_ml"]["unit"] == "mL"
        assert record.detail["plan"]["line"] == "Dev1/ao0"
        assert dispenser.closed
        # ...and stored as the rig's calibration for 50 ms pulses on that line.
        stored = load_reward_calibration(tmp_path / "rig-test.yaml")
        assert stored is not None
        entry = stored["widths"][50]
        assert entry["ul_per_pulse"] == pytest.approx(25.0)
        assert (entry["line"], entry["voltage"], entry["trains"]) == ("Dev1/ao0", 5.0, 4)
        assert record.detail["calibration_file"].endswith("rig-test.reward.yaml")

    def test_a_failed_train_stops_the_run_and_records_nothing(self, tmp_path):
        dispenser = Dispenser(fail=TimeoutError("no acknowledgement"))
        report = self._run(tmp_path, dispenser, Operator([3, 25, 50, 200], [True]))
        record = report.records[0]
        assert record.state == FAILED
        assert record.detail["delivered"] == "uncertain"
        assert record.detail["trains_completed"] == 0
        # Never retried, never asked for a volume, nothing stored.
        assert len(dispenser.deliveries) == 1
        assert not (tmp_path / "rig-test.reward.yaml").exists()

    def test_a_plan_over_the_limits_is_refused_before_arming(self, tmp_path):
        dispenser = Dispenser()
        operator = Operator([1, 500, 1000, 200], [True])
        report = self._run(tmp_path, dispenser, operator)
        assert report.records[0].state == ERROR and "limit" in report.records[0].summary
        assert dispenser.deliveries == [] and "reward.volume.arm" not in operator.asked

    def test_trains_that_add_up_past_the_pulse_limit_are_refused(self, tmp_path):
        dispenser = Dispenser()
        operator = Operator([10, 100, 10, 200], [True])
        report = self._run(tmp_path, dispenser, operator)
        assert report.records[0].state == ERROR and "1000 pulses" in report.records[0].summary
        assert dispenser.deliveries == []

    def test_arming_cannot_come_from_measure_input(self, tmp_path):
        dispenser = Dispenser()
        inputs = {
            "reward.volume.trains": "1",
            "reward.volume.pulses": "10",
            "reward.volume.pulse_ms": "50",
            "reward.volume.inter_pulse_ms": "200",
            "reward.volume.arm": "yes",
        }
        operator = Operator([], [False])
        report = self._run(tmp_path, dispenser, operator, inputs)
        assert report.records[0].state == CANCELLED and dispenser.deliveries == []

    def test_volume_per_pulse_from_a_beaker_reading(self):
        r = stats.reward_volume_read(volume_ml=2.5, n_pulses=100, pulse_ms=50)
        assert r["ul_per_pulse"] == pytest.approx(25.0)
        assert r["ul_per_ms_open"] == pytest.approx(0.5)
        for bad in (dict(volume_ml=0), dict(n_pulses=0), dict(pulse_ms=-1)):
            with pytest.raises(ValueError):
                stats.reward_volume_read(**(dict(volume_ml=2.5, n_pulses=100, pulse_ms=50) | bad))

    def test_a_simulated_reward_is_unavailable_not_measured(self, tmp_path):
        report = run(
            [builtin_job("reward.volume")],
            tmp_path,
            rig_cfg=rig(reward=RewardHwConfig(backend="simulated")),
        )
        assert report.records[0].state == UNAVAILABLE


class TestRewardConnection:
    def _fake_nidaqmx(self, monkeypatch, devices):
        system = SimpleNamespace(
            driver_version=SimpleNamespace(major_version=23, minor_version=8, update_version=0),
            devices=devices,
        )
        module = types.ModuleType("nidaqmx")
        module.system = SimpleNamespace(System=SimpleNamespace(local=lambda: system))
        monkeypatch.setitem(sys.modules, "nidaqmx", module)
        monkeypatch.setitem(sys.modules, "nidaqmx.system", module.system)

    def _device(self, name, channels):
        return SimpleNamespace(
            name=name,
            product_type="USB-6001",
            ao_physical_chans=SimpleNamespace(channel_names=channels),
        )

    def test_the_configured_channel_is_found_read_only(self, tmp_path, monkeypatch):
        self._fake_nidaqmx(monkeypatch, [self._device("Dev1", ["Dev1/ao0", "Dev1/ao1"])])
        report = run([builtin_job("reward.connection")], tmp_path, rig_cfg=rig(reward=NIDAQ))
        assert report.records[0].state == PASSED
        assert report.records[0].detail["driver"] == "23.8.0"

    def test_a_missing_device_fails_naming_what_is_there(self, tmp_path, monkeypatch):
        self._fake_nidaqmx(monkeypatch, [self._device("Dev2", ["Dev2/ao0"])])
        report = run([builtin_job("reward.connection")], tmp_path, rig_cfg=rig(reward=NIDAQ))
        assert report.records[0].state == FAILED and "Dev2" in report.records[0].summary


class FakeSglx:
    def __init__(self, running=True, rate=30000.0, step=60000):
        self.running, self.rate, self.step = running, rate, step
        self.count = 1_000_000
        self.closed = False
        self.calls = []

    def version(self):
        return "20250930"

    def is_running(self):
        return self.running

    def sample_rate(self, js, ip):
        return self.rate

    def acq_channel_counts(self, js, ip):
        return [384, 384, 1]

    def sample_count(self, js, ip):
        self.calls.append("count")
        value = self.count
        self.count += self.step
        return value

    def fetch(self, js, ip, start, n, channels):
        rng = np.random.default_rng(0)
        return start, (rng.normal(0, 12, size=(n, len(channels)))).astype(np.int16)

    def close(self):
        self.closed = True


SPIKEGLX = SpikeSourceConfig(backend="spikeglx", host="10.0.0.2", stream="imec0")


class TestNeuralStream:
    def _run(self, tmp_path, monkeypatch, connection):
        # A clock that the skipped sleep still advances: with sleep a no-op
        # the real monotonic clock can read the same value twice (Windows
        # ticks every ~16 ms), and a zero listening time is refused.
        clock = [1000.0]

        def sleep(seconds):
            clock[0] += seconds

        monkeypatch.setattr(builtin.time, "sleep", sleep)
        monkeypatch.setattr(builtin.time, "monotonic", lambda: clock[0])
        devices = Devices(
            {"spikes": lambda stack: (stack.callback(connection.close), connection)[1]}
        )
        return run(
            [builtin_job("neural.stream")],
            tmp_path,
            devices=devices,
            rig_cfg=rig(spikes=SPIKEGLX),
            inputs={"neural.stream.listen_s": "2"},
        )

    def test_no_acquisition_configured_names_the_choice_to_make(self, tmp_path):
        report = run([builtin_job("neural.stream")], tmp_path)
        summary = report.records[0].summary
        assert report.records[0].state == UNAVAILABLE
        assert "spikeglx" in summary and "Open Ephys" in summary

    def test_a_stream_that_is_not_running_is_unavailable_and_never_started(
        self, tmp_path, monkeypatch
    ):
        connection = FakeSglx(running=False)
        report = self._run(tmp_path, monkeypatch, connection)
        assert (
            report.records[0].state == UNAVAILABLE and "never starts" in report.records[0].summary
        )
        assert connection.calls == [] and connection.closed

    def test_a_running_stream_reports_channels_and_noise_in_counts(self, tmp_path, monkeypatch):
        connection = FakeSglx()
        report = self._run(tmp_path, monkeypatch, connection)
        detail = report.records[0].detail
        assert detail["channel_counts"] == [384, 384, 1]
        assert detail["noise"]["units"] == "int16 ADC counts"
        assert detail["noise"]["n_channels"] == builtin.NOISE_CHANNELS
        assert connection.closed


class Tracker:
    def __init__(self, result):
        self.result = result
        self.calibrations = 0
        self.trials = []

    def calibrate(self):
        self.calibrations += 1
        return self.result

    def start_trial(self, index, status):
        self.trials.append("start")

    def stop_trial(self):
        self.trials.append("stop")


def calibration(**kwargs):
    from alhazen.devices.eyetracker.protocol import CalibrationResult

    base = dict(ok=True, layout="HV9", n_targets=9, eye="left", advance="manual", t=1.0)
    return CalibrationResult(**{**base, **kwargs})


EYELINK = EyeTrackerConfig(backend="eyelink")


class TestTracker:
    def _run(self, tmp_path, monkeypatch, tracker, samples=None):
        if samples is not None:
            monkeypatch.setattr(
                builtin,
                "gaze_collector",
                lambda *a: lambda position, seconds, echo: samples(position),
            )
        devices = Devices({"tracker": lambda stack: tracker, "display": lambda stack: "window"})
        ordered = plan(installed_jobs(lambda: []), ["tracker.accuracy", "tracker.calibration"])
        return run(
            ordered, tmp_path, devices=devices, rig_cfg=rig(eyetracker=EYELINK), subject="s01"
        )

    def test_an_aborted_calibration_cancels_and_blocks_the_accuracy(self, tmp_path, monkeypatch):
        tracker = Tracker(calibration(ok=None, aborted=True, note="aborted at target 3"))
        report = self._run(tmp_path, monkeypatch, tracker)
        assert [r.state for r in report.records] == [CANCELLED, BLOCKED]
        assert tracker.calibrations == 1

    def test_accuracy_is_measured_once_on_fresh_samples_without_recalibrating(
        self, tmp_path, monkeypatch
    ):
        tracker = Tracker(calibration(note="the device reports a calibration"))
        one = SCREEN.deg2px(1.0)
        report = self._run(
            tmp_path, monkeypatch, tracker, samples=lambda p: [(p[0] + 0.3 * one, p[1])] * 5
        )
        assert [r.state for r in report.records] == [PASSED, PASSED]
        assert tracker.calibrations == 1  # the accuracy job does not calibrate again
        assert report.records[1].detail["accuracy_max_dva"] == pytest.approx(0.3, rel=1e-6)
        assert tracker.trials == ["start", "stop"]

    def test_a_measurement_of_the_subject_needs_one_named(self, tmp_path):
        ordered = plan(installed_jobs(lambda: []), ["tracker.calibration"])
        with pytest.raises(ValueError, match="subject in the chair"):
            run(ordered, tmp_path, rig_cfg=rig(eyetracker=EYELINK))
        machine_only = plan(installed_jobs(lambda: []), ["monitor.colour"])
        assert run(machine_only, tmp_path).provenance["subject"] is None

    def test_a_stand_in_tracker_is_unavailable(self, tmp_path):
        ordered = plan(installed_jobs(lambda: []), ["tracker.calibration"])
        report = run(
            ordered,
            tmp_path,
            subject="s01",
            rig_cfg=rig(eyetracker=EyeTrackerConfig(backend="mouse_sim")),
        )
        assert report.records[0].state == UNAVAILABLE and "stand-in" in report.records[0].summary


class TestMonitorJobs:
    def test_geometry_from_given_tape_readings(self, tmp_path):
        bar_cm = SCREEN.deg2px(10.0) * 52.0 / 1920
        report = run(
            [builtin_job("monitor.geometry")],
            tmp_path,
            inputs={
                "monitor.geometry.distance_cm": "57",
                "monitor.geometry.bar_cm": f"{bar_cm:.6f}",
            },
        )
        assert report.records[0].state == PASSED

    def test_luminance_is_fitted_and_never_applied(self, tmp_path):
        readings = tmp_path / "meter.csv"
        levels = np.linspace(0, 1, 9)
        lum = 0.4 + 99.6 * levels**2.2
        readings.write_text(
            "level,luminance\n" + "\n".join(f"{a},{b}" for a, b in zip(levels, lum, strict=True)),
            encoding="utf-8",
        )
        report = run(
            [builtin_job("monitor.luminance")],
            tmp_path,
            inputs={
                "monitor.luminance.readings": str(readings),
                "monitor.luminance.instrument": "PR-655",
            },
        )
        record = report.records[0]
        assert record.state == MEASURED
        assert record.detail["fit"]["gamma"] == pytest.approx(2.2, rel=1e-6)
        assert record.detail["instrument"] == "PR-655"
        assert not list(tmp_path.glob("*_gamma.yaml"))  # nothing written beside the rig
        assert Path(record.detail["readings_csv"]).is_file()

    def test_guided_luminance_asks_for_every_level_in_cd_m2(self, tmp_path, monkeypatch):
        shown = []
        monkeypatch.setattr(builtin, "show_patch", lambda display, level: shown.append(level))
        readings = [0.5 + 100 * (i / 4) ** 2.0 for i in range(5)]
        operator = Operator(readings)
        report = run(
            [builtin_job("monitor.luminance")],
            tmp_path,
            operator=operator,
            devices=Devices({"display": lambda stack: "window"}),
            inputs={"monitor.luminance.levels": "5"},
        )
        assert shown == [0.0, 0.25, 0.5, 0.75, 1.0]
        assert report.records[0].detail["fit"]["gamma"] == pytest.approx(2.0, rel=1e-6)
        assert report.records[0].detail["operator_inputs"]["level_2"]["unit"] == "cd/m²"

    def test_colour_is_said_to_be_unsupported(self, tmp_path):
        report = run([builtin_job("monitor.colour")], tmp_path)
        assert report.records[0].state == UNAVAILABLE and "colorimeter" in report.records[0].summary

    def test_esc_at_a_prompt_cancels_that_job_only(self, tmp_path):
        report = run(
            [builtin_job("monitor.geometry"), job("z.after", 99, OK)],
            tmp_path,
            operator=Operator(["ESC"]),
            devices=Devices({"display": lambda stack: "window"}),
        )
        assert [r.state for r in report.records] == [CANCELLED, PASSED]


class TestMouse:
    def test_pointer_gain_from_drags(self, tmp_path, monkeypatch):
        drags = iter([400.0, 404.0, 396.0, 600.0, 610.0, 590.0])
        monkeypatch.setattr(builtin, "mouse_dragger", lambda display: lambda prompt: next(drags))
        report = run(
            [builtin_job("input.mouse")],
            tmp_path,
            operator=Operator([10.0]),
            devices=Devices({"display": lambda stack: "window"}),
        )
        record = report.records[0]
        assert record.state == MEASURED
        assert record.detail["slow"]["mean_px_per_cm"] == pytest.approx(40.0)
        assert record.detail["acceleration_suspected"]


class TestWindowOperator:
    class Display:
        def __init__(self):
            self.shown = []

        def show_message(self, text):
            self.shown.append(text)

    def _operator(self, batches):
        display = self.Display()
        devices = Devices({"display": lambda stack: display})
        events = []
        batches = list(batches)

        def keys():
            events.append("poll")
            return batches.pop(0) if batches else []

        operator = builtin.WindowOperator(
            devices, get_keys=keys, clear=lambda: events.append("clear"), sleep=lambda s: None
        )
        return operator, events, display

    def test_a_number_is_typed_and_accepted_after_the_buffer_was_cleared(self):
        operator, events, display = self._operator([["5", "7", "period", "5"], ["return"]])
        assert operator.ask_number("k", "distance", unit="cm", low=1, high=400) == 57.5
        assert events[0] == "clear"
        assert "57.5" in display.shown[-1]

    def test_out_of_range_asks_again_and_esc_cancels(self):
        operator, _events, display = self._operator([["9", "9", "9", "return"], ["escape"]])
        with pytest.raises(OperatorCancelled):
            operator.ask_number("k", "distance", unit="cm", low=1, high=400)
        assert any("outside" in text for text in display.shown)

    def test_confirm_needs_an_explicit_yes(self):
        operator, *_ = self._operator([["space", "return"], ["n"]])
        assert operator.confirm("k", "arm?") is False
        operator, *_ = self._operator([["y"]])
        assert operator.confirm("k", "arm?") is True


class TestCommandLine:
    """``alhazen run --mode measure`` with the new flags, end to end, with a
    selection that opens no device (inputs given in advance)."""

    def _rig(self, tmp_path):
        path = tmp_path / "rig-bench.yaml"
        path.write_text(
            "monitor: {width_px: 1920, height_px: 1080, width_cm: 52.0, distance_cm: 57.0, "
            "refresh_rate_hz: 60.0}\ndata_root: data\n",
            encoding="utf-8",
        )
        return path

    def test_the_list_is_json_in_run_order(self, capsys):
        from alhazen.cli.main import main

        assert main(["run", "--mode", "measure", "--list-measurements"]) == 0
        listed = json.loads(capsys.readouterr().out)
        assert listed[0]["key"] == "monitor.refresh"

    def test_a_selection_runs_and_writes_its_report_beside_the_rig(
        self, tmp_path, monkeypatch, capsys
    ):
        from alhazen.cli.main import main

        monkeypatch.chdir(tmp_path)
        rig_file = self._rig(tmp_path)
        bar_cm = SCREEN.deg2px(10.0) * 52.0 / 1920
        status = tmp_path / "status.json"
        code = main(
            [
                "run",
                "--mode",
                "measure",
                "--rig",
                str(rig_file),
                "--measure",
                "monitor.colour",
                "--measure",
                "monitor.geometry",
                "--measure-input",
                "monitor.geometry.distance_cm=57",
                "--measure-input",
                f"monitor.geometry.bar_cm={bar_cm:.6f}",
                "--measure-status",
                str(status),
            ]
        )
        out = capsys.readouterr().out
        assert "measuring, in this order: monitor.geometry, monitor.colour" in out
        # One passed, one unavailable: not a clean bill of health.
        assert code == 1
        reports = list((tmp_path / "measurements").glob("rig-bench_*.json"))
        saved = json.loads(reports[0].read_text())
        assert [j["state"] for j in saved["jobs"]] == ["passed", "unavailable"]
        assert json.loads(status.read_text())["report"] == str(reports[0])

    def test_unknown_or_mixed_flags_are_refused_before_anything_runs(
        self, tmp_path, monkeypatch, capsys
    ):
        from alhazen.cli.main import main

        monkeypatch.chdir(tmp_path)
        rig_file = self._rig(tmp_path)
        assert (
            main(["run", "--mode", "measure", "--rig", str(rig_file), "--measure", "x.nope"]) == 2
        )
        assert (
            main(
                [
                    "run",
                    "--mode",
                    "measure",
                    "--rig",
                    str(rig_file),
                    "--measure",
                    "monitor.refresh",
                    "--skip",
                    "keys",
                ]
            )
            == 2
        )
        assert (
            main(
                [
                    "run",
                    "--mode",
                    "measure",
                    "--rig",
                    str(rig_file),
                    "--measure",
                    "monitor.refresh",
                    "--measure-input",
                    "monitor.geometry.distance_cm=57",
                ]
            )
            == 2
        )
        assert (
            main(
                [
                    "run",
                    "--mode",
                    "measure",
                    "--rig",
                    str(rig_file),
                    "--measure-input",
                    "monitor.geometry.distance_cm=57",
                ]
            )
            == 2
        )
        assert not (tmp_path / "measurements").exists()


class TestKeepingTheTrackersRecording:
    class Tracker:
        def __init__(self):
            self.shutdowns = []

        def shutdown(self, destination):
            self.shutdowns.append(destination)

    def _devices(self, tracker):
        devices = Devices(
            {
                "tracker": lambda stack: (
                    stack.callback(
                        lambda: (
                            None if devices.was_handed_back("tracker") else tracker.shutdown(None)
                        )
                    ),
                    tracker,
                )[1]
            }
        )
        return devices

    def test_the_last_tracker_job_keeps_the_recording_and_it_is_ended_once(self, tmp_path):
        tracker = self.Tracker()

        def keeps(ctx):
            ctx.devices.get("tracker")
            ctx.keep_tracker_recording(ctx.output_dir / "bead" / "bead.edf")
            return FACT

        report = run(
            [job("p.bead", 1, run=keeps, needs=["tracker"])],
            tmp_path,
            devices=self._devices(tracker),
        )
        assert report.records[0].state == MEASURED
        assert tracker.shutdowns == [tmp_path / "out" / "bead" / "bead.edf"]
        assert report.provenance["released"] == ["tracker"]

    def test_it_is_refused_while_a_later_job_needs_the_tracker(self, tmp_path):
        tracker = self.Tracker()

        def keeps(ctx):
            ctx.devices.get("tracker")
            ctx.keep_tracker_recording(ctx.output_dir / "x.edf")
            return FACT

        report = run(
            [
                job("p.bead", 1, run=keeps, needs=["tracker"]),
                job("p.after", 2, OK, needs=["tracker"]),
            ],
            tmp_path,
            devices=self._devices(tracker),
        )
        assert report.records[0].state == ERROR and "later measurement" in report.records[0].summary
        assert tracker.shutdowns == [None]  # released once, by the run, keeping nothing


def test_the_ruler_pause_is_shown_as_waiting(tmp_path, monkeypatch):
    seen = []
    status_path = tmp_path / "status.json"

    def ruler(display, rig, dva):
        seen.append(json.loads(status_path.read_text())["jobs"][0]["state"])

    monkeypatch.setattr(builtin, "draw_ruler_on", ruler)
    bar_cm = SCREEN.deg2px(10.0) * 52.0 / 1920
    report = run(
        [builtin_job("monitor.geometry")],
        tmp_path,
        operator=Operator([57.0, bar_cm]),
        devices=Devices({"display": lambda stack: "window"}),
        status=StatusFile(status_path),
    )
    assert seen == [WAITING]
    assert report.records[0].state == PASSED
