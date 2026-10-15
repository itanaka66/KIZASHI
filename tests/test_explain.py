"""Tests for kizashi/explain.py (P4 SHAP feature contributions)."""

from __future__ import annotations

import numpy as np
import pytest

lgb = pytest.importorskip("lightgbm", reason="lightgbm not installed")
shap = pytest.importorskip("shap", reason="shap not installed")
sklearn = pytest.importorskip("sklearn", reason="scikit-learn not installed")

from kizashi.explain import (
    TopContributor,
    _extract_lgbm,
    compute_shap_values,
    top_contributors,
    top_contributors_batch,
    try_explain,
)
from kizashi.models.failure import FailureDataset, fit_failure_model

RNG = np.random.default_rng(42)
N_FEATURES = 5
FEATURE_NAMES = [f"feat_{i}" for i in range(N_FEATURES)]


def _make_pipeline():
    """Fit a small LightGBM pipeline on synthetic data."""
    n_pos, n_neg = 80, 800
    X_pos = RNG.normal(loc=4.0, scale=1.0, size=(n_pos, N_FEATURES))
    X_neg = RNG.normal(loc=0.0, scale=1.0, size=(n_neg, N_FEATURES))
    X = np.vstack([X_pos, X_neg])
    y = np.array([1] * n_pos + [0] * n_neg, dtype=int)
    ts = np.linspace(0, 1000, n_pos + n_neg)
    ds = FailureDataset(
        X=X, y=y, timestamps=ts,
        feature_names=FEATURE_NAMES,
        failure_mode="rx_low",
        horizon_s=3600.0,
    )
    _, pipeline = fit_failure_model(ds)
    return pipeline, X


# ─────────────────────────── _extract_lgbm ───────────────────────────────────


def test_extract_lgbm_from_raw():
    import lightgbm as lgb_mod
    clf = lgb_mod.LGBMClassifier(n_estimators=5, verbose=-1)
    X = RNG.normal(size=(50, 3))
    y = np.array([0] * 25 + [1] * 25)
    clf.fit(X, y)
    extracted = _extract_lgbm(clf)
    assert extracted is clf


def test_extract_lgbm_from_calibrated():
    pipeline, _ = _make_pipeline()
    if hasattr(pipeline, "calibrated_classifiers_"):
        extracted = _extract_lgbm(pipeline)
        assert hasattr(extracted, "predict")
    else:
        # pipeline is a raw LGBMClassifier when calibration set was insufficient
        assert hasattr(_extract_lgbm(pipeline), "predict")


# ─────────────────────────── compute_shap_values ─────────────────────────────


def test_shap_values_shape():
    pipeline, X = _make_pipeline()
    shap_vals = compute_shap_values(pipeline, X[:20])
    assert shap_vals.shape == (20, N_FEATURES)


def test_shap_values_dtype():
    pipeline, X = _make_pipeline()
    shap_vals = compute_shap_values(pipeline, X[:5])
    assert shap_vals.dtype == np.float64 or np.issubdtype(shap_vals.dtype, np.floating)


# ─────────────────────────── top_contributors ────────────────────────────────


def test_top_contributors_returns_correct_count():
    pipeline, X = _make_pipeline()
    contribs = top_contributors(pipeline, X[0], FEATURE_NAMES, top_k=3)
    assert len(contribs) == 3


def test_top_contributors_ranks_are_sequential():
    pipeline, X = _make_pipeline()
    contribs = top_contributors(pipeline, X[0], FEATURE_NAMES, top_k=3)
    assert [c.rank for c in contribs] == [1, 2, 3]


def test_top_contributors_feature_names_valid():
    pipeline, X = _make_pipeline()
    contribs = top_contributors(pipeline, X[0], FEATURE_NAMES, top_k=3)
    for c in contribs:
        assert c.feature in FEATURE_NAMES


def test_top_contributors_shap_values_are_floats():
    pipeline, X = _make_pipeline()
    contribs = top_contributors(pipeline, X[0], FEATURE_NAMES, top_k=3)
    for c in contribs:
        assert isinstance(c.shap_value, float)


def test_top_contributors_top1_has_largest_absolute_value():
    pipeline, X = _make_pipeline()
    contribs = top_contributors(pipeline, X[0], FEATURE_NAMES, top_k=3)
    abs_vals = [abs(c.shap_value) for c in contribs]
    assert abs_vals[0] >= abs_vals[1] >= abs_vals[2]


def test_top_contributors_with_top_k_1():
    pipeline, X = _make_pipeline()
    contribs = top_contributors(pipeline, X[0], FEATURE_NAMES, top_k=1)
    assert len(contribs) == 1
    assert contribs[0].rank == 1


# ─────────────────────────── top_contributors_batch ──────────────────────────


def test_batch_returns_list_per_row():
    pipeline, X = _make_pipeline()
    batch = top_contributors_batch(pipeline, X[:5], FEATURE_NAMES, top_k=3)
    assert len(batch) == 5
    for row in batch:
        assert len(row) == 3


def test_batch_matches_single_row():
    pipeline, X = _make_pipeline()
    single = top_contributors(pipeline, X[0], FEATURE_NAMES, top_k=3)
    batch = top_contributors_batch(pipeline, X[:1], FEATURE_NAMES, top_k=3)
    # Feature names and ordering should match
    assert [c.feature for c in single] == [c.feature for c in batch[0]]


# ─────────────────────────── try_explain ─────────────────────────────────────


def test_try_explain_returns_list_on_success():
    pipeline, X = _make_pipeline()
    result = try_explain(pipeline, X[0], FEATURE_NAMES, top_k=3)
    assert isinstance(result, list)
    assert len(result) <= 3


def test_try_explain_returns_empty_on_error():
    class BrokenPipeline:
        def predict_proba(self, X):
            raise RuntimeError("broken")

    result = try_explain(BrokenPipeline(), np.zeros(N_FEATURES), FEATURE_NAMES, top_k=3)
    assert result == []
