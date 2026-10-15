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
