"""Tests for kizashi/models/failure.py (P4 LightGBM failure prediction)."""

from __future__ import annotations

import numpy as np
import pytest

lgb = pytest.importorskip("lightgbm", reason="lightgbm not installed")
sklearn = pytest.importorskip("sklearn", reason="scikit-learn not installed")

from kizashi.models.failure import (
    HORIZONS,
    MIN_POSITIVES,
    PSI_RETRAIN,
    PSI_WARN,
    FailureDataset,
    FailureModelMeta,
    WalkForwardResult,
    _compute_percentiles,
    compute_feature_psi,
    compute_psi,
    downsample_negatives,
    fit_failure_model,
    horizon_label,
    max_feature_psi,
    psi_from_percentiles,
    score_batch,
    score_failure,
    walk_forward_eval,
)

RNG = np.random.default_rng(0)


# ────────────────────────────── dataset factory ──────────────────────────────


def _make_dataset(n_pos: int = 60, n_neg: int = 600, n_features: int = 4) -> FailureDataset:
    """Create a synthetic dataset with a linearly separable positive cluster."""
    X_pos = RNG.normal(loc=5.0, scale=1.0, size=(n_pos, n_features))
    X_neg = RNG.normal(loc=0.0, scale=1.0, size=(n_neg, n_features))
    X = np.vstack([X_pos, X_neg])
    y = np.array([1] * n_pos + [0] * n_neg, dtype=int)
    ts = np.linspace(0, 1000, n_pos + n_neg)
    return FailureDataset(
        X=X, y=y, timestamps=ts,
        feature_names=[f"feat_{i}" for i in range(n_features)],
        failure_mode="optic_degradation",
        horizon_s=3600.0,
    )


# ─────────────────────────────── horizon_label ───────────────────────────────


def test_horizon_label_canonical():
    assert horizon_label(3600.0) == "1h"
    assert horizon_label(86400.0) == "24h"
    assert horizon_label(604800.0) == "7d"


def test_horizon_label_unknown():
    label = horizon_label(7200.0)
    assert "2" in label or label  # just not empty


# ─────────────────────────── downsample_negatives ────────────────────────────


def test_downsample_keeps_all_positives():
    rng = np.random.default_rng(1)
    X = np.zeros((200, 3))
    y = np.array([1] * 20 + [0] * 180, dtype=int)
    ts = np.arange(200, dtype=float)
    X_ds, y_ds, _ = downsample_negatives(X, y, ts, neg_ratio=5.0, rng=rng)
    assert y_ds.sum() == 20


def test_downsample_limits_negatives():
    rng = np.random.default_rng(2)
    X = np.zeros((200, 3))
    y = np.array([1] * 20 + [0] * 180, dtype=int)
    ts = np.arange(200, dtype=float)
    X_ds, y_ds, _ = downsample_negatives(X, y, ts, neg_ratio=5.0, rng=rng)
    assert (y_ds == 0).sum() == 100


def test_downsample_does_not_exceed_available_negatives():
    rng = np.random.default_rng(3)
    X = np.zeros((25, 2))
    y = np.array([1] * 20 + [0] * 5, dtype=int)
    ts = np.arange(25, dtype=float)
    _, y_ds, _ = downsample_negatives(X, y, ts, neg_ratio=10.0, rng=rng)
    assert (y_ds == 0).sum() == 5


# ────────────────────────────── fit_failure_model ────────────────────────────


def test_fit_returns_meta_and_pipeline():
    ds = _make_dataset()
    meta, pipeline = fit_failure_model(ds)
    assert isinstance(meta, FailureModelMeta)
    assert hasattr(pipeline, "predict_proba")


def test_fit_meta_fields_populated():
    ds = _make_dataset()
    meta, _ = fit_failure_model(ds)
    assert meta.failure_mode == "optic_degradation"
    assert meta.horizon_label == "1h"
    assert meta.n_pos >= MIN_POSITIVES
    assert len(meta.feature_percentiles) == 4


def test_fit_raises_when_too_few_positives():
    ds = _make_dataset(n_pos=5, n_neg=50)
    with pytest.raises((ValueError, RuntimeError)):
        fit_failure_model(ds)


def test_fit_precision_recall_non_negative():
    ds = _make_dataset()
    meta, _ = fit_failure_model(ds)
    assert 0.0 <= meta.precision_at_05 <= 1.0
    assert 0.0 <= meta.recall <= 1.0


def test_score_failure_in_unit_interval():
    ds = _make_dataset()
    _, pipeline = fit_failure_model(ds)
    prob = score_failure(pipeline, ds.X[0])
    assert 0.0 <= prob <= 1.0


def test_score_batch_shape():
    ds = _make_dataset()
    _, pipeline = fit_failure_model(ds)
    probs = score_batch(pipeline, ds.X[:10])
    assert probs.shape == (10,)
    assert np.all((probs >= 0) & (probs <= 1))


# ─────────────────────────── FailureModelMeta serialization ──────────────────


def test_meta_round_trip():
    ds = _make_dataset()
    meta, _ = fit_failure_model(ds)
    d = meta.to_dict()
    meta2 = FailureModelMeta.from_dict(d)
    assert meta2.failure_mode == meta.failure_mode
    assert meta2.horizon_label == meta.horizon_label
    assert meta2.feature_percentiles == meta.feature_percentiles


# ─────────────────────────── walk_forward_eval ───────────────────────────────


def test_walk_forward_returns_result():
    ds = _make_dataset(n_pos=100, n_neg=1000)
    result = walk_forward_eval(ds, n_folds=3)
    assert isinstance(result, WalkForwardResult)
    assert result.folds >= 0


def test_walk_forward_too_few_positives_returns_empty():
    ds = _make_dataset(n_pos=5, n_neg=50)
    result = walk_forward_eval(ds, n_folds=3)
    assert result.folds == 0
    assert not result.meets_criteria


def test_walk_forward_lead_time_matches_horizon():
    ds = _make_dataset(n_pos=100, n_neg=1000)
    result = walk_forward_eval(ds, n_folds=3)
    if result.folds > 0 and result.lead_time_median_h > 0:
        assert abs(result.lead_time_median_h - ds.horizon_s / 3600.0) < 0.01


# ──────────────────────────────── PSI functions ──────────────────────────────


def test_compute_psi_identical_distributions():
    ref = RNG.normal(size=1000)
    psi = compute_psi(ref, ref.copy())
    assert psi < PSI_WARN


def test_compute_psi_shifted_distribution():
    ref = RNG.normal(loc=0.0, size=1000)
    cur = RNG.normal(loc=5.0, size=1000)  # very different
    psi = compute_psi(ref, cur)
    assert psi >= PSI_WARN


def test_max_feature_psi_identical():
    X = RNG.normal(size=(500, 4))
    psi = max_feature_psi(X, X.copy())
    assert psi < PSI_WARN


def test_psi_from_percentiles_stable():
    X = RNG.normal(size=(500, 4))
    percs = _compute_percentiles(X)
    psi = psi_from_percentiles(percs, X.copy())
    assert psi < PSI_RETRAIN


def test_compute_feature_psi_returns_per_feature():
    X = RNG.normal(size=(500, 3))
    Y = RNG.normal(size=(500, 3))
    result = compute_feature_psi(X, Y)
    assert len(result) == 3
