"""Reading Kilosort output: spike times, and which unit each belongs to.

Kilosort writes a directory of numpy arrays. Three of them matter here:
``spike_times.npy`` (in samples), ``spike_clusters.npy`` (which unit), and
``cluster_group.tsv`` (the human's judgement of each unit, after curation).

Sample indices become seconds through the recording's own sample rate — read
from the meta, never assumed, for the same reason as everywhere else in this
package: the nominal rate is not the real one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from alhazen.errors import DataError

log = logging.getLogger(__name__)

# What Kilosort's curation labels mean. "good" is a unit a human accepted;
# "mua" is multi-unit activity; "noise" is not a unit at all.
GOOD_LABELS = {"good"}


@dataclass
class SpikeData:
    """Sorted spikes, in recording-clock seconds."""

    times_s: np.ndarray
    clusters: np.ndarray
    cluster_groups: dict[int, str]

    @property
    def unit_ids(self) -> list[int]:
        return sorted(int(unit) for unit in np.unique(self.clusters))

    def good_units(self) -> list[int]:
        """Units a human curated as good. An uncurated sort has none, and
        saying so is better than quietly treating every cluster as a unit."""
        return sorted(
            unit for unit in self.unit_ids if self.cluster_groups.get(unit) in GOOD_LABELS
        )

    def times_of(self, unit: int) -> np.ndarray:
        """One unit's spike times."""
        return self.times_s[self.clusters == unit]


def read_kilosort(sort_dir: Path | str, sample_rate_hz: float) -> SpikeData:
    """Read a Kilosort output directory."""
    sort_dir = Path(sort_dir)
    times_path = sort_dir / "spike_times.npy"
    clusters_path = sort_dir / "spike_clusters.npy"
    for path in (times_path, clusters_path):
        if not path.exists():
            raise DataError(
                f"{path.name} is missing from {sort_dir} — this is not a Kilosort output "
                f"directory, or the sort did not finish"
            )
    if sample_rate_hz <= 0:
        raise DataError(f"sample rate must be positive, got {sample_rate_hz}")

    # Kilosort stores sample indices, sometimes as an (n, 1) column.
    samples = np.load(times_path).astype(np.int64).ravel()
    clusters = np.load(clusters_path).astype(np.int64).ravel()
    if samples.shape != clusters.shape:
        raise DataError(
            f"{sort_dir} has {samples.size} spike times but {clusters.size} cluster "
            f"assignments — the sort output is inconsistent"
        )

    groups = _read_cluster_groups(sort_dir)
    log.info(
        "%s: %d spikes, %d clusters (%d curated good)",
        sort_dir.name,
        samples.size,
        len(np.unique(clusters)),
        sum(1 for label in groups.values() if label in GOOD_LABELS),
    )
    return SpikeData(
        times_s=samples.astype(float) / sample_rate_hz,
        clusters=clusters,
        cluster_groups=groups,
    )


def _read_cluster_groups(sort_dir: Path) -> dict[int, str]:
    """The curator's labels, if anyone has curated this sort yet.

    Both filenames Kilosort/phy have used are accepted, newest first. An
    absent file means an uncurated sort, which is a normal state — the
    caller finds out by asking for ``good_units()`` and getting none.
    """
    for name in ("cluster_group.tsv", "cluster_groups.csv"):
        path = sort_dir / name
        if not path.exists():
            continue
        groups: dict[int, str] = {}
        for line in path.read_text().splitlines()[1:]:  # skip the header
            fields = line.replace(",", "\t").split("\t")
            if len(fields) < 2:
                continue
            try:
                groups[int(fields[0])] = fields[1].strip()
            except ValueError:
                continue
        return groups
    log.info("%s has no cluster group file — this sort has not been curated", sort_dir.name)
    return {}
