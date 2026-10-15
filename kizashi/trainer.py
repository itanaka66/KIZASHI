"""Training job: fit per-series baselines from long-term storage."""

from __future__ import annotations

import logging
import time

import numpy as np

from kizashi.config import Config
from kizashi.models.baseline import BaselineModel, fit_baseline
from kizashi.models.store import ModelStore
from kizashi.sources.prometheus import PrometheusClient
from kizashi.topology import TopologyStore

log = logging.getLogger(__name__)


def train(
    config: Config,
    client: PrometheusClient,
    store: ModelStore,
    now: float | None = None,
    silences=None,        # list[Silence] | None — from AlertmanagerClient.active_silences()
    topology: TopologyStore | None = None,
    force_metrics: set[str] | None = None,  # retrain only these metrics (None = all)
) -> dict[str, int]:
    """Train baselines for every target that uses the baseline layer.

    Parameters
    ----------
    silences:
        Active Alertmanager silences. Time points that fall inside a silence
        window (plus ``config.alertmanager.silence_buffer`` on each side) are
        excluded from training so that past incidents do not distort the
        seasonal profile.
    topology:
        When provided, topology changes are detected and logged. After a
        successful fit the topology store is updated with the current series.
    force_metrics:
        When given, only targets whose metric name is in this set are retrained.
        Used for targeted re-fits after a topology change.

    Returns the number of fitted series per target metric.
    """
    now = time.time() if now is None else now
    start = now - config.training.period
    silence_buffer = config.alertmanager.silence_buffer if config.alertmanager else 1800.0
    fitted: dict[str, int] = {}
    current_topology: dict[str, set[str]] = {}

    for target in config.targets:
        if "baseline" not in target.layers:
            continue
        if force_metrics is not None and target.metric not in force_metrics:
            continue

        series = client.query_range(target.query, start, now, config.training.step)
        current_keys = {s.key for s in series}
        current_topology[target.metric] = current_keys

        # Topology change detection: log new / removed series.
        if topology is not None:
            diffs = topology.diff({target.metric: current_keys})
            for diff in diffs:
                if diff.added:
                    log.info(
                        "topology: %s — %d new series detected, fitting immediately",
                        diff.metric,
                        len(diff.added),
                    )
                if diff.removed:
                    log.info(
                        "topology: %s — %d series removed from Prometheus",
                        diff.metric,
                        len(diff.removed),
                    )

        models: dict[str, BaselineModel] = {}
        skipped = 0
        for s in series:
            ts, vals = s.timestamps, s.values

            # Mask out silence windows so past incidents don't inflate the band.
            if silences:
                mask = _silence_mask(ts, vals, s.labels, silences, silence_buffer)
                ts, vals = ts[mask], vals[mask]

            model = fit_baseline(ts, vals)
            if model is None:
                skipped += 1
            else:
                models[s.key] = model

        store.save_baselines(target.metric, models)
        fitted[target.metric] = len(models)
        log.info(
            "trained %s: %d series fitted, %d skipped (insufficient history)",
            target.metric,
            len(models),
            skipped,
        )

    # Persist updated topology only after all targets have been processed
    # successfully, so a partial failure doesn't advance the topology state.
    if topology is not None and current_topology:
        topology.update(current_topology)
        topology.purge_removed(current_topology)

    return fitted


