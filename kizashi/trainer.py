"""Training job: fit per-series baselines from long-term storage."""

from __future__ import annotations

import logging
import time

from kizashi.config import Config
from kizashi.models.baseline import BaselineModel, fit_baseline
from kizashi.models.store import ModelStore
from kizashi.sources.prometheus import PrometheusClient

log = logging.getLogger(__name__)


def train(config: Config, client: PrometheusClient, store: ModelStore, now: float | None = None) -> dict[str, int]:
    """Train baselines for every target that uses the baseline layer.

    Returns the number of fitted series per target metric. A target whose query
    fails raises, so a scheduler (CronJob) sees the failure and the previous
    model file stays in place.
    """
    now = time.time() if now is None else now
    start = now - config.training.period
    fitted: dict[str, int] = {}
    for target in config.targets:
        if "baseline" not in target.layers:
            continue
        series = client.query_range(target.query, start, now, config.training.step)
        models: dict[str, BaselineModel] = {}
        skipped = 0
        for s in series:
            model = fit_baseline(s.timestamps, s.values)
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
    return fitted
