"""Third-layer multivariate anomaly detection using Isolation Forest.

Groups devices by (role × vendor) and fits one IsolationForest per group.
sklearn is an optional dependency; import errors surface only when the model is used.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Label keys used to form the group key
ROLE_LABEL = "device_role"    # "core" | "distribution" | "access" | "unknown"
VENDOR_LABEL = "vendor"       # "cisco-ios" | "cisco-nxos" | "juniper" | "arista"

MIN_GROUP_SAMPLES = 10

import math as _math


def _require_sklearn() -> Any:
    try:
        from sklearn.ensemble import IsolationForest  # noqa: PLC0415
        return IsolationForest
    except ImportError as exc:
        raise RuntimeError(
            "scikit-learn is required for multivariate anomaly detection. "
            "Install it with: pip install 'kizashi[multivariate]'"
        ) from exc


@dataclass
class MultivariateModel:
    group_key: str
    feature_names: list[str]
    version: str
    trained_at: float
    n_points: int
    contamination: float = 0.05

    def to_dict(self) -> dict:
        return {
            "group_key": self.group_key,
            "feature_names": self.feature_names,
            "version": self.version,
            "trained_at": self.trained_at,
            "n_points": self.n_points,
            "contamination": self.contamination,
        }

    @classmethod
    def from_dict(cls, d: dict) -> MultivariateModel:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def group_key_for(labels: dict[str, str]) -> str:
    """Return 'role.vendor' from device labels, e.g. 'core.cisco-ios'."""
    role = labels.get(ROLE_LABEL, "unknown")
    vendor = labels.get(VENDOR_LABEL, "unknown")
    return f"{role}.{vendor}"


def _df_to_0_10(df: float) -> float:
    """Map IsolationForest decision_function output to 0–10 anomaly score.

    ``decision_function > 0`` → inlier (score approaches 0).
    ``decision_function = 0`` → detection boundary (score ≈ 5).
    ``decision_function < 0`` → outlier (score approaches 10).

    A logistic function with steepness 5 gives a smooth S-curve.
    """
    score = 10.0 / (1.0 + _math.exp(5.0 * df))
    return round(max(0.0, min(10.0, score)), 2)


def fit_multivariate(
    X: np.ndarray,
    feature_names: list[str],
    group_key: str,
    contamination: float = 0.05,
) -> tuple[MultivariateModel, Any]:
    """Fit an Isolation Forest on a device feature matrix.

    Parameters
    ----------
    X : shape (n_devices, n_features)  – one row per device snapshot.
        NaN values are replaced with column medians before fitting.
    feature_names : column labels for X.
    group_key : string like 'core.cisco-ios', used for file naming.
    contamination : expected fraction of outliers (IsolationForest parameter).

    Returns
    -------
    (MultivariateModel metadata, fitted IsolationForest)
    """
    IsolationForest = _require_sklearn()

    if X.ndim != 2 or X.shape[1] != len(feature_names):
        raise ValueError(f"X must be (n_devices, {len(feature_names)}); got shape {X.shape}")
    if X.shape[0] < MIN_GROUP_SAMPLES:
        raise ValueError(
            f"group {group_key!r}: need >= {MIN_GROUP_SAMPLES} devices, got {X.shape[0]}"
        )

    # Impute NaN with column medians
    X = X.copy()
    col_medians = np.nanmedian(X, axis=0)
    for col in range(X.shape[1]):
        nan_rows = np.isnan(X[:, col])
        if nan_rows.any():
            X[nan_rows, col] = col_medians[col]

    clf = IsolationForest(contamination=contamination, random_state=42, n_estimators=100)
    clf.fit(X)

    meta = MultivariateModel(
        group_key=group_key,
        feature_names=list(feature_names),
        version=time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        trained_at=time.time(),
        n_points=int(X.shape[0]),
        contamination=contamination,
    )
    logger.info(
        "multivariate: fitted group=%r n_devices=%d features=%s",
        group_key,
        X.shape[0],
        feature_names,
    )
    return meta, clf


def score_multivariate(clf: Any, features: np.ndarray) -> float:
    """Score a single device feature vector. Returns 0–10 anomaly score.

    0 = normal, 5 = at the inlier/outlier boundary, 10 = maximally anomalous.
    Uses ``decision_function`` which is properly calibrated against the training
    contamination fraction, so scores are consistent across different data scales.
    """
    _require_sklearn()
    row = features.reshape(1, -1) if features.ndim == 1 else features
    df = float(clf.decision_function(row)[0])
    return _df_to_0_10(df)


def build_feature_matrix(
    device_series: dict[str, dict[str, float]],
    feature_names: list[str],
) -> tuple[np.ndarray, list[str]]:
    """Convert {device_key: {feature: value}} to (X, device_keys).

    Missing feature values become NaN (imputed before fitting).
    """
    device_keys = sorted(device_series)
    X = np.full((len(device_keys), len(feature_names)), np.nan)
    for i, dk in enumerate(device_keys):
        for j, fn in enumerate(feature_names):
            v = device_series[dk].get(fn)
            if v is not None:
                X[i, j] = float(v)
    return X, device_keys
