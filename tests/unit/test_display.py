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


class _FakeTextStim:
    """Stands in for `visual.TextStim`: keeps its keyword arguments, counts
    draws, and reports a bounding box the way PsychoPy's does, so a test can
    check what would have been laid out without a renderer."""

    bounding_box: tuple[float, float] | None = (600.0, 200.0)

    def __init__(self, window, **kwargs):
        self.window = window
        self.kwargs = kwargs
        self.text = kwargs["text"]
        self.draws = 0

    @property
    def boundingBox(self):  # noqa: N802 — PsychoPy's spelling
        if self.bounding_box is None:
            raise AttributeError("no layout")
        return self.bounding_box

    def draw(self):
        self.draws += 1
        self.window.drawn.append(self)


class _FakeRect:
    def __init__(self, window, **kwargs):
        self.window = window
        self.kwargs = kwargs
        self.draws = 0

    def draw(self):
        self.draws += 1
        self.window.drawn.append(self)


class _FakeWindow:
    """An open PsychoPy window as the backend sees it: something to draw
    into and flip, with a handle that can be raised to the foreground."""

    frameBufferSize = (1920, 1080)  # noqa: N815 — PsychoPy's spelling
    clientSize = (1920, 1080)  # noqa: N815
    size = (1920, 1080)

    def __init__(self):
        self.drawn: list = []
        self.flips = 0
        self.activations = 0
        self.winHandle = types.SimpleNamespace(activate=self._activate)  # noqa: N815

    def _activate(self):
        self.activations += 1

    def flip(self, clearBuffer=True):  # noqa: N803 — PsychoPy's spelling
        self.flips += 1


def _open_psychopy_display(monkeypatch):
    """A PsychoPyDisplay opened against fake `psychopy.visual` classes, with
    the presentation sleep removed so the test does not wait on it."""
    from alhazen.config.models import MonitorConfig
    from alhazen.display import psychopy_backend

    fake_visual = types.SimpleNamespace(
        Window=lambda **kwargs: _FakeWindow(), TextStim=_FakeTextStim, Rect=_FakeRect
    )
    monkeypatch.setattr(psychopy_backend, "resolve_monitor", lambda monitor: None)
    monkeypatch.setitem(
        __import__("sys").modules, "psychopy", types.SimpleNamespace(visual=fake_visual)
    )
    monkeypatch.setitem(__import__("sys").modules, "psychopy.visual", fake_visual)
    monkeypatch.setattr(psychopy_backend.time, "sleep", lambda seconds: None)
    monitor = MonitorConfig(
        width_px=1920, height_px=1080, width_cm=52.0, distance_cm=57.0, refresh_rate_hz=120.0
    )
    display = psychopy_backend.PsychoPyDisplay(monitor)
    display.open()
    return display


class TestMessageBox:
    """A message is the session talking: it is drawn as a terminal — mono
    text on a dark panel outlined in green, sized to what it says — so it
    reads as a panel over the session, and as a different panel from the
    orange pause menu and the red fault."""

    def test_the_text_sits_in_a_box_sized_to_it(self, monkeypatch):
        from alhazen.display import psychopy_backend as pb

        display = _open_psychopy_display(monkeypatch)
        display.show_message("Look at the dot.\nPress SPACE when ready.")

        drawn = display.window.drawn
        rects = [d for d in drawn if isinstance(d, _FakeRect)]
        texts = [d for d in drawn if isinstance(d, _FakeTextStim)]
        assert rects and texts
        # The panel is drawn under the text, every time the text is drawn.
        assert isinstance(drawn[0], _FakeRect)
        assert len(rects) == len(texts) == 2

        text_height = max(18.0, 1080 * 0.022)
        box = rects[0].kwargs
        assert box["lineColor"] == pb.MESSAGE_OUTLINE == pb.TERMINAL_GREEN
        assert box["fillColor"] == pb.MESSAGE_PANEL_FILL
        # Hugging the laid-out text: its bounding box plus the padding.
        assert box["width"] == pytest.approx(600.0 + 2 * pb.MESSAGE_PADDING[0] * text_height)
        assert box["height"] == pytest.approx(200.0 + 2 * pb.MESSAGE_PADDING[1] * text_height)

        text = texts[0].kwargs
        assert text["font"] == pb.MESSAGE_FONT == pb.MONO_FONT
        assert text["color"] == pb.MESSAGE_COLOR == pb.TERMINAL_TEXT
        assert text["alignText"] == "left" and text["anchorHoriz"] == "center"
        assert text["height"] == pytest.approx(text_height)

    def test_the_message_is_presented_twice_from_the_foreground(self, monkeypatch):
        """The instructions are the first frame after the build, when the
        dashboard's browser may have just taken the foreground. Claim it,
        and present twice, or Windows keeps the previous frame."""
        display = _open_psychopy_display(monkeypatch)
        display.show_message("hello")
        assert display.window.flips == 2
        assert display.window.activations == 1

    def test_a_text_with_no_layout_still_gets_a_box_and_says_so(self, monkeypatch, caplog):
        """A renderer that cannot report the text's extent is not a reason to
        draw no box: the box is estimated from the wrap width and the line
        count, and the estimate is logged as one."""
        monkeypatch.setattr(_FakeTextStim, "bounding_box", None)
        display = _open_psychopy_display(monkeypatch)
        with caplog.at_level(logging.WARNING):
            display.show_message("one\ntwo\nthree")
        rect = next(d for d in display.window.drawn if isinstance(d, _FakeRect))
        text_height = max(18.0, 1080 * 0.022)
        wrap_width = min(1920 * 0.8, text_height * 34)
        assert rect.kwargs["width"] == pytest.approx(wrap_width + 4 * text_height)
        assert rect.kwargs["height"] > 3 * text_height * 1.2
        assert "could not measure the message text" in caplog.text
        assert "3 line(s)" in caplog.text

    def test_the_menu_keeps_its_own_colour_and_size(self, monkeypatch):
        """The pause menu is the other panel: same backing, its own colour,
        and a fixed fraction of the screen rather than a text-sized box."""
        from alhazen.display import psychopy_backend as pb

        display = _open_psychopy_display(monkeypatch)
        display.show_menu("PAUSED", "SPACE  resume", color=(1.0, 0.16, -0.70))
        drawn = display.window.drawn
        assert isinstance(drawn[0], _FakeRect)
        assert drawn[0].kwargs["lineColor"] == (1.0, 0.16, -0.70)
        assert drawn[0].kwargs["width"] == pytest.approx(1920 * pb.MENU_PANEL_FRACTION[0])
        assert drawn[0].kwargs["height"] == pytest.approx(1080 * pb.MENU_PANEL_FRACTION[1])
        heading, rows = (d for d in drawn if isinstance(d, _FakeTextStim))
        assert heading.kwargs["font"] == pb.HEADING_FONT
        assert rows.kwargs["font"] == pb.MENU_FONT == pb.MONO_FONT
        assert display.window.flips == 1
