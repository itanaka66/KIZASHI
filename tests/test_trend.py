import numpy as np

from kizashi.models.trend import fit_trend

HOUR = 3600


def degrading_optic(slope_per_hour=-0.1, start=-8.0, hours=6, noise=0.05, seed=0):
    rng = np.random.default_rng(seed)
    ts = np.arange(0, hours * HOUR, 60.0) + 1_700_000_000
    values = start + slope_per_hour * (ts - ts[0]) / HOUR + rng.normal(0, noise, len(ts))
    return ts, values


def test_recovers_slope():
    ts, v = degrading_optic()
    fit = fit_trend(ts, v)
    assert abs(fit.slope * HOUR - (-0.1)) < 0.005
    assert fit.slope_lo <= fit.slope <= fit.slope_hi < 0
    assert abs(fit.intercept - (-8.6)) < 0.05


def test_time_to_threshold():
    ts, v = degrading_optic()
    fit = fit_trend(ts, v)
    # -8.6 now, -0.1 dBm/h: reaches -14.4 in ~58h.
    eta = fit.time_to_threshold(-14.4, "below", max_horizon=30 * 86400)
    assert abs(eta / HOUR - 58) < 3
    # Falling, so it never climbs above -5.
    assert fit.time_to_threshold(-5.0, "above", max_horizon=30 * 86400) is None


def test_already_beyond_threshold():
    ts, v = degrading_optic(start=-15)
    assert fit_trend(ts, v).time_to_threshold(-14.4, "below", 1e9) == 0.0


def test_beyond_max_horizon():
    ts, v = degrading_optic()
    assert fit_trend(ts, v).time_to_threshold(-14.4, "below", max_horizon=24 * HOUR) is None


def test_flat_noisy_series_has_no_eta():
    ts, v = degrading_optic(slope_per_hour=0, noise=0.5)
    fit = fit_trend(ts, v)
    assert fit.slope_lo < 0 < fit.slope_hi
    assert fit.time_to_threshold(-14.4, "below", 1e9) is None


def test_robust_to_spikes():
    ts, v = degrading_optic()
    v[-20:-15] = 5.0  # transient spikes at the end would wreck a least-squares fit
    fit = fit_trend(ts, v)
    assert abs(fit.slope * HOUR - (-0.1)) < 0.01


def test_forecast_band_widens():
    ts, v = degrading_optic()
    fit = fit_trend(ts, v)
    lo1, mid1, hi1 = fit.forecast(HOUR)
    lo24, mid24, hi24 = fit.forecast(24 * HOUR)
    assert lo1 < mid1 < hi1
    assert abs(mid24 - (-8.6 - 2.4)) < 0.2
    assert hi24 - lo24 > hi1 - lo1


def test_too_few_points():
    ts, v = degrading_optic(hours=0.1)
    assert fit_trend(ts, v, min_points=10) is None


def test_constant_series():
    ts = np.arange(0, 3600, 60.0)
    fit = fit_trend(ts, np.full(len(ts), 3.0))
    assert fit.slope == 0
    assert fit.time_to_threshold(5.0, "above", 1e9) is None
