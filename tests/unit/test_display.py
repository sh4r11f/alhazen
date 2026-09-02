"""Screen geometry and frame QA mechanics."""

from __future__ import annotations

import logging
import math
import types

import pytest

from alhazen.config.models import FrameQAConfig, MonitorConfig
from alhazen.display import simulated
from alhazen.display.frames import FrameMonitor
from alhazen.display.screen import Screen, within_radius
from alhazen.display.simulated import _SPIN_MARGIN_S, SimulatedDisplay, _wait_until
from alhazen.errors import DisplayError, FrameQAError


class TestScreen:
    def setup_method(self):
        self.screen = Screen.from_monitor(
            MonitorConfig(
                width_px=1920,
                height_px=1080,
                width_cm=60.0,
                distance_cm=60.0,
                refresh_rate_hz=60.0,
            )
        )

    def test_px2deg_is_exact_inverse_of_deg2px(self):
        # The invariant that keeps recorded positions honest: whatever model
        # places a stimulus must read positions back identically.
        for deg in (0.0, 0.5, 3.7, 15.0, -8.2):
            assert self.screen.px2deg(self.screen.deg2px(deg)) == pytest.approx(deg)

    def test_px_per_deg_magnitude(self):
        # 60 cm distance, 32 px/cm: 1 dva ~ 60*tan(1 deg)*32 ~ 33.5 px.
        assert self.screen.px_per_deg == pytest.approx(33.5, abs=0.1)

    def test_screen_centered_roundtrip_and_y_flip(self):
        cx, cy = self.screen.screen_to_centered(960.0, 540.0)
        assert (cx, cy) == (0.0, 0.0)
        # Screen y grows down; centered y grows up.
        _, top = self.screen.screen_to_centered(960.0, 0.0)
        assert top == 540.0
        sx, sy = self.screen.centered_to_screen(-100.0, 200.0)
        assert self.screen.screen_to_centered(sx, sy) == (-100.0, 200.0)

    def test_within_radius_boundary_inclusive(self):
        assert within_radius((3.0, 4.0), (0.0, 0.0), 5.0)
        assert not within_radius((3.0, 4.1), (0.0, 0.0), 5.0)


class TestFrameMonitor:
    def make(self, policy="warn", tolerance=0.5, budget=3):
        cfg = FrameQAConfig(policy=policy, tolerance=tolerance, max_dropped_per_trial=budget)
        return FrameMonitor(cfg, refresh_rate_hz=100.0)  # expected 10 ms

    def test_first_flip_only_establishes_reference(self):
        monitor = self.make()
        monitor.start_trial(1)
        assert monitor.note_flip(5.0) is False
        assert monitor.records == []

    def test_detects_drop_beyond_tolerance(self):
        monitor = self.make(tolerance=0.5)
        monitor.start_trial(1)
        monitor.note_flip(0.0)
        assert monitor.note_flip(0.010) is False  # exactly one period
        assert monitor.note_flip(0.024) is False  # 14 ms < 15 ms threshold
        assert monitor.note_flip(0.040) is True  # 16 ms > threshold
        assert [r.dropped for r in monitor.records] == [False, False, True]

    def test_trial_gap_is_not_a_drop(self):
        monitor = self.make()
        monitor.start_trial(1)
        monitor.note_flip(0.0)
        monitor.note_flip(0.010)
        monitor.start_trial(2)  # long inter-trial gap follows
        assert monitor.note_flip(5.0) is False

    def test_abort_run_raises_only_past_budget(self):
        monitor = self.make(policy="abort_run", budget=1)
        monitor.start_trial(1)
        monitor.note_flip(0.0)
        assert monitor.note_flip(0.1) is True  # first drop: within budget
        with pytest.raises(FrameQAError):
            monitor.note_flip(0.2)

    def test_marks_trials_flag(self):
        assert self.make(policy="mark_trial").marks_trials
        assert self.make(policy="abort_run").marks_trials
        assert not self.make(policy="warn").marks_trials
        assert not self.make(policy="log").marks_trials

    def test_save_writes_full_log(self, tmp_path):
        monitor = self.make()
        monitor.start_trial(1)
        monitor.note_flip(0.0)
        monitor.note_flip(0.010)
        monitor.note_flip(0.040)
        path = tmp_path / "frames.csv"
        monitor.save(path)
        lines = path.read_text().strip().splitlines()
        assert lines[0] == "trial_index,t,interval_s,dropped"
        assert len(lines) == 3  # header + 2 measured intervals
        assert lines[2].endswith("True")


