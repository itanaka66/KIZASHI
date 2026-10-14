"""Train -> infer -> /metrics, against a fake Prometheus."""

import os
import time

import numpy as np
import pytest
from prometheus_client import CollectorRegistry, generate_latest

from kizashi.config import parse_config
from kizashi.exporter import SnapshotCollector
from kizashi.inference import InferenceEngine, _output_labels
from kizashi.models.store import ModelStore
from kizashi.series import Series
from kizashi.sources.prometheus import PrometheusClient
from kizashi.trainer import train
from tests.fakes import FakePrometheus

DAY, HOUR = 86400, 3600
NOW = 1_760_000_000.0 - (1_760_000_000.0 % 60)
RX = "kizashi:optic_rx_dbm:avg5m"
ERR = "kizashi:if_in_errors:rate5m"

HEALTHY = {"__name__": RX, "instance": "rt01", "ifName": "Te0/1", "transceiver_type": "10GBASE-LR"}
DEGRADING = {"__name__": RX, "instance": "rt01", "ifName": "Te0/2", "transceiver_type": "10GBASE-LR"}
ERRORS = {"__name__": ERR, "instance": "rt02", "ifName": "Gi0/1"}


def build_data():
    rng = np.random.default_rng(1)
    ts = np.arange(NOW - 21 * DAY, NOW + 1, 60.0)
    healthy = -6.0 + rng.normal(0, 0.05, len(ts))
    # Stable for 3 weeks, then losing 0.1 dBm/h over the last 6 hours.
    degrading = -7.0 + rng.normal(0, 0.05, len(ts))
    recent = ts > NOW - 6 * HOUR
    degrading[recent] -= 0.1 * (ts[recent] - (NOW - 6 * HOUR)) / HOUR
    # Error rate follows a business-hours pattern, with a burst in the latest sample.
    hour = (ts % DAY) / HOUR
    errors = 1 + 4 * ((hour >= 9) & (hour < 18)) + rng.normal(0, 0.2, len(ts))
    errors[-1] = 40
    return {
        RX: [(HEALTHY, ts, healthy), (DEGRADING, ts, degrading)],
        ERR: [(ERRORS, ts, errors)],
    }


CONFIG = {
    "prometheus": {"url": "http://prom"},
    "inference": {"forecast_horizons": ["1h", "24h"]},
    "training": {"period": "21d", "step": "5m"},
    "targets": [
        {
            "metric": RX,
            "layers": ["baseline", "trend"],
            "thresholds": [
                {
                    "name": "rx_low",
                    "direction": "below",
                    "by_label": "transceiver_type",
                    "values": {"10GBASE-LR": -14.4},
                }
            ],
        },
        {"metric": ERR, "layers": ["baseline"]},
    ],
}


@pytest.fixture
def env(tmp_path):
    fake = FakePrometheus(build_data())
    cfg = parse_config({**CONFIG, "models": {"dir": str(tmp_path)}})
    client = PrometheusClient("http://prom", transport=fake.transport)
    registry = CollectorRegistry()
    collector = SnapshotCollector()
    registry.register(collector)
    store = ModelStore(tmp_path)
    engine = InferenceEngine(cfg, client, store, collector, registry)
    return cfg, client, store, engine, registry, fake


def by_name(samples, name):
    return [s for s in samples if s.name == name]


def test_train_then_infer(env):
    cfg, client, store, engine, registry, _ = env
    assert train(cfg, client, store, now=NOW) == {RX: 2, ERR: 1}

    samples = engine.run_once(NOW)

    scores = {(s.labels["metric"], s.labels["ifName"]): s.value for s in by_name(samples, "kizashi_anomaly_score")}
    assert scores[(RX, "Te0/1")] < 3
    assert scores[(ERR, "Gi0/1")] > 10

    [eta] = by_name(samples, "kizashi_time_to_threshold_seconds")
    assert eta.labels["ifName"] == "Te0/2"
    assert eta.labels["threshold"] == "rx_low"
    # -7.6 dBm now, -0.1 dBm/h: about 68h to -14.4 dBm.
    assert abs(eta.value / HOUR - 68) < 4

    forecasts = by_name(samples, "kizashi_forecast_value")
    # Only the series heading to its threshold gets forecasts (cardinality control).
    assert {s.labels["ifName"] for s in forecasts} == {"Te0/2"}
    assert {(s.labels["horizon"], s.labels["quantile"]) for s in forecasts} == {
        (h, q) for h in ("1h", "24h") for q in ("0.1", "0.5", "0.9")
    }

    info = by_name(samples, "kizashi_model_info")
    assert {s.labels["metric"] for s in info} == {RX, ERR}
    trained = by_name(samples, "kizashi_model_trained_timestamp_seconds")
    assert len(trained) == 2 and all(s.value > 0 for s in trained)

    text = generate_latest(registry).decode()
    assert "# TYPE kizashi_anomaly_score gauge" in text
    assert 'kizashi_time_to_threshold_seconds{' in text
    assert "kizashi_last_inference_timestamp_seconds" in text


def test_without_trained_models_only_trend_runs(env):
    _, _, _, engine, _, _ = env
    samples = engine.run_once(NOW)
    assert not by_name(samples, "kizashi_anomaly_score")
    assert len(by_name(samples, "kizashi_time_to_threshold_seconds")) == 1


def test_query_failure_drops_stale_outputs(env):
    cfg, client, store, engine, registry, fake = env
    train(cfg, client, store, now=NOW)
    assert engine.run_once(NOW)
    fake.fail = True
    assert engine.run_once(NOW + 60) == []
    assert "kizashi_anomaly_score{" not in generate_latest(registry).decode()
    assert registry.get_sample_value("kizashi_query_errors_total", {"target": RX}) == 1


def test_sharding_partitions_series(env, tmp_path):
    _, client, store, _, _, _ = env
    total = 0
    for index in range(2):
        cfg = parse_config(
            {**CONFIG, "models": {"dir": str(tmp_path)}, "inference": {"shard": {"total": 2, "index": index}}}
        )
        registry = CollectorRegistry()
        InferenceEngine(cfg, client, store, SnapshotCollector(), registry).run_once(NOW)
        total += sum(registry.get_sample_value("kizashi_inference_series", {"target": t.metric}) for t in cfg.targets)
    assert total == 3


def test_metric_label_collision():
    labels = _output_labels(RX, {"__name__": "x", "instance": "rt01", "metric": "original"})
    assert labels == {"instance": "rt01", "exported_metric": "original", "metric": RX}


def test_model_hot_reload(env):
    cfg, client, store, _, _, _ = env
    train(cfg, client, store, now=NOW)
    first = store.load_baselines(RX)
    assert store.load_baselines(RX) is first
    path = store.root / "baseline" / "kizashi_optic_rx_dbm_avg5m.json"
    later = time.time() + 10
    os.utime(path, (later, later))
    assert store.load_baselines(RX) is not first


def test_series_that_stopped_reporting_is_skipped(env):
    cfg, _, _, engine, _, _ = env
    ts = np.arange(NOW - 6 * HOUR, NOW - 2 * HOUR, 60.0)
    values = -7 - 0.1 * (ts - ts[0]) / HOUR
    [target] = [t for t in cfg.targets if t.metric == RX]
    assert engine.infer_target(target, [Series(DEGRADING, ts, values)], NOW) == []
    assert engine.infer_target(target, [Series(DEGRADING, ts, values)], ts[-1] + 60)
