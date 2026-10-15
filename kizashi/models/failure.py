"""Fourth-layer failure probability prediction using LightGBM.

One binary classifier per (failure_mode × horizon). Features come from
layers 1–3 plus multi-window statistics. Calibration uses Isotonic Regression.

LightGBM and scikit-learn are optional dependencies; ImportErrors surface only
when these functions are actually called.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Canonical prediction horizons
HORIZONS: dict[str, float] = {
    "1h": 3600.0,
    "24h": 86400.0,
    "7d": 604800.0,
}

# Minimum positive samples required to train a model
MIN_POSITIVES = 30

# PSI thresholds for drift detection
PSI_WARN = 0.1
PSI_RETRAIN = 0.2


# ───────────────────────────── optional imports ───────────────────────────────


def _require_lgbm():
    try:
        import lightgbm as lgb  # noqa: PLC0415
        return lgb
    except ImportError as exc:
        raise RuntimeError(
            "lightgbm is required for failure probability prediction. "
            "Install it with: pip install 'kizashi[p4]'"
        ) from exc


def _require_sklearn():
    try:
        from sklearn.calibration import CalibratedClassifierCV  # noqa: PLC0415
        from sklearn.metrics import precision_score, recall_score  # noqa: PLC0415
        return CalibratedClassifierCV, precision_score, recall_score
    except ImportError as exc:
        raise RuntimeError(
            "scikit-learn is required for failure probability calibration. "
            "Install it with: pip install 'kizashi[p4]'"
        ) from exc


# ─────────────────────────────── data structures ──────────────────────────────


@dataclass
class FailureDataset:
    """Training data for one (failure_mode × horizon) model."""

    X: np.ndarray               # (n_samples, n_features) float64
    y: np.ndarray               # (n_samples,) int: 1 = failure, 0 = normal
    timestamps: np.ndarray      # (n_samples,) unix seconds of each observation
    feature_names: list[str]
    failure_mode: str
    horizon_s: float            # prediction horizon in seconds


@dataclass
class WalkForwardResult:
    folds: int
    precision_scores: list[float]
    recall_scores: list[float]
    mean_precision: float
    mean_recall: float
    lead_time_median_h: float
    meets_criteria: bool        # precision ≥ 0.5 AND recall ≥ 0.6 AND lead_time ≥ 1h


@dataclass
class FailureModelMeta:
    failure_mode: str
    horizon_label: str          # "1h" | "24h" | "7d"
    horizon_s: float
    feature_names: list[str]
    version: str
    trained_at: float
    n_pos: int
    n_neg_sampled: int
    n_neg_total: int
    precision_at_05: float      # precision at 0.5 probability threshold on cal set
    recall: float               # recall at 0.5 threshold on cal set
    lead_time_median_h: float
    is_shadow: bool = False     # True → model runs silently; alerts suppressed
    psi: float = 0.0            # latest PSI vs training feature distribution
    # Per-feature percentile breakpoints stored for future PSI computation.
    # Shape: [n_features][n_percentile_points]; percentile points are [0,10,20,...,100].
    feature_percentiles: list[list[float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "failure_mode": self.failure_mode,
            "horizon_label": self.horizon_label,
            "horizon_s": self.horizon_s,
            "feature_names": self.feature_names,
            "version": self.version,
            "trained_at": self.trained_at,
            "n_pos": self.n_pos,
            "n_neg_sampled": self.n_neg_sampled,
            "n_neg_total": self.n_neg_total,
            "precision_at_05": self.precision_at_05,
            "recall": self.recall,
            "lead_time_median_h": self.lead_time_median_h,
            "is_shadow": self.is_shadow,
            "psi": self.psi,
            "feature_percentiles": self.feature_percentiles,
        }

    @classmethod
    def from_dict(cls, d: dict) -> FailureModelMeta:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


# ───────────────────────────── helper functions ───────────────────────────────


def horizon_label(seconds: float) -> str:
    """Return the canonical label for a horizon duration."""
    for label, s in HORIZONS.items():
        if abs(s - seconds) < 60:
            return label
    h = seconds / 3600
    return f"{int(h)}h" if h == int(h) else f"{seconds:.0f}s"


def _compute_percentiles(X: np.ndarray) -> list[list[float]]:
    """Compute [p0, p10, …, p100] breakpoints per feature for PSI storage."""
    percentile_points = list(range(0, 101, 10))
    result = []
    for j in range(X.shape[1]):
        col = X[:, j]
        col = col[~np.isnan(col)]
        if len(col) == 0:
            result.append([0.0] * len(percentile_points))
        else:
            result.append([float(np.percentile(col, p)) for p in percentile_points])
    return result


# ─────────────────────────────── core functions ───────────────────────────────


def downsample_negatives(
    X: np.ndarray,
    y: np.ndarray,
    timestamps: np.ndarray,
    neg_ratio: float = 10.0,
    rng: np.random.Generator | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Downsample negatives to ``neg_ratio`` × positives.

    All positive samples are kept. When there are fewer negatives than the
    target, all negatives are kept (ratio is treated as an upper bound).
    """
    if rng is None:
        rng = np.random.default_rng(42)
    pos_idx = np.where(y == 1)[0]
    neg_idx = np.where(y == 0)[0]
    n_neg_keep = min(len(neg_idx), max(1, int(len(pos_idx) * neg_ratio)))
    chosen_neg = rng.choice(neg_idx, size=n_neg_keep, replace=False)
    idx = np.sort(np.concatenate([pos_idx, chosen_neg]))
    return X[idx], y[idx], timestamps[idx]