class TestSimulatedDisplay:
    def test_unpaced_reports_nominal_rate(self):
        display = SimulatedDisplay(nominal_refresh_hz=60.0, frame_period_s=0.0)
        display.open()
        assert display.measure_refresh_rate(10) == 60.0
        display.flip()
        assert display.flip_count == 1

    def test_messages_are_recorded(self):
        display = SimulatedDisplay(nominal_refresh_hz=60.0, frame_period_s=0.0)
        display.show_message("hello")
        assert display.messages == ["hello"]


class _Host:
    """A clock that moves only when something sleeps on it or polls it.

    A sleep returns at the next scheduler tick at or after the time asked
    for, which is what ``time.sleep`` really does; ``tick_s`` is the tick.
    A poll costs 10 us, so a spin makes progress rather than looping forever.
    """

    def __init__(self, tick_s: float) -> None:
        self.t = 0.0
        self.tick_s = tick_s
        self.slept: list[float] = []

    def now(self) -> float:
        self.t += 1e-5
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t = math.ceil((self.t + seconds) / self.tick_s) * self.tick_s


class TestSimulatedDisplayPacing:
    """A paced flip lands on its deadline whatever the host's sleep costs.

    Sleeping the whole remainder returned one scheduler tick late on every
    frame, and on Windows that tick is 15.6 ms: a 60 Hz simulation ran at
    31 ms a frame and the frame monitor flagged every flip as dropped. These
    drive the wait with a fake clock, so they pin the fix without timing.
    """

    FRAME = 1.0 / 60.0

    def test_sleeps_all_but_the_margin_then_spins_to_the_deadline(self):
        host = _Host(tick_s=0.001)
        _wait_until(self.FRAME, now=host.now, sleep=host.sleep)
        # One clock poll (10 us) happens before the sleep is sized.
        assert host.slept == [pytest.approx(self.FRAME - _SPIN_MARGIN_S, abs=2e-5)]
        # Returned on the deadline, not on the tick after it.
        assert host.t == pytest.approx(self.FRAME, abs=2e-5)

    def test_a_wait_shorter_than_the_margin_only_spins(self):
        host = _Host(tick_s=0.001)
        _wait_until(0.001, now=host.now, sleep=host.sleep)
        assert host.slept == []
        assert host.t == pytest.approx(0.001, abs=2e-5)

    def test_a_deadline_already_passed_returns_at_once(self):
        host = _Host(tick_s=0.001)
        host.t = 0.5
        _wait_until(0.1, now=host.now, sleep=host.sleep)
        assert host.slept == []
        assert host.t == pytest.approx(0.5, abs=2e-5)

    def test_flips_land_one_period_apart(self):
        host = _Host(tick_s=0.001)
        display = SimulatedDisplay(nominal_refresh_hz=60.0, now=host.now, sleep=host.sleep)
        display.flip()
        first = host.t
        for _ in range(3):
            display.flip()
        assert display.flip_count == 4
        assert host.t - first == pytest.approx(3 * self.FRAME, abs=1e-4)
        assert len(host.slept) == 3

    def test_unpaced_never_sleeps(self):
        host = _Host(tick_s=0.001)
        display = SimulatedDisplay(
            nominal_refresh_hz=60.0, frame_period_s=0.0, now=host.now, sleep=host.sleep
        )
        for _ in range(5):
            display.flip()
        assert host.slept == []

    def test_open_requests_fine_ticks_and_close_releases_them(self, monkeypatch):
        calls: list[str] = []

        def request() -> object:
            calls.append("begin")
            return lambda: calls.append("end")

        monkeypatch.setattr(simulated, "_request_fine_timer", request)
        display = SimulatedDisplay(nominal_refresh_hz=60.0)
        display.open()
        display.open()  # idempotent: one request, one release
        assert calls == ["begin"]
        display.close()
        display.close()
        assert calls == ["begin", "end"]

    def test_unpaced_display_leaves_the_timer_alone(self, monkeypatch):
        monkeypatch.setattr(
            simulated, "_request_fine_timer", lambda: pytest.fail("should not be asked")
        )
        display = SimulatedDisplay(nominal_refresh_hz=60.0, frame_period_s=0.0)
        display.open()
        display.close()

    def test_only_windows_is_asked(self, monkeypatch):
        monkeypatch.setattr(simulated.sys, "platform", "linux")
        release = simulated._request_fine_timer()
        release()  # a no-op, not an error


