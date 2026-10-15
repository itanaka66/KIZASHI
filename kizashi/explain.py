"""SHAP-based feature contribution explanations for Layer 4 failure models.

Provides the top-K contributing features per prediction, published as
``kizashi_top_contributor{rank, feature, failure_mode, instance, ifName}``.

SHAP is an optional dependency; if not installed these functions raise
``RuntimeError`` with installation instructions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


def _require_shap():
    try:
        import shap  # noqa: PLC0415
        return shap
    except ImportError as exc:
        raise RuntimeError(
            "shap is required for feature contribution explanations. "
            "Install it with: pip install 'kizashi[p4]'"
        ) from exc


@dataclass
class TopContributor:
    rank: int           # 1 = most important
    feature: str
    shap_value: float   # positive = pushes probability up, negative = down


def _extract_lgbm(pipeline: Any) -> Any:
    """Return the underlying LGBMClassifier from a possibly-calibrated wrapper."""
    # CalibratedClassifierCV stores base models in calibrated_classifiers_[i].estimator
    if hasattr(pipeline, "calibrated_classifiers_"):
        return pipeline.calibrated_classifiers_[0].estimator
    return pipeline


def compute_shap_values(
    pipeline: Any,
    X: np.ndarray,
) -> np.ndarray:
    """Compute SHAP values for a batch of samples using TreeExplainer.

    Parameters
    ----------
    pipeline : calibrated or raw LGBMClassifier pipeline.
    X : (n_samples, n_features) feature matrix.

    Returns
    -------
    shap_values : (n_samples, n_features) SHAP values for the positive class.
        Positive values increase the predicted failure probability.
    """
    shap = _require_shap()
    lgbm_clf = _extract_lgbm(pipeline)
    explainer = shap.TreeExplainer(lgbm_clf)
    values = explainer.shap_values(X if X.ndim == 2 else X.reshape(1, -1))
    # LightGBM binary: shap_values is a list [neg_class, pos_class] or a 2D array
    if isinstance(values, list):
        return np.array(values[1])
    return np.array(values)


def top_contributors(
    pipeline: Any,
    features: np.ndarray,
    feature_names: list[str],
    top_k: int = 3,
) -> list[TopContributor]:
    """Explain a single prediction: return the top-K most influential features.

    Ranking is by absolute SHAP value (largest impact first).

    Parameters
    ----------
    pipeline : calibrated or raw LGBMClassifier.
    features : 1-D feature vector (n_features,) for the device to explain.
    feature_names : list of feature names aligned with ``features``.
    top_k : number of contributors to return.
    """
    row = features.reshape(1, -1) if features.ndim == 1 else features
    shap_vals = compute_shap_values(pipeline, row)[0]  # shape: (n_features,)
    abs_vals = np.abs(shap_vals)
    ranked_idx = np.argsort(abs_vals)[::-1][:top_k]
    return [
        TopContributor(
            rank=rank + 1,
            feature=feature_names[i],
            shap_value=float(shap_vals[i]),
        )
        for rank, i in enumerate(ranked_idx)
    ]


def top_contributors_batch(
    pipeline: Any,
    X: np.ndarray,
    feature_names: list[str],
    top_k: int = 3,
) -> list[list[TopContributor]]:
    """Explain a batch of predictions. Returns one list of TopContributors per row."""
    shap_vals = compute_shap_values(pipeline, X)  # (n_samples, n_features)
    result = []
    for i in range(len(X)):
        row_shap = shap_vals[i]
        abs_vals = np.abs(row_shap)
        ranked_idx = np.argsort(abs_vals)[::-1][:top_k]
        result.append([
            TopContributor(
                rank=rank + 1,
                feature=feature_names[j],
                shap_value=float(row_shap[j]),
            )
            for rank, j in enumerate(ranked_idx)
        ])
    return result


def try_explain(
    pipeline: Any,
    features: np.ndarray,
    feature_names: list[str],
    top_k: int = 3,
) -> list[TopContributor]:
    """Return top contributors, or an empty list on any error (SHAP is optional)."""
    try:
        return top_contributors(pipeline, features, feature_names, top_k)
    except Exception as exc:
        logger.debug("SHAP explanation failed: %s", exc)
        return []