def fit_failure_model(
    dataset: FailureDataset,
    neg_ratio: float = 10.0,
    cal_fraction: float = 0.2,
) -> tuple[FailureModelMeta, Any]:
    """Fit and calibrate a LightGBM binary classifier.

    Training procedure
    ------------------
    1. Sort samples by timestamp (walk-forward safety).
    2. Time-split: first ``(1 - cal_fraction)`` for LightGBM, last ``cal_fraction``
       for Isotonic Regression calibration.
    3. Downsample negatives in the training set (``neg_ratio : 1``).
    4. Fit ``LGBMClassifier`` with ``scale_pos_weight``.
    5. Fit ``CalibratedClassifierCV(cv='prefit', method='isotonic')`` on the
       calibration set.

    Returns
    -------
    (FailureModelMeta, calibrated pipeline)
    The pipeline supports ``predict_proba(X)[:, 1]``.
    """
    lgb = _require_lgbm()
    CalibratedClassifierCV, precision_score, recall_score = _require_sklearn()

    # Sort by timestamp
    order = np.argsort(dataset.timestamps)
    X = dataset.X[order].astype(np.float64)
    y = dataset.y[order].astype(int)
    ts = dataset.timestamps[order]

    n = len(y)
    n_pos = int(y.sum())
    if n_pos < MIN_POSITIVES:
        raise ValueError(
            f"failure_mode={dataset.failure_mode!r}: need >= {MIN_POSITIVES} positives, got {n_pos}"
        )

    # Time-based split
    split = max(1, int(n * (1.0 - cal_fraction)))
    X_train, y_train, ts_train = X[:split], y[:split], ts[:split]
    X_cal, y_cal = X[split:], y[split:]

    # Downsample negatives in training portion
    X_ds, y_ds, _ = downsample_negatives(X_train, y_train, ts_train, neg_ratio)
    n_pos_ds = int(y_ds.sum())
    n_neg_ds = int((y_ds == 0).sum())
    spw = float(n_neg_ds) / max(1, n_pos_ds)

    # Fit LightGBM
    clf = lgb.LGBMClassifier(
        n_estimators=200,
        num_leaves=31,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_samples=5,
        scale_pos_weight=spw,
        random_state=42,
        verbose=-1,
    )
    clf.fit(X_ds, y_ds)

    # Isotonic calibration when the calibration set has both classes
    if len(y_cal) > 0 and y_cal.sum() > 0 and (y_cal == 0).sum() > 0:
        calibrated = CalibratedClassifierCV(clf, cv="prefit", method="isotonic")
        calibrated.fit(X_cal, y_cal)
        pipeline = calibrated
    else:
        pipeline = clf

    # Evaluate on calibration set (or full set if calibration was skipped)
    eval_X = X_cal if len(X_cal) > 0 else X
    eval_y = y_cal if len(y_cal) > 0 else y
    proba_cal = score_batch(pipeline, eval_X)
    y_pred_cal = (proba_cal >= 0.5).astype(int)
    if eval_y.sum() > 0:
        prec = float(precision_score(eval_y, y_pred_cal, zero_division=0))
        rec = float(recall_score(eval_y, y_pred_cal, zero_division=0))
    else:
        prec = rec = 0.0

    hl = horizon_label(dataset.horizon_s)
    meta = FailureModelMeta(
        failure_mode=dataset.failure_mode,
        horizon_label=hl,
        horizon_s=dataset.horizon_s,
        feature_names=list(dataset.feature_names),
        version=time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        trained_at=time.time(),
        n_pos=n_pos,
        n_neg_sampled=n_neg_ds,
        n_neg_total=int((y == 0).sum()),
        precision_at_05=prec,
        recall=rec,
        lead_time_median_h=dataset.horizon_s / 3600.0,
        is_shadow=False,
        feature_percentiles=_compute_percentiles(X),
    )
    logger.info(
        "failure: fitted mode=%s horizon=%s n_pos=%d n_neg=%d prec=%.2f rec=%.2f",
        dataset.failure_mode,
        hl,
        n_pos,
        n_neg_ds,
        prec,
        rec,
    )
    return meta, pipeline