class TestFramebufferMatchesTheRigConfig:
    """Everything downstream is degrees computed from `width_px`, and stimuli
    are drawn in framebuffer pixels. A framebuffer that is not the size the
    config claims makes every stimulus the wrong physical size, silently — a
    Retina Mac halves them. These pin the check that refuses to run.
    """

    def _display(self, monkeypatch, *, buffer, client, fullscreen=True, windowed=False):
        from alhazen.config.models import MonitorConfig
        from alhazen.display import psychopy_backend

        class FakeWindow:
            frameBufferSize = buffer
            clientSize = client
            size = buffer

        class FakeVisual:
            Window = staticmethod(lambda **kwargs: FakeWindow())

        monkeypatch.setattr(psychopy_backend, "resolve_monitor", lambda monitor: None)
        monkeypatch.setitem(
            __import__("sys").modules, "psychopy", types.SimpleNamespace(visual=FakeVisual)
        )
        monkeypatch.setitem(__import__("sys").modules, "psychopy.visual", FakeVisual)
        monitor = MonitorConfig(
            width_px=1920,
            height_px=1080,
            width_cm=52.0,
            distance_cm=57.0,
            refresh_rate_hz=120.0,
            fullscreen=fullscreen,
        )
        return psychopy_backend.PsychoPyDisplay(monitor, windowed=windowed)

    def test_a_matching_framebuffer_opens(self, monkeypatch):
        display = self._display(monkeypatch, buffer=(1920, 1080), client=(1920, 1080))
        display.open()
        assert display.window is not None

    def test_a_retina_framebuffer_refuses_and_says_what_to_put_in_the_config(self, monkeypatch):
        """The Mac case: 1920x1080 points, 3840x2160 device pixels. PsychoPy's
        `units="pix"` then means the small pixels, so every size is doubled."""
        display = self._display(monkeypatch, buffer=(3840, 2160), client=(1920, 1080))
        with pytest.raises(DisplayError) as caught:
            display.open()
        message = str(caught.value)
        assert "3840x2160" in message and "1920x1080" in message
        assert "2x" in message  # names the factor everything would be wrong by
        assert "NATIVE pixel count" in message

    def test_a_windowed_run_is_not_refused_only_warned(self, monkeypatch, caplog):
        """A dev window is smaller than the panel by definition. Sizes in
        degrees are still right; only the edges are clipped."""
        display = self._display(monkeypatch, buffer=(1200, 800), client=(1200, 800), windowed=True)
        with caplog.at_level(logging.WARNING):
            display.open()
        assert display.window is not None
        assert "clipped" in caplog.text

    def test_a_backend_that_reports_no_framebuffer_warns_rather_than_crashing(
        self, monkeypatch, caplog
    ):
        display = self._display(monkeypatch, buffer=None, client=(1920, 1080))
        with caplog.at_level(logging.WARNING):
            display.open()
        assert "could not be checked" in caplog.text
