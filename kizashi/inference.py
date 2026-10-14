"""Inference loop: query Prometheus, run the models, publish results."""

from __future__ import annotations

import logging
import threading
import time

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from kizashi.config import Config, TargetConfig, format_duration
from kizashi.exporter import Sample, SnapshotCollector
from kizashi.models.store import ModelStore
from kizashi.models.trend import fit_trend
from kizashi.series import Series, shard_of
from kizashi.sources.prometheus import PrometheusClient, PrometheusError

log = logging.getLogger(__name__)

FORECAST_QUANTILES = ("0.1", "0.5", "0.9")
# Same as Prometheus' default lookback delta.
STALE_AFTER = 300.0


class InferenceEngine:
    def __init__(
        self,
        config: Config,
        client: PrometheusClient,
        store: ModelStore,
        collector: SnapshotCollector,
        registry: CollectorRegistry,
    ):
        self.config = config
        self.client = client
        self.store = store
        self.collector = collector
        self._duration = Histogram(
            "kizashi_inference_duration_seconds",
            "Time spent on one inference cycle.",
            ["target"],
            registry=registry,
            buckets=(0.1, 0.5, 1, 2.5, 5, 10, 20, 30, 45, 60),
        )
        self._errors = Counter(
            "kizashi_query_errors_total", "Failed Prometheus queries.", ["target"], registry=registry
        )
        self._series = Gauge(
            "kizashi_inference_series", "Series processed in the last cycle.", ["target"], registry=registry
        )
        self._last_success = Gauge(
            "kizashi_last_inference_timestamp_seconds",
            "Unix time of the last completed inference cycle.",
            registry=registry,
        )

    def run_once(self, now: float | None = None) -> list[Sample]:
        now = time.time() if now is None else now
        samples: list[Sample] = []
        for target in self.config.targets:
            start = time.monotonic()
            try:
                series = self.client.query_range(
                    target.query, now - self.config.inference.window, now, self.config.inference.step
                )
            except PrometheusError as e:
                # Drop this target's outputs for the cycle so stale predictions are not served.
                log.warning("query failed for %s: %s", target.metric, e)
                self._errors.labels(target.metric).inc()
                continue
            shard = self.config.inference.shard
            mine = [s for s in series if shard_of(s.key, shard.total) == shard.index]
            samples.extend(self.infer_target(target, mine, now))
            self._series.labels(target.metric).set(len(mine))
            self._duration.labels(target.metric).observe(time.monotonic() - start)
        self.collector.publish(samples)
        self._last_success.set(now)
        return samples

    def infer_target(self, target: TargetConfig, series: list[Series], now: float) -> list[Sample]:
        cfg = self.config
        baselines = self.store.load_baselines(target.metric) if "baseline" in target.layers else None
        samples: list[Sample] = []
        if baselines is not None:
            model_labels = {"layer": "baseline", "metric": target.metric}
            samples += [
                Sample("kizashi_model_info", {**model_labels, "version": baselines.version}, 1.0),
                Sample("kizashi_model_trained_timestamp_seconds", model_labels, baselines.trained_at),
            ]

        for s in series:
            s = s.finite()
            if len(s) == 0:
                continue
            ts, value = float(s.timestamps[-1]), float(s.values[-1])
            if now - ts > STALE_AFTER + cfg.inference.step:
                # The device stopped reporting inside the window; don't predict from old data.
                continue
            labels = _output_labels(target.metric, s.labels)
            score = None

            model = baselines.models.get(s.key) if baselines else None
            if model is not None:
                r = model.score(ts, value, cfg.baseline.band_sigma)
                score = r.score
                samples += [
                    Sample("kizashi_expected_value", labels, r.expected),
                    Sample("kizashi_band_lower", labels, r.lower),
                    Sample("kizashi_band_upper", labels, r.upper),
                    Sample("kizashi_anomaly_score", {**labels, "layer": "baseline"}, r.score),
                ]

            if "trend" not in target.layers:
                continue
            fit = fit_trend(
                s.timestamps,
                s.values,
                confidence=cfg.trend.confidence,
                max_points=cfg.trend.max_points,
                min_points=cfg.trend.min_points,
            )
            if fit is None:
                continue
            # The fit is anchored at the last sample; shift horizons so they are relative to now.
            lag = max(0.0, now - ts)
            heading_to_threshold = False
            for th in target.thresholds:
                limit = th.resolve(s.labels)
                if limit is None:
                    continue
                eta = fit.time_to_threshold(limit, th.direction, cfg.trend.max_horizon)
                if eta is None:
                    continue
                heading_to_threshold = True
                samples.append(
                    Sample(
                        "kizashi_time_to_threshold_seconds",
                        {**labels, "threshold": th.name},
                        max(0.0, eta - lag),
                    )
                )
            if heading_to_threshold or (score is not None and score >= cfg.inference.emit_forecast_min_score):
                for horizon in cfg.inference.forecast_horizons:
                    band = fit.forecast(horizon + lag)
                    for q, v in zip(FORECAST_QUANTILES, band):
                        samples.append(
                            Sample(
                                "kizashi_forecast_value",
                                {**labels, "horizon": format_duration(horizon), "quantile": q},
                                v,
                            )
                        )
        return samples

    def run_forever(self, stop: threading.Event) -> None:
        interval = self.config.inference.interval
        while not stop.is_set():
            started = time.time()
            try:
                self.run_once(started)
            except Exception:
                log.exception("inference cycle failed")
            elapsed = time.time() - started
            if elapsed > interval:
                log.warning("inference cycle took %.1fs, longer than interval %.0fs", elapsed, interval)
            stop.wait(max(0.0, interval - elapsed))


def _output_labels(metric: str, labels: dict[str, str]) -> dict[str, str]:
    out = {k: v for k, v in labels.items() if k != "__name__"}
    if "metric" in out:
        out["exported_metric"] = out.pop("metric")
    out["metric"] = metric
    return out

