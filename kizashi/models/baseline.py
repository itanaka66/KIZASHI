"""Layer 1: adaptive thresholds from a seasonal baseline.

The expected value for a point in time is the median of historical values in the
same seasonal bin (hour of week, or hour of day when there is not enough history).
The spread is the median absolute deviation (MAD) of the residuals, which keeps
past incidents from inflating the band the way a standard deviation would.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

HOUR = 3600
DAY = 24 * HOUR
WEEK = 7 * DAY
MAD_TO_SIGMA = 1.4826

SEASONS = {"week": (WEEK, 168), "day": (DAY, 24)}


@dataclass
class BaselineModel:
    season: str  # "week" or "day"
    profile: list[float]  # expected value per hourly bin
    mad: float
    n_points: int

    def bin_of(self, ts: float | np.ndarray) -> np.ndarray:
        period, bins = SEASONS[self.season]
        return ((np.asarray(ts) % period) // HOUR).astype(int) % bins

    def expected(self, ts: float) -> float:
        return self.profile[int(self.bin_of(ts))]

    def sigma(self) -> float:
        return MAD_TO_SIGMA * self.mad

    def score(self, ts: float, value: float, band_sigma: float = 3.0) -> "BaselineResult":
        expected = self.expected(ts)
        sigma = self.sigma()
        return BaselineResult(
            expected=expected,
            lower=expected - band_sigma * sigma,
            upper=expected + band_sigma * sigma,
            score=abs(value - expected) / sigma,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BaselineModel":
        return cls(season=d["season"], profile=list(d["profile"]), mad=float(d["mad"]), n_points=int(d["n_points"]))


@dataclass
class BaselineResult:
    expected: float
    lower: float
    upper: float
    score: float


def fit_baseline(timestamps: np.ndarray, values: np.ndarray, min_periods: int = 2) -> BaselineModel | None:
    """Fit a seasonal baseline; returns None when there is not enough history.

    A weekly profile needs at least `min_periods` weeks of data, otherwise a daily
    profile is used if at least `min_periods` days are available.
    """
    mask = np.isfinite(values)
    ts, vals = timestamps[mask], values[mask]
    if len(vals) < 2:
        return None
    span = ts[-1] - ts[0]
    if span >= min_periods * WEEK:
        season = "week"
    elif span >= min_periods * DAY:
        season = "day"
    else:
        return None

    period, n_bins = SEASONS[season]
    bins = ((ts % period) // HOUR).astype(int) % n_bins
    overall = float(np.median(vals))
    profile = np.full(n_bins, overall)
    for b in range(n_bins):
        in_bin = vals[bins == b]
        if len(in_bin):
            profile[b] = np.median(in_bin)

    residuals = vals - profile[bins]
    mad = float(np.median(np.abs(residuals - np.median(residuals))))
    # A flat series has MAD 0; floor it so any change yields a finite (large) score.
    mad = max(mad, 1e-3 * abs(overall), 1e-9)
    return BaselineModel(season=season, profile=profile.tolist(), mad=mad, n_points=len(vals))
