"""Layer 2: robust trend forecasting and time-to-threshold.

Uses the Theil-Sen estimator (median of pairwise slopes) instead of the least
squares fit behind PromQL's predict_linear(), so a few spikes do not swing the
forecast. Sen's confidence interval on the slope decides whether a trend is real
enough to report a time-to-threshold.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np


@dataclass
class TrendFit:
    slope: float  # units per second
    slope_lo: float
    slope_hi: float
    intercept: float  # fitted value at the last timestamp
    resid_lo: float  # residual quantiles used for the forecast band
    resid_hi: float
    n_points: int

    def forecast(self, horizon: float) -> tuple[float, float, float]:
        """Return (lower, median, upper) at `horizon` seconds after the last sample."""
        median = self.intercept + self.slope * horizon
        a, b = self.slope_lo * horizon, self.slope_hi * horizon
        return self.intercept + min(a, b) + self.resid_lo, median, self.intercept + max(a, b) + self.resid_hi

    def time_to_threshold(self, threshold: float, direction: str, max_horizon: float) -> float | None:
        """Seconds until the trend crosses `threshold`, or None if it is not heading there.

        Only reported when the whole slope confidence interval points toward the
        threshold, so flat or noisy series do not produce spurious predictions.
        """
        if direction == "below":
            if self.intercept <= threshold:
                return 0.0
            if self.slope_hi >= 0:
                return None
        else:
            if self.intercept >= threshold:
                return 0.0
            if self.slope_lo <= 0:
                return None
        eta = (threshold - self.intercept) / self.slope
        return eta if 0 <= eta <= max_horizon else None


def fit_trend(
    timestamps: np.ndarray,
    values: np.ndarray,
    confidence: float = 0.9,
    max_points: int = 120,
    min_points: int = 10,
) -> TrendFit | None:
    mask = np.isfinite(values)
    ts, vals = timestamps[mask], values[mask]
    n = len(vals)
    if n < min_points:
        return None
    if n > max_points:
        # Pairwise slopes are O(n^2); thin evenly but always keep the latest sample.
        idx = np.unique(np.linspace(0, n - 1, max_points).round().astype(int))
        ts, vals = ts[idx], vals[idx]
        n = len(vals)

    x = ts - ts[-1]
    i, j = np.triu_indices(n, k=1)
    dx = x[j] - x[i]
    valid = dx != 0
    slopes = np.sort((vals[j] - vals[i])[valid] / dx[valid])
    if len(slopes) == 0:
        return None
    slope = float(np.median(slopes))
    intercept = float(np.median(vals - slope * x))

    # Sen's (1968) confidence interval for the slope, without tie correction.
    n_pairs = len(slopes)
    z = NormalDist().inv_cdf(0.5 + confidence / 2)
    sigma = math.sqrt(n * (n - 1) * (2 * n + 5) / 18)
    lo_idx = int(np.clip(round((n_pairs - z * sigma) / 2), 0, n_pairs - 1))
    hi_idx = int(np.clip(round((n_pairs + z * sigma) / 2) - 1, 0, n_pairs - 1))
    slope_lo, slope_hi = float(slopes[lo_idx]), float(slopes[max(hi_idx, lo_idx)])

    resid = vals - (intercept + slope * x)
    tail = (1 - confidence) / 2
    resid_lo, resid_hi = np.quantile(resid, [tail, 1 - tail])

    return TrendFit(
        slope=slope,
        slope_lo=slope_lo,
        slope_hi=slope_hi,
        intercept=intercept,
        resid_lo=float(resid_lo),
        resid_hi=float(resid_hi),
        n_points=n,
    )