def score_failure(pipeline: Any, features: np.ndarray) -> float:
    """Return calibrated failure probability [0, 1] for a single feature vector."""
    row = features.reshape(1, -1) if features.ndim == 1 else features
    proba = pipeline.predict_proba(row)
    return float(proba[0, 1])


def score_batch(pipeline: Any, X: np.ndarray) -> np.ndarray:
    """Return calibrated failure probabilities for a batch. Shape: (n,)."""
    return pipeline.predict_proba(X)[:, 1]


# ─────────────────────────── walk-forward evaluation ─────────────────────────


def walk_forward_eval(
    dataset: FailureDataset,
    n_folds: int = 5,
    neg_ratio: float = 10.0,
) -> WalkForwardResult:
    """Walk-forward (time-ordered) cross-validation.

    Folds are time-ordered: fold i trains on rows 0..i×N/k, tests on the next
    block. A fold is skipped when its training set has fewer than MIN_POSITIVES
    or the test set has no positives.

    Evaluation criteria
    --------------------
    - Mean precision at threshold 0.5 ≥ 0.5
    - Mean recall at threshold 0.5 ≥ 0.6
    - Median lead time ≥ 1h (by construction: lead_time = horizon_s / 3600)
    """
    _, precision_score, recall_score = _require_sklearn()

    order = np.argsort(dataset.timestamps)
    X = dataset.X[order].astype(np.float64)
    y = dataset.y[order].astype(int)
    ts = dataset.timestamps[order]
    n = len(y)
    fold_size = max(1, n // n_folds)

    precision_list: list[float] = []
    recall_list: list[float] = []
    lead_times: list[float] = []

    for fold_idx in range(1, n_folds):
        split = fold_idx * fold_size
        if split >= n:
            break
        X_tr, y_tr, ts_tr = X[:split], y[:split], ts[:split]
        X_te, y_te = X[split:], y[split:]
        if int(y_tr.sum()) < MIN_POSITIVES or y_te.sum() == 0:
            continue
        sub = FailureDataset(X_tr, y_tr, ts_tr, dataset.feature_names, dataset.failure_mode, dataset.horizon_s)
        try:
            _, pipe = fit_failure_model(sub, neg_ratio=neg_ratio)
        except (ValueError, RuntimeError):
            continue
        proba = score_batch(pipe, X_te)
        y_pred = (proba >= 0.5).astype(int)
        precision_list.append(float(precision_score(y_te, y_pred, zero_division=0)))
        recall_list.append(float(recall_score(y_te, y_pred, zero_division=0)))
        n_tp = int(((y_pred == 1) & (y_te == 1)).sum())
        if n_tp > 0:
            lead_times.extend([dataset.horizon_s / 3600.0] * n_tp)

    if not precision_list:
        return WalkForwardResult(
            folds=0, precision_scores=[], recall_scores=[],
            mean_precision=0.0, mean_recall=0.0,
            lead_time_median_h=0.0, meets_criteria=False,
        )

    mean_prec = float(np.mean(precision_list))
    mean_rec = float(np.mean(recall_list))
    med_lead = float(np.median(lead_times)) if lead_times else 0.0
    meets = mean_prec >= 0.5 and mean_rec >= 0.6 and med_lead >= 1.0
    return WalkForwardResult(
        folds=len(precision_list),
        precision_scores=precision_list,
        recall_scores=recall_list,
        mean_precision=mean_prec,
        mean_recall=mean_rec,
        lead_time_median_h=med_lead,
        meets_criteria=meets,
    )


# ─────────────────────────── PSI drift detection ─────────────────────────────


def compute_psi(
    reference: np.ndarray,
    current: np.ndarray,
    n_bins: int = 10,
) -> float:
    """Population Stability Index for a single feature.

    PSI < 0.1    : no significant distribution shift
    PSI ∈ [0.1, 0.2) : moderate shift (warning)
    PSI ≥ 0.2    : significant shift → trigger retraining

    Bin boundaries are the quantiles of ``reference``.
    """
    eps = 1e-8
    quantiles = np.linspace(0, 100, n_bins + 1)
    bins = np.nanpercentile(reference, quantiles)
    bins[0] = -np.inf
    bins[-1] = np.inf
    ref_counts = np.histogram(reference[~np.isnan(reference)], bins=bins)[0]
    cur_counts = np.histogram(current[~np.isnan(current)], bins=bins)[0]
    ref_frac = ref_counts / max(len(reference), 1)
    cur_frac = cur_counts / max(len(current), 1)
    ref_frac = np.clip(ref_frac, eps, None)
    cur_frac = np.clip(cur_frac, eps, None)
    return float(np.sum((cur_frac - ref_frac) * np.log(cur_frac / ref_frac)))


def compute_feature_psi(
    reference_X: np.ndarray,
    current_X: np.ndarray,
    n_bins: int = 10,
) -> dict[int, float]:
    """PSI per feature column. Returns {column_index: psi}."""
    return {
        j: compute_psi(reference_X[:, j], current_X[:, j], n_bins)
        for j in range(reference_X.shape[1])
    }


def max_feature_psi(
    reference_X: np.ndarray,
    current_X: np.ndarray,
    n_bins: int = 10,
) -> float:
    """Maximum PSI across all features. Use this to decide whether to retrain."""
    psis = compute_feature_psi(reference_X, current_X, n_bins)
    return max(psis.values()) if psis else 0.0


def psi_from_percentiles(
    stored_percentiles: list[list[float]],
    current_X: np.ndarray,
    n_bins: int = 10,
) -> float:
    """Compute max PSI using stored per-feature percentile breakpoints.

    Avoids storing the full training set; stored_percentiles comes from
    ``FailureModelMeta.feature_percentiles``.
    """
    if not stored_percentiles or current_X.shape[1] != len(stored_percentiles):
        return 0.0
    eps = 1e-8
    max_psi = 0.0
    percentile_points = np.linspace(0, 100, len(stored_percentiles[0]))
    for j, percs in enumerate(stored_percentiles):
        if j >= current_X.shape[1]:
            break
        # Reconstruct reference distribution from stored percentiles
        # (use percentile values as approximate reference samples)
        ref_approx = np.array(percs, dtype=float)
        cur_col = current_X[:, j]
        psi = compute_psi(ref_approx, cur_col[~np.isnan(cur_col)], n_bins=max(2, len(percs) - 1))
        max_psi = max(max_psi, psi)
    return max_psi


# ────────────────────────── dataset construction ─────────────────────────────


def build_training_dataset(
    positive_keys: list[tuple[str, str, float]],  # (instance, if_name, failure_ts)
    feature_snapshots: dict[str, dict[str, float]],  # {series_key: {feature: value}}
    negative_keys: list[str],                        # series keys for normal samples
    feature_names: list[str],
    failure_mode: str,
    horizon_s: float,
    negative_timestamps: dict[str, float] | None = None,
    positive_timestamps: dict[str, float] | None = None,
) -> FailureDataset:
    """Assemble a FailureDataset from pre-queried feature snapshots.

    Parameters
    ----------
    positive_keys : (instance, if_name, failure_ts) tuples for known failures.
    feature_snapshots : {series_key: {feature_name: value}} for all observations.
        Series keys for positives should be ``"{instance}:{if_name}"`` at time
        ``failure_ts - horizon_s``; negatives at their respective observation times.
    negative_keys : series keys for normal (negative) observations.
    feature_names : ordered list of feature names (columns of X).
    failure_mode : string identifier for the failure type.
    horizon_s : prediction horizon in seconds.
    negative_timestamps, positive_timestamps : observation unix timestamps per key.
    """
    rows_X: list[np.ndarray] = []
    rows_y: list[int] = []
    rows_ts: list[float] = []
    now = time.time()

    for inst, if_name, failure_ts in positive_keys:
        key = f"{inst}:{if_name}"
        snap = feature_snapshots.get(key)
        if snap is None:
            continue
        row = np.array([snap.get(f, np.nan) for f in feature_names], dtype=np.float64)
        rows_X.append(row)
        rows_y.append(1)
        ts = (positive_timestamps or {}).get(key, failure_ts - horizon_s)
        rows_ts.append(ts)

    for key in negative_keys:
        snap = feature_snapshots.get(key)
        if snap is None:
            continue
        row = np.array([snap.get(f, np.nan) for f in feature_names], dtype=np.float64)
        rows_X.append(row)
        rows_y.append(0)
        ts = (negative_timestamps or {}).get(key, now)
        rows_ts.append(ts)

    if not rows_X:
        X = np.empty((0, len(feature_names)), dtype=np.float64)
        y = np.empty(0, dtype=int)
        ts_arr = np.empty(0, dtype=float)
    else:
        X = np.vstack(rows_X)
        y = np.array(rows_y, dtype=int)
        ts_arr = np.array(rows_ts, dtype=float)

    return FailureDataset(
        X=X,
        y=y,
        timestamps=ts_arr,
        feature_names=feature_names,
        failure_mode=failure_mode,
        horizon_s=horizon_s,
    )
