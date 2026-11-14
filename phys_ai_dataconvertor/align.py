"""
Nearest-timestamp alignment of one stream onto the ticks of another.

Each reference tick (a camera frame) takes the source sample (a joint reading) closest to it in time.
The distance is kept, and a tick whose nearest sample is further than the tolerance gets no value
rather than a stale one: the caller decides whether to drop the frame or fail.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Alignment:
    index: np.ndarray       # per tick: index of the nearest sample, -1 when outside the tolerance
    age_ns: np.ndarray      # per tick: |tick - sample time| in ns (of the nearest sample, even when rejected)
    matched: np.ndarray     # per tick: True when within the tolerance

    @property
    def n_unmatched(self) -> int:
        return int((~self.matched).sum())


def nearest(ticks_ns: np.ndarray, sample_ns: np.ndarray, tolerance_ns: int) -> Alignment:
    """Match every tick to the nearest sample. Both arrays must be sorted ascending; ties go to the
    earlier sample, so a value is never taken from after it could have been known when both are
    equally close."""
    ticks_ns = np.asarray(ticks_ns, dtype=np.int64)
    sample_ns = np.asarray(sample_ns, dtype=np.int64)
    if len(sample_ns) == 0:
        n = len(ticks_ns)
        return Alignment(np.full(n, -1), np.full(n, np.iinfo(np.int64).max), np.zeros(n, dtype=bool))
    if np.any(np.diff(sample_ns) < 0) or np.any(np.diff(ticks_ns) < 0):
        raise ValueError("timestamps must be sorted")
    right = np.searchsorted(sample_ns, ticks_ns, side="left").clip(0, len(sample_ns) - 1)
    left = (right - 1).clip(0, len(sample_ns) - 1)
    d_right = np.abs(sample_ns[right] - ticks_ns)
    d_left = np.abs(ticks_ns - sample_ns[left])
    idx = np.where(d_left <= d_right, left, right)
    age = np.minimum(d_left, d_right)
    ok = age <= tolerance_ns
    return Alignment(np.where(ok, idx, -1), age, ok)
