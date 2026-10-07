"""Seed discipline.

One session seed, resolved to a concrete integer before anything random
happens, then *spawned* into genuinely independent named streams — one per
subsystem (scheduler, placement, task, ...). Spawning, rather than reseeding
``default_rng(seed)`` per subsystem, is what keeps the streams independent
instead of byte-identical-but-offset; naming them is what keeps "add a
subsystem" from silently shifting every other subsystem's draws.

Module-level ``np.random`` is never used anywhere in this package — it is
process-global state a session seed cannot isolate.
"""

from __future__ import annotations

import secrets

import numpy as np

# Fixed stream names, in fixed order: reproducibility of a session from its
# seed depends on this list being stable. New subsystems APPEND here — never
# insert, reorder, or remove — so existing (seed -> stream) mappings survive
# framework upgrades.
STREAMS = ("scheduler", "session", "task", "calibration_target")


def resolve_seed(seed: int | None) -> int:
    """A concrete seed, always: the caller's if given, otherwise a fresh
    random one — which the snapshot records, so an unseeded session is still
    exactly reproducible after the fact."""
    return secrets.randbits(32) if seed is None else seed


def spawn_streams(seed: int) -> dict[str, np.random.Generator]:
    """One generator per name in STREAMS, each from its own child of the seed.

    A child depends only on the seed and its position in STREAMS (numpy's
    spawn keys), never on how many there are, so appending a name leaves
    every existing stream's draws exactly as they were."""
    children = np.random.SeedSequence(seed).spawn(len(STREAMS))
    return {name: np.random.default_rng(seq) for name, seq in zip(STREAMS, children, strict=True)}


def named_stream(seed: int, name: str) -> np.random.Generator:
    """The one stream ``name`` of ``spawn_streams(seed)``, alone: identical
    draws, for a part of the session built before the others (the eye
    tracker's calibration targets)."""
    index = STREAMS.index(name)
    child = np.random.SeedSequence(seed).spawn(index + 1)[index]
    return np.random.default_rng(child)
