"""Device backends: one protocol per device class, one real backend, one
simulated, one deterministic double.

Three rules hold for everything in here:

- vendor SDKs (pylink, nidaqmx, psychopy) are imported inside the method that
  needs them, so ``import alhazen`` and the whole default test suite work
  with none of them installed;
- a missing SDK or a dead device raises a typed alhazen error naming what to
  install or check, at use time, never a bare ImportError;
- nothing below this layer imports devices. Only ``session/builder.py`` wires
  them, which is why the engine and every phase stay hardware-free.
"""

from __future__ import annotations

from alhazen.devices.eyetracker import EyeLinkTracker as EyeLinkTracker
from alhazen.devices.eyetracker import EyeTracker, GazeSample, HostShape, TrackerMessageSubscriber
from alhazen.devices.eyetracker import MouseSimTracker as MouseSimTracker
from alhazen.devices.eyetracker import ScriptedTracker as ScriptedTracker
from alhazen.devices.eyetracker import ViewPixxTracker as ViewPixxTracker
from alhazen.devices.eyetracker import make_tracker as make_tracker
from alhazen.devices.response import NullResponse as NullResponse
from alhazen.devices.response import ResponseDevice, ResponseSample
from alhazen.devices.response import SubjectKeyboard as SubjectKeyboard
from alhazen.devices.reward import NidaqReward as NidaqReward
from alhazen.devices.reward import RewardDispenser, SimulatedReward
from alhazen.devices.reward import build_reward_waveform as build_reward_waveform
from alhazen.devices.reward import make_reward as make_reward
from alhazen.devices.sync import NidaqSync as NidaqSync
from alhazen.devices.sync import NullSync as NullSync
from alhazen.devices.sync import SimulatedSync, SyncOutput
from alhazen.devices.sync import make_sync as make_sync
from alhazen.devices.sync import make_sync_subscriber as make_sync_subscriber

# `__all__` holds only the names docs/reference.md lists as public (a test in
# tests/unit/test_docs_snippets.py holds it to that). The `X as X` imports
# above are internal: they stay importable from here, for code that already
# imports them this way, but are not exported.
__all__ = [
    "EyeTracker",
    "GazeSample",
    "HostShape",
    "ResponseDevice",
    "ResponseSample",
    "RewardDispenser",
    "SimulatedReward",
    "SimulatedSync",
    "SyncOutput",
    "TrackerMessageSubscriber",
]
