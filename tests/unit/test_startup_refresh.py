"""The refresh rate at startup: measured once, quietly, and still enforced.

PsychoPy measured the frame rate twice at every session start — once inside
``visual.Window`` (checkTiming, behind "Attempting to measure frame rate of
screen, please wait ...") and once in alhazen's own measure_refresh_rate,
whose result is the one the session's frame math uses. The first is gone; the
second stays, quiet, bounded, and as strict as before.
"""

from __future__ import annotations

import sys
import types

import pytest
from tests.unit.test_display import _fake_pyglet

from alhazen.config.models import MonitorConfig, resolve_refresh
from alhazen.display import psychopy_backend as pb
from alhazen.errors import ConfigError, DisplayError

MONITOR = MonitorConfig(
    width_px=1920, height_px=1080, width_cm=52.0, distance_cm=57.0, refresh_rate_hz=120.0
)


class Window:
    frameBufferSize = (1920, 1080)
    clientSize = (1920, 1080)
    size = (1920, 1080)

    def __init__(self, rate=119.96, **kwargs):
        self.kwargs = kwargs
        self.rate = rate
        self.calls = []
        self.monitorFramePeriod = 1 / 60  # PsychoPy's own guess with checkTiming off
        self.refreshThreshold = 1.2 / 60

    def getActualFrameRate(self, **kwargs):
        self.calls.append(kwargs)
        return self.rate


def open_display(monkeypatch, rate=119.96):
    made = []

    class Visual:
        @staticmethod
        def Window(**kwargs):
            made.append(Window(rate, **kwargs))
            return made[-1]

    monkeypatch.setattr(pb, "resolve_monitor", lambda monitor: None)
    monkeypatch.setitem(sys.modules, "psychopy", types.SimpleNamespace(visual=Visual))
    monkeypatch.setitem(sys.modules, "psychopy.visual", Visual)
    _fake_pyglet(monkeypatch)
    display = pb.PsychoPyDisplay(MONITOR)
    display.open()
    return display, made[0]


def test_the_window_no_longer_measures_on_its_own(monkeypatch):
    display, window = open_display(monkeypatch)
    assert window.kwargs["checkTiming"] is False
    assert window.calls == []
    # Until the measurement, the window's bookkeeping holds the rig's nominal
    # rate, not PsychoPy's 60 Hz guess.
    assert window.monitorFramePeriod == pytest.approx(1 / 120)
    assert window.refreshThreshold == pytest.approx(1.2 / 120)


def test_one_quiet_bounded_measurement_feeds_the_window_and_the_session(monkeypatch):
    display, window = open_display(monkeypatch)
    rate = display.measure_refresh_rate(30)
    assert rate == pytest.approx(119.96)
    assert window.calls == [dict(nIdentical=10, nMaxFrames=30, nWarmUpFrames=10, infoMsg="")]
    assert window._monitorFrameRate == pytest.approx(119.96)
    assert window.monitorFramePeriod == pytest.approx(1 / 119.96)
    assert window.refreshThreshold == pytest.approx(1.2 / 119.96)


def test_an_unstable_display_is_still_refused(monkeypatch):
    display, _window = open_display(monkeypatch, rate=None)
    with pytest.raises(DisplayError, match="stable refresh rate"):
        display.measure_refresh_rate(30)


def test_a_wrong_rate_is_still_refused_against_the_rig():
    """The guard the measurement feeds is unchanged: a 60 Hz panel configured
    as 120 Hz is a loud error, never a session."""
    assert resolve_refresh(120.0, 119.96, 5.0) == pytest.approx(119.96)
    with pytest.raises(ConfigError):
        resolve_refresh(120.0, 60.0, 5.0)


def test_a_session_measures_exactly_once(tmp_path, monkeypatch):
    from tests.unit.test_builder import build

    from alhazen.core.events import EventSchema
    from alhazen.display.simulated import SimulatedDisplay

    calls = []
    real = SimulatedDisplay.measure_refresh_rate

    def counted(self, n_flips):
        calls.append(n_flips)
        return real(self, n_flips)

    monkeypatch.setattr(SimulatedDisplay, "measure_refresh_rate", counted)
    build(tmp_path, EventSchema(())).run()
    assert calls == [30]
