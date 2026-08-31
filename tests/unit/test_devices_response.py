"""The subject's own input: keys and a wheel, and how they reach a phase.

The device -> InputFrame layer had no coverage at all. It is the seam where a
subject's press becomes data, and where gaze changes coordinate frame — the
one conversion the whole geometry depends on.
"""

from __future__ import annotations

import pytest

from alhazen.core.trial import InputFrame
from alhazen.devices.response import (
    NullResponse,
    ResponseDevice,
    ResponseSample,
    SubjectKeyboard,
)
from alhazen.session.builder import make_input_provider
from support import SCREEN


class ScriptedKeys:
    """A key getter shaped like psychopy's: batches, one per poll."""

    def __init__(self, batches: list[list[str]]) -> None:
        self.batches = list(batches)
        self.calls = 0

    def __call__(self) -> list[str]:
        self.calls += 1
        return self.batches.pop(0) if self.batches else []


class TestNullResponse:
    def test_it_reports_nothing_forever(self):
        device = NullResponse()

        for _ in range(3):
            assert device.poll() == ResponseSample(keys=(), wheel=0.0)

    def test_it_satisfies_the_protocol(self):
        assert isinstance(NullResponse(), ResponseDevice)


class TestSubjectKeyboard:
    def test_presses_since_the_last_poll_come_back_in_order(self):
        """A list, not a single key: a fast double-press inside one frame
        must not be silently dropped."""
        device = SubjectKeyboard(key_getter=ScriptedKeys([["left", "right"], ["left"]]))

        assert device.poll().keys == ("left", "right")
        assert device.poll().keys == ("left",)
        assert device.poll().keys == ()

    def test_each_press_is_reported_once(self):
        getter = ScriptedKeys([["space"]])
        device = SubjectKeyboard(key_getter=getter)

        assert device.poll().keys == ("space",)
        assert device.poll().keys == ()
        assert getter.calls == 2

    def test_the_wheel_comes_back_with_the_keys(self):
        device = SubjectKeyboard(
            key_getter=ScriptedKeys([["up"]]),
            wheel_getter=lambda: 2.5,
        )

        sample = device.poll()

        assert sample.keys == ("up",) and sample.wheel == pytest.approx(2.5)

    def test_a_rig_with_no_wheel_reports_zero(self):
        device = SubjectKeyboard(key_getter=ScriptedKeys([[]]))

        assert device.poll().wheel == 0.0

    def test_it_satisfies_the_protocol(self):
        assert isinstance(SubjectKeyboard(key_getter=ScriptedKeys([])), ResponseDevice)

    def test_constructing_it_imports_no_renderer(self):
        # Lazy vendor imports (invariant 7): psychopy is touched only when a
        # real read happens, so `import alhazen` and the default suite work
        # with none of it installed.
        #
        # Checked in a subprocess, because the invariant is about a fresh
        # import state: asserted in-process it fails whenever an earlier test
        # in the suite's ordering has already loaded psychopy — which is the
        # ordering, not the device, and made this the suite's one flaky test.
        import subprocess
        import sys

        probe = (
            "import sys\n"
            "from alhazen.devices.response import SubjectKeyboard\n"
            "SubjectKeyboard(keys=('left', 'right'), window=object())\n"
            "assert 'psychopy' not in sys.modules, 'constructing it imported psychopy'\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120
        )
        assert result.returncode == 0, result.stderr


class FakeTracker:
    def __init__(self, sample):
        self._sample = sample

    def get_gaze(self):
        return self._sample


class Sample:
    def __init__(self, gx, gy):
        self.gx, self.gy = gx, gy


class TestInputProviderMergesTheDevices:
    def test_no_devices_means_no_provider(self):
        # The engine keeps its own empty-frame default rather than calling a
        # closure that would only ever return the same thing.
        assert make_input_provider(SCREEN) is None

    def test_gaze_only(self):
        provider = make_input_provider(SCREEN, tracker=FakeTracker(Sample(960.0, 540.0)))

        frame = provider()

        assert frame.gaze == (0.0, 0.0)  # screen centre in centered px
        assert frame.keys == () and frame.wheel == 0.0

    def test_response_only(self):
        provider = make_input_provider(
            SCREEN,
            response=SubjectKeyboard(
                key_getter=ScriptedKeys([["left"]]), wheel_getter=lambda: -1.5
            ),
        )

        frame = provider()

        assert frame.gaze is None
        assert frame.keys == ("left",)
        assert frame.wheel == pytest.approx(-1.5)

    def test_both_land_in_one_frame(self):
        provider = make_input_provider(
            SCREEN,
            tracker=FakeTracker(Sample(1160.0, 540.0)),
            response=SubjectKeyboard(key_getter=ScriptedKeys([["right"]])),
        )

        frame = provider()

        assert frame.gaze == (200.0, 0.0)
        assert frame.keys == ("right",)

    def test_screen_px_become_centered_px_with_y_up(self):
        """Trackers report y growing DOWN from the top-left; phases read y
        growing UP from the centre. A second conversion anywhere else is how
        a task ends up silently mirrored about the horizontal midline."""
        provider = make_input_provider(SCREEN, tracker=FakeTracker(Sample(960.0, 340.0)))

        # 200 px above the centre on screen is +200 in centered coordinates.
        assert provider().gaze == (0.0, 200.0)

    def test_no_gaze_sample_stays_none(self):
        """The blink rule: an unverifiable position stays unverifiable, never
        a guess at the last known one."""
        provider = make_input_provider(
            SCREEN,
            tracker=FakeTracker(None),
            response=SubjectKeyboard(key_getter=ScriptedKeys([["a"]])),
        )

        frame = provider()

        assert frame.gaze is None
        assert frame.keys == ("a",)  # and the rest of the frame is still real

    def test_the_frame_is_the_engines_own_type(self):
        provider = make_input_provider(SCREEN, response=NullResponse())
        assert isinstance(provider(), InputFrame)
