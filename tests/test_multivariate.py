"""Tests for kizashi/models/multivariate.py and ModelStore multivariate I/O."""

from __future__ import annotations

import numpy as np
import pytest

sklearn = pytest.importorskip("sklearn", reason="scikit-learn not installed")

from kizashi.models.multivariate import (
    MIN_GROUP_SAMPLES,
    MultivariateModel,
    _df_to_0_10,
    build_feature_matrix,
    fit_multivariate,
    group_key_for,
    score_multivariate,
)


# ─────────────────────────────── helpers ───────────────────────────────────

def _normal_data(rng: np.random.Generator, n: int = 40, n_features: int = 3) -> np.ndarray:
    """Generate a cluster of 'normal' device snapshots."""
    return rng.normal(loc=0.5, scale=0.05, size=(n, n_features)).clip(0, 1)


FEATURES = ["cpu_util", "memory_util", "if_error_total"]


# ─────────────────────────────── unit tests ────────────────────────────────

class TestGroupKey:
    def test_known_labels(self):
        labels = {"device_role": "core", "vendor": "cisco-ios", "instance": "sw1"}
        assert group_key_for(labels) == "core.cisco-ios"

    def test_missing_labels_give_unknown(self):
        assert group_key_for({}) == "unknown.unknown"

    def test_partial_labels(self):
        assert group_key_for({"vendor": "juniper"}) == "unknown.juniper"


class TestScoreConversion:
    def test_boundary_is_five(self):
        # decision_function=0 means exactly at the inlier/outlier boundary
        score = _df_to_0_10(0.0)
        assert abs(score - 5.0) < 0.1

    def test_clear_inlier_near_zero(self):
        # Large positive df → clearly inlier → score approaches 0
        assert _df_to_0_10(2.0) < 0.5

    def test_clear_outlier_near_ten(self):
        # Large negative df → clearly outlier → score approaches 10
        assert _df_to_0_10(-2.0) > 9.5

    def test_monotone_decreasing(self):
        # More negative df → higher anomaly score
        scores = [_df_to_0_10(df) for df in [1.0, 0.5, 0.0, -0.5, -1.0]]
        assert scores == sorted(scores)

    def test_clamped_to_0_10(self):
        assert 0.0 <= _df_to_0_10(10.0) <= 10.0
        assert 0.0 <= _df_to_0_10(-10.0) <= 10.0


class TestFitMultivariate:
    def test_returns_model_and_clf(self):
        rng = np.random.default_rng(0)
        X = _normal_data(rng)
        meta, clf = fit_multivariate(X, FEATURES, "core.cisco-ios")
        assert isinstance(meta, MultivariateModel)
        assert meta.group_key == "core.cisco-ios"
        assert meta.feature_names == FEATURES
        assert meta.n_points == X.shape[0]
        assert clf is not None

    def test_too_few_samples_raises(self):
        rng = np.random.default_rng(0)
        X = _normal_data(rng, n=MIN_GROUP_SAMPLES - 1)
        with pytest.raises(ValueError, match="need >= "):
            fit_multivariate(X, FEATURES, "access.arista")

    def test_wrong_feature_count_raises(self):
        rng = np.random.default_rng(0)
        X = _normal_data(rng, n_features=2)
        with pytest.raises(ValueError, match="shape"):
            fit_multivariate(X, FEATURES, "core.cisco-ios")

    def test_nan_imputed_before_fit(self):
        rng = np.random.default_rng(1)
        X = _normal_data(rng)
        X[0, 0] = np.nan  # inject NaN
        # Should not raise
        meta, clf = fit_multivariate(X, FEATURES, "core.cisco-ios")
        assert meta.n_points == X.shape[0]

    def test_normal_points_score_low(self):
        rng = np.random.default_rng(2)
        X = _normal_data(rng, n=100)
        meta, clf = fit_multivariate(X, FEATURES, "core.cisco-ios")
        # Center of training distribution → inlier → decision_function > 0 → score < 5
        normal_point = np.array([0.5, 0.5, 0.5])
        score = score_multivariate(clf, normal_point)
        assert score < 5.0, f"expected inlier score < 5 for center point, got {score}"

    def test_outlier_scores_high(self):
        rng = np.random.default_rng(3)
        X = _normal_data(rng, n=100)
        meta, clf = fit_multivariate(X, FEATURES, "core.cisco-ios")
        # Far from training cluster → outlier → decision_function < 0 → score > 5
        outlier = np.array([0.99, 0.01, 0.99])
        score = score_multivariate(clf, outlier)
        assert score > 5.0, f"expected outlier score > 5, got {score}"


class TestScoreMultivariate:
    def test_1d_input_accepted(self):
        rng = np.random.default_rng(4)
        X = _normal_data(rng)
        _, clf = fit_multivariate(X, FEATURES, "core.cisco-ios")
        point = np.array([0.5, 0.5, 0.5])
        score = score_multivariate(clf, point)
        assert 0.0 <= score <= 10.0

    def test_2d_input_accepted(self):
        rng = np.random.default_rng(5)
        X = _normal_data(rng)
        _, clf = fit_multivariate(X, FEATURES, "core.cisco-ios")
        point = np.array([[0.5, 0.5, 0.5]])
        score = score_multivariate(clf, point)
        assert 0.0 <= score <= 10.0


class TestBuildFeatureMatrix:
    def test_basic_assembly(self):
        devices = {
            "sw1": {"cpu_util": 0.3, "memory_util": 0.5},
            "sw2": {"cpu_util": 0.8, "memory_util": 0.2},
        }
        X, keys = build_feature_matrix(devices, ["cpu_util", "memory_util"])
        assert X.shape == (2, 2)
        assert set(keys) == {"sw1", "sw2"}

    def test_missing_feature_becomes_nan(self):
        devices = {"sw1": {"cpu_util": 0.3}}  # memory_util missing
        X, keys = build_feature_matrix(devices, ["cpu_util", "memory_util"])
        assert np.isnan(X[0, 1])

    def test_keys_sorted(self):
        devices = {"zz": {"cpu_util": 0.1}, "aa": {"cpu_util": 0.9}}
        _, keys = build_feature_matrix(devices, ["cpu_util"])
        assert keys == sorted(keys)


class TestMultivariateModelSerialization:
    def test_roundtrip(self):
        meta = MultivariateModel(
            group_key="core.cisco-ios",
            feature_names=["cpu", "mem"],
            version="20260101T000000Z",
            trained_at=1_700_000_000.0,
            n_points=42,
            contamination=0.05,
        )
        assert MultivariateModel.from_dict(meta.to_dict()) == meta


class TestModelStoreMultivariate:
    def test_save_and_load(self, tmp_path):
        from kizashi.models.store import ModelStore

        rng = np.random.default_rng(6)
        X = _normal_data(rng)
        meta, clf = fit_multivariate(X, FEATURES, "core.cisco-ios")
        store = ModelStore(tmp_path)
        store.save_multivariate("core.cisco-ios", meta, clf)

        loaded_meta, loaded_clf = store.load_multivariate("core.cisco-ios")
        assert loaded_meta.group_key == "core.cisco-ios"
        assert loaded_meta.feature_names == FEATURES

        # Loaded classifier should produce same scores
        point = np.array([0.5, 0.5, 0.5])
        assert score_multivariate(clf, point) == score_multivariate(loaded_clf, point)

    def test_load_missing_returns_none(self, tmp_path):
        from kizashi.models.store import ModelStore

        store = ModelStore(tmp_path)
        assert store.load_multivariate("nonexistent.group") is None
