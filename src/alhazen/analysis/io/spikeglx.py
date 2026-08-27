"""Reading SpikeGLX files: the meta, and the digital lines the sync rides on.

SpikeGLX writes a raw ``.bin`` of interleaved int16 samples and a ``.meta``
text file describing it. Sync pulses arrive on the NI stream's packed 16-bit
*digital word*: one channel whose bits are the physical lines. Getting an
event's pulse times means reading one bit of that word.

Two things this module refuses to guess. The sample rate comes from the meta,
never from the nominal rate an experimenter remembers — the pilot rig's NI
stream runs at 24999.92 Hz, and using 25000 drifts 3 ms over ten minutes,
which is more than the effects these recordings measure. And the file is
memory-mapped, never loaded: an hour of Neuropixels is tens of gigabytes and
no analysis here needs more than one column at a time.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from alhazen.errors import DataError

log = logging.getLogger(__name__)

# SpikeGLX writes interleaved signed 16-bit samples, always.
SAMPLE_DTYPE = np.dtype("<i2")
# Samples read at a time when scanning for edges. Big enough that the scan is
# not dominated by per-chunk overhead, small enough to stay off the heap.
DEFAULT_CHUNK_SAMPLES = 1_000_000


def parse_meta(meta_path: Path | str) -> dict[str, str]:
    """A ``.meta`` file as a dict of its ``key=value`` lines.

    Keys are kept exactly as written, including SpikeGLX's ``~`` prefixes, so
    a caller looking for a documented key name finds it.
    """
    meta_path = Path(meta_path)
    if not meta_path.exists():
        raise DataError(f"SpikeGLX meta file not found: {meta_path}")
    meta: dict[str, str] = {}
    for line in meta_path.read_text().splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        meta[key.strip()] = value.strip()
    return meta


def sample_rate_hz(meta: dict[str, str]) -> float:
    """The stream's actual sampling rate.

    ``imSampRate`` for a Neuropixels stream, ``niSampRate`` for the NI-DAQ
    one. Which key is present says which stream this is.
    """
    for key in ("imSampRate", "niSampRate", "fileSampRate"):
        if key in meta:
            try:
                return float(meta[key])
            except ValueError as error:
                raise DataError(f"{key}={meta[key]!r} is not a number") from error
    raise DataError(
        f"no sample-rate field in this meta (looked for imSampRate, niSampRate, "
        f"fileSampRate); present keys: {sorted(meta)[:20]}"
    )


def channel_count(meta: dict[str, str]) -> int:
    if "nSavedChans" not in meta:
        raise DataError(f"meta has no nSavedChans; present keys: {sorted(meta)[:20]}")
    return int(meta["nSavedChans"])


def has_digital_word(meta: dict[str, str]) -> bool:
    """Whether the last saved channel is the packed digital word.

    ``acqMnMaXaDw`` counts multiplexed-neural, multiplexed-aux, analog and
    digital-word channels; a non-zero fourth entry means the word is there.
    """
    spec = meta.get("acqMnMaXaDw")
    if not spec:
        return False
    parts = [part.strip() for part in spec.split(",")]
    if len(parts) != 4:
        return False
    try:
        return int(parts[3]) > 0
    except ValueError:
        return False


def n_samples(bin_path: Path | str, meta: dict[str, str]) -> int:
    """Sample count, from the file's size.

    A file that is not a whole number of frames is truncated, or the meta
    beside it belongs to a different recording. Either way the caller must
    hear about it before any of it is interpreted as data.
    """
    bin_path = Path(bin_path)
    if not bin_path.exists():
        raise DataError(f"SpikeGLX binary not found: {bin_path}")
    frame_bytes = channel_count(meta) * SAMPLE_DTYPE.itemsize
    file_bytes = bin_path.stat().st_size
    if file_bytes % frame_bytes != 0:
        raise DataError(
            f"{bin_path} is {file_bytes} bytes, not a whole number of {frame_bytes}-byte "
            f"frames ({channel_count(meta)} channels x {SAMPLE_DTYPE.itemsize} bytes) — "
            f"the file is truncated, or this meta describes a different recording"
        )
    return file_bytes // frame_bytes


def memmap_bin(bin_path: Path | str, meta: dict[str, str]) -> np.memmap:
    """A lazily-read ``(n_samples, n_channels)`` view of the binary."""
    return np.memmap(
        bin_path,
        mode="r",
        dtype=SAMPLE_DTYPE,
        shape=(n_samples(bin_path, meta), channel_count(meta)),
    )


def digital_word_edges(
    bin_path: Path | str,
    meta_path: Path | str,
    *,
    bit_index: int,
    edge: str = "rising",
    word_channel: int | None = None,
    chunk_samples: int = DEFAULT_CHUNK_SAMPLES,
) -> np.ndarray:
    """Times, in seconds from the recording's start, of transitions on one bit.

    ``bit_index`` is a physical DAQ line, derived from the run's own config
    (analysis/sync.py) rather than hardcoded anywhere.
    """
    if not 0 <= bit_index <= 15:
        raise DataError(f"bit_index must be 0..15 for a 16-bit word, got {bit_index}")
    if edge not in ("rising", "falling", "both"):
        raise DataError(f"edge must be 'rising', 'falling' or 'both', got {edge!r}")

    meta = parse_meta(meta_path)
    rate = sample_rate_hz(meta)
    if word_channel is None:
        if not has_digital_word(meta):
            raise DataError(
                f"{meta_path} declares acqMnMaXaDw={meta.get('acqMnMaXaDw')!r}, which has "
                f"no digital-word channel — this recording carries no sync lines"
            )
        # SpikeGLX writes the digital word last.
        word_channel = channel_count(meta) - 1

    data = memmap_bin(bin_path, meta)
    mask = np.uint16(1 << bit_index)
    found: list[np.ndarray] = []
    # The last sample of the previous chunk, so an edge that falls exactly on
    # a chunk boundary is still seen. Without it, one edge per boundary
    # silently disappears — and a dropped pulse is exactly what alignment
    # cannot recover from.
    carried: np.ndarray | None = None

    for start in range(0, data.shape[0], chunk_samples):
        stop = min(start + chunk_samples, data.shape[0])
        word = np.asarray(data[start:stop, word_channel]).astype(np.uint16)
        high = (word & mask) != 0
        if carried is None:
            series, offset = high, 0
        else:
            series, offset = np.concatenate([carried, high]), 1
        if edge in ("rising", "both"):
            # flatnonzero finds the LOW sample of each (low, high) pair; +1
            # reports the first HIGH sample, which is when the line went up.
            found.append(np.flatnonzero(~series[:-1] & series[1:]) + 1 - offset + start)
        if edge in ("falling", "both"):
            found.append(np.flatnonzero(series[:-1] & ~series[1:]) + 1 - offset + start)
        carried = high[-1:]

    if not found:
        return np.empty(0, dtype=float)
    indices = np.sort(np.concatenate(found))
    log.info("bit %d: %d %s edges over %.1f s", bit_index, len(indices), edge, data.shape[0] / rate)
    return indices.astype(float) / rate


def analog_channel(
    bin_path: Path | str, meta_path: Path | str, *, channel: int
) -> tuple[np.ndarray, float]:
    """One analog channel and its sample rate — the photodiode's trace.

    Returned as raw int16 counts rather than volts: the photodiode analysis
    only needs edges, and converting to volts would need a gain this module
    would have to guess.
    """
    meta = parse_meta(meta_path)
    data = memmap_bin(bin_path, meta)
    if not 0 <= channel < data.shape[1]:
        raise DataError(f"channel {channel} is outside this recording's {data.shape[1]} channels")
    return np.asarray(data[:, channel]), sample_rate_hz(meta)


def find_run_files(run_dir: Path | str) -> dict[str, Path]:
    """The ``.bin``/``.meta`` pair of the NI stream in a SpikeGLX run.

    The NI stream is the one carrying sync; a run also holds the much larger
    neural streams, which nothing here reads.
    """
    run_dir = Path(run_dir)
    binaries = sorted(run_dir.rglob("*.nidq.bin"))
    if not binaries:
        raise DataError(
            f"no *.nidq.bin under {run_dir} — sync pulses are recorded on the NI stream, "
            f"and this run has none"
        )
    if len(binaries) > 1:
        raise DataError(
            f"{run_dir} holds {len(binaries)} NI binaries ({[p.name for p in binaries]}); "
            f"point at the single run whose sync you want"
        )
    bin_path = binaries[0]
    meta_path = bin_path.with_suffix(".meta")
    if not meta_path.exists():
        raise DataError(f"{bin_path.name} has no matching .meta beside it")
    return {"bin_path": bin_path, "meta_path": meta_path}
