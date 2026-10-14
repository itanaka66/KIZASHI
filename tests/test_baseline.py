import numpy as np

from kizashi.models.baseline import DAY, HOUR, WEEK, BaselineModel, fit_baseline


def daily_pattern(days: float, step: float = 300, noise: float = 0.5, seed: int = 0):
    rng = np.random.default_rng(seed)
    ts = np.arange(0, days * DAY, step)
    hour = (ts % DAY) / HOUR
    values = 50 + 30 * (hour >= 9) * (hour < 18) + rng.normal(0, noise, len(ts))
    return ts, values


def test_too_little_history():
    ts, v = daily_pattern(1.5)
    assert fit_baseline(ts, v) is None


def test_daily_season_when_short():
    ts, v = daily_pattern(5)
    assert fit_baseline(ts, v).season == "day"


def test_weekly_profile_follows_pattern():
    ts, v = daily_pattern(21)
    m = fit_baseline(ts, v)
    assert m.season == "week"
    assert len(m.profile) == 168
    business, night = 3 * WEEK + 12 * HOUR, 3 * WEEK + 3 * HOUR
    assert abs(m.expected(business) - 80) < 1
    assert abs(m.expected(night) - 50) < 1


def test_score_flags_deviation_relative_to_time_of_day():
    ts, v = daily_pattern(21)
    m = fit_baseline(ts, v)
    noon, night = 3 * WEEK + 12 * HOUR, 3 * WEEK + 3 * HOUR
    assert m.score(noon, 80).score < 1
    # 80 is normal at noon but anomalous at 3am.
    r = m.score(night, 80)
    assert r.score > 10
    assert r.lower < 50 < r.upper < 80


def test_robust_to_outliers():
    ts, v = daily_pattern(21)
    v[100:130] = 10_000  # a past incident
    m = fit_baseline(ts, v)
    assert m.mad < 2


def test_flat_series_has_floor():
    ts = np.arange(0, 3 * DAY, 300.0)
    m = fit_baseline(ts, np.full(len(ts), 100.0))
    assert m.mad > 0
    assert np.isfinite(m.score(ts[-1], 101).score)


def test_nan_ignored():
    ts, v = daily_pattern(5)
    v[::7] = np.nan
    assert fit_baseline(ts, v) is not None


def test_roundtrip():
    ts, v = daily_pattern(5)
    m = fit_baseline(ts, v)
    assert BaselineModel.from_dict(m.to_dict()) == m
