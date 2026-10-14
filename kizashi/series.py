"""Time series container shared by sources and models."""

from __future__ import annotations

import zlib
from dataclasses import dataclass

import numpy as np


def series_key(labels: dict[str, str]) -> str:
    """Canonical identity of a series, excluding the metric name."""
    return ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()) if k != "__name__")


def shard_of(key: str, total: int) -> int:
    return zlib.crc32(key.encode()) % total


@dataclass
class Series:
    labels: dict[str, str]
    timestamps: np.ndarray  # unix seconds, ascending
    values: np.ndarray

    @property
    def key(self) -> str:
        return series_key(self.labels)

    def finite(self) -> "Series":
        mask = np.isfinite(self.values)
        return Series(self.labels, self.timestamps[mask], self.values[mask])

    def __len__(self) -> int:
        return len(self.values)