def train_failure_models(
    config: Config,
    store: ModelStore,
    now: float | None = None,
    registry=None,  # ModelRegistry | NoOpRegistry | None
) -> dict[str, object]:
    """Train Layer 4 failure probability models from P3 feedback labels.

    Reads finalized labels from LabelStore (true_positive + confirmed_failure),
    builds FailureDataset per (failure_mode × horizon), runs walk-forward
    evaluation, and saves models that meet the promotion criteria.

    PSI check
    ---------
    If an existing model is on disk, the max per-feature PSI between the
    stored training distribution and the new data is computed. PSI ≥ 0.2
    marks the new model as shadow (``is_shadow=True``); otherwise it is
    promoted directly to production.

    Returns a dict mapping ``"{failure_mode}:{horizon_label}"`` to the
    ``WalkForwardResult`` for each (mode, horizon) pair that was evaluated.
    """
    from pathlib import Path  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    from kizashi.label_store import (  # noqa: PLC0415
        LabelStore,
        LABEL_TRUE_POSITIVE,
        LABEL_CONFIRMED_FAILURE,
    )
    from kizashi.models.failure import (  # noqa: PLC0415
        FailureDataset,
        WalkForwardResult,
        fit_failure_model,
        walk_forward_eval,
        psi_from_percentiles,
        PSI_RETRAIN,
        PSI_WARN,
        HORIZONS,
        MIN_POSITIVES,
        horizon_label as _hl,
    )

    now = time.time() if now is None else now

    if config.feedback is None:
        log.info("failure training: no feedback config; skipping")
        return {}

    label_path = Path(config.feedback.label_store_path) / "labels.jsonl"
    label_store = LabelStore(label_path)
    training_records = label_store.training_labels()

    if not training_records:
        log.info("failure training: no finalized labels found; skipping")
        return {}

    # Group positive records by failure_mode
    pos_by_mode: dict[str, list] = {}
    for rec in training_records:
        if rec.label in (LABEL_TRUE_POSITIVE, LABEL_CONFIRMED_FAILURE):
            pos_by_mode.setdefault(rec.failure_mode, []).append(rec)

    # Dummy feature names from multivariate config (or a minimal fallback)
    if config.multivariate and config.multivariate.features:
        feature_names = [f.name for f in config.multivariate.features]
    else:
        feature_names = ["anomaly_score", "forecast_delta", "band_breach_count"]

    results: dict[str, WalkForwardResult] = {}

    for failure_mode, records in pos_by_mode.items():
        n_pos = len(records)
        if n_pos < MIN_POSITIVES:
            log.info(
                "failure training: mode=%s has %d labels (need %d); skipping",
                failure_mode,
                n_pos,
                MIN_POSITIVES,
            )
            continue

        for hl, horizon_s in HORIZONS.items():
            # Build a minimal dataset from the feedback timestamps.
            # Positive samples: each failure record's `started_at - horizon_s`.
            pos_times = np.array([r.started_at - horizon_s for r in records], dtype=float)
            n_features = len(feature_names)
            # Without live feature snapshots at training time we synthesise
            # placeholder feature vectors (NaN-filled). Real deployments should
            # inject actual feature snapshots here.
            X_pos = np.zeros((n_pos, n_features), dtype=np.float64)
            y_pos = np.ones(n_pos, dtype=int)

            # Use 10× negatives at evenly-spaced past times
            n_neg = n_pos * 10
            neg_times = np.linspace(pos_times.min() - 86400, pos_times.max(), n_neg)
            X_neg = np.zeros((n_neg, n_features), dtype=np.float64)
            y_neg = np.zeros(n_neg, dtype=int)

            X = np.vstack([X_pos, X_neg])
            y = np.concatenate([y_pos, y_neg])
            ts = np.concatenate([pos_times, neg_times])

            dataset = FailureDataset(
                X=X, y=y, timestamps=ts,
                feature_names=feature_names,
                failure_mode=failure_mode,
                horizon_s=horizon_s,
            )

            result = walk_forward_eval(dataset)
            key = f"{failure_mode}:{hl}"
            results[key] = result

            log.info(
                "failure walk-forward mode=%s horizon=%s folds=%d prec=%.2f rec=%.2f meets=%s",
                failure_mode,
                hl,
                result.folds,
                result.mean_precision,
                result.mean_recall,
                result.meets_criteria,
            )

            if not result.meets_criteria:
                log.warning(
                    "failure training: mode=%s horizon=%s does not meet criteria; skipping save",
                    failure_mode,
                    hl,
                )
                continue

            try:
                meta, pipeline = fit_failure_model(dataset)
            except (ValueError, RuntimeError) as exc:
                log.warning("failure training: fit failed for %s/%s: %s", failure_mode, hl, exc)
                continue

            # PSI check against existing model
            is_shadow = False
            existing = store.load_failure(failure_mode, hl)
            if existing is not None:
                existing_meta, _ = existing
                if existing_meta.feature_percentiles:
                    psi = psi_from_percentiles(existing_meta.feature_percentiles, X)
                    meta.psi = psi
                    if psi >= PSI_RETRAIN:
                        is_shadow = True
                        log.warning(
                            "failure training: PSI=%.3f >= %.1f for mode=%s horizon=%s; "
                            "saving as shadow",
                            psi,
                            PSI_RETRAIN,
                            failure_mode,
                            hl,
                        )
                    elif psi >= PSI_WARN:
                        log.warning(
                            "failure training: PSI=%.3f >= %.1f (warn) for mode=%s horizon=%s",
                            psi,
                            PSI_WARN,
                            failure_mode,
                            hl,
                        )

            meta.is_shadow = is_shadow
            store.save_failure(failure_mode, hl, meta, pipeline, shadow=is_shadow)
            log.info(
                "failure training: saved mode=%s horizon=%s shadow=%s prec=%.2f rec=%.2f",
                failure_mode,
                hl,
                is_shadow,
                meta.precision_at_05,
                meta.recall,
            )

            if registry is not None and not is_shadow:
                try:
                    run_info = registry.log_run(meta, pipeline)
                    registry.promote_if_better(meta, run_info)
                except Exception as exc:
                    log.warning("failure training: MLflow log failed: %s", exc)

    return results


def _silence_mask(
    timestamps: np.ndarray,
    values: np.ndarray,
    labels: dict[str, str],
    silences,
    buffer: float,
) -> np.ndarray:
    """Return a boolean keep-mask; False = inside a matching silence ± buffer."""
    mask = np.ones(len(timestamps), dtype=bool)
    for silence in silences:
        if not silence.matches_labels(labels):
            continue
        lo = silence.start - buffer
        hi = silence.end + buffer
        mask &= ~((timestamps >= lo) & (timestamps <= hi))
    return mask
