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

from alhazen.devices.eyetracker import (
    EyeLinkTracker,
    EyeTracker,
    GazeSample,
    HostShape,
    MouseSimTracker,
    ScriptedTracker,
    TrackerMessageSubscriber,
    ViewPixxTracker,
    make_tracker,
)
from alhazen.devices.response import (
    NullResponse,
    ResponseDevice,
    ResponseSample,
    SubjectKeyboard,
)
from alhazen.devices.reward import (
    NidaqReward,
    RewardDispenser,
    SimulatedReward,
    build_reward_waveform,
    make_reward,
)
from alhazen.devices.sync import (
    NidaqSync,
    NullSync,
    SimulatedSync,
    SyncOutput,
    make_sync,
    make_sync_subscriber,
)

__all__ = [
    "EyeLinkTracker",
    "EyeTracker",
    "GazeSample",
    "HostShape",
    "MouseSimTracker",
    "NidaqReward",
    "NullResponse",
    "NidaqSync",
    "ResponseDevice",
    "ResponseSample",
    "RewardDispenser",
    "ScriptedTracker",
    "SimulatedReward",
    "NullSync",
    "SimulatedSync",
    "SubjectKeyboard",
    "SyncOutput",
    "TrackerMessageSubscriber",
    "ViewPixxTracker",
    "build_reward_waveform",
    "make_reward",
    "make_sync",
    "make_sync_subscriber",
    "make_tracker",
]
