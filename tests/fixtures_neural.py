"""Synthetic device files, with known contents.

Every reader and the alignment are tested against files this module wrote, so
a test can say exactly what should come out: "the pulses are at these times,
recover them" rather than "it parsed without crashing". A real recording
could only ever support the second kind of claim.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from alhazen.analysis.io.spikeglx import SAMPLE_DTYPE

# A plausible NI-DAQ stream: a few analog channels plus the packed digital
# word SpikeGLX writes last.
DEFAULT_RATE_HZ = 25000.0
DEFAULT_ANALOG_CHANNELS = 2


def write_nidq(
    run_dir: Path,
    pulses: dict[int, list[float]],
    duration_s: float = 10.0,
    rate_hz: float = DEFAULT_RATE_HZ,
    pulse_width_s: float = 0.002,
    analog_channels: int = DEFAULT_ANALOG_CHANNELS,
    analog_edges: list[float] | None = None,
    analog_high_s: float = 0.01,
    name: str = "sim_g0_t0",
) -> dict[str, Path]:
    """Write a ``.nidq.bin``/``.meta`` pair with pulses at known times.

    ``pulses`` maps a digital-word bit to the times, in seconds, at which
    that line goes high. ``analog_edges`` optionally plants square pulses on
    analog channel 0 — a stand-in photodiode, so a planted display latency
    can be recovered.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    n_samples = int(duration_s * rate_hz)
    n_channels = analog_channels + 1  # + the digital word

    data = np.zeros((n_samples, n_channels), dtype=SAMPLE_DTYPE)

    # -- the digital word ------------------------------------------------
    word = np.zeros(n_samples, dtype=np.uint16)
    width = max(int(pulse_width_s * rate_hz), 1)
    for bit, times in pulses.items():
        mask = np.uint16(1 << bit)
        for time in times:
            start = int(round(time * rate_hz))
            word[start : start + width] |= mask
    # The word is stored in the same int16 samples as everything else, so a
    # bit-15 pulse would read as a negative number; view rather than cast so
    # the bits survive the trip.
    data[:, -1] = word.view(np.int16)

    # -- an optional photodiode trace -----------------------------------
    if analog_edges:
        high_samples = max(int(analog_high_s * rate_hz), 1)
        for time in analog_edges:
            start = int(round(time * rate_hz))
            data[start : start + high_samples, 0] = 20000  # a bright patch

    bin_path = run_dir / f"{name}.nidq.bin"
    data.tofile(bin_path)

    meta_path = run_dir / f"{name}.nidq.meta"
    meta_path.write_text(
        "\n".join(
            [
                f"niSampRate={rate_hz}",
                f"nSavedChans={n_channels}",
                # multiplexed-neural, multiplexed-aux, analog, digital-word.
                f"acqMnMaXaDw=0,0,{analog_channels},1",
                f"fileTimeSecs={duration_s}",
                "typeThis=nidq",
            ]
        )
        + "\n"
    )
    return {"bin_path": bin_path, "meta_path": meta_path}


def write_kilosort(
    sort_dir: Path,
    spikes: dict[int, list[float]],
    rate_hz: float = 30000.0,
    good_units: set[int] | None = None,
) -> Path:
    """Write a Kilosort output directory with known spike times per unit."""
    sort_dir = Path(sort_dir)
    sort_dir.mkdir(parents=True, exist_ok=True)

    times: list[int] = []
    clusters: list[int] = []
    for unit, unit_times in sorted(spikes.items()):
        for time in unit_times:
            times.append(int(round(time * rate_hz)))
            clusters.append(unit)
    order = np.argsort(times)  # Kilosort writes spikes in time order
    np.save(sort_dir / "spike_times.npy", np.asarray(times)[order].reshape(-1, 1))
    np.save(sort_dir / "spike_clusters.npy", np.asarray(clusters)[order])

    if good_units is not None:
        lines = ["cluster_id\tgroup"]
        for unit in sorted(spikes):
            lines.append(f"{unit}\t{'good' if unit in good_units else 'mua'}")
        (sort_dir / "cluster_group.tsv").write_text("\n".join(lines) + "\n")
    return sort_dir


def write_asc(
    path: Path,
    samples: list[tuple[float, float, float]],
    messages: list[tuple[float, str]],
    blinks_at: set[float] | None = None,
) -> Path:
    """Write an EyeLink ASC file with known samples and messages.

    ``blinks_at`` writes the "no eye" marker for those sample times, which is
    what a real blink looks like in an ASC.
    """
    path = Path(path)
    blinks_at = blinks_at or set()
    lines = ["** CONVERTED FROM a synthetic fixture **", "START\t1\tRIGHT\tSAMPLES\tEVENTS"]
    for time, text in messages:
        lines.append(f"MSG\t{time:.0f}\t{text}")
    for time, x, y in samples:
        if time in blinks_at:
            lines.append(f"{time:.0f}\t   .\t   .\t    0.0\t...")
        else:
            lines.append(f"{time:.0f}\t{x:.1f}\t{y:.1f}\t{1000.0:.1f}\t...")
    lines.append("END\t1\tSAMPLES\tEVENTS")
    path.write_text("\n".join(lines) + "\n")
    return path
