"""Expose inference results on /metrics.

Results are published as a whole snapshot per inference cycle. A series that is
missing from the latest snapshot simply disappears from /metrics, so Prometheus
marks it stale instead of keeping an outdated prediction alive.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass

from prometheus_client.core import Metric

FAMILIES = {
    "kizashi_expected_value": "Layer 1 expected value from the seasonal baseline.",
    "kizashi_band_lower": "Layer 1 lower adaptive threshold.",
    "kizashi_band_upper": "Layer 1 upper adaptive threshold.",
    "kizashi_anomaly_score": "Robust z-score of the latest value (0 = normal, >= 3 is unusual).",
    "kizashi_forecast_value": "Layer 2 forecast at now + horizon.",
    "kizashi_time_to_threshold_seconds": "Layer 2 estimated seconds until the trend crosses the threshold.",
    "kizashi_model_info": "Model currently used for inference (value is always 1).",
    "kizashi_model_trained_timestamp_seconds": "Unix time when the model in use was trained.",
    # Layer 4: failure probability prediction (P4)
    "kizashi_failure_probability": "Layer 4 calibrated failure probability [0, 1] per failure_mode and horizon.",
    # Layer 4: SHAP top contributing features (P4)
    "kizashi_top_contributor": "SHAP contribution value for the top-K features driving the failure probability prediction.",
}


@dataclass(frozen=True)
class Sample:
    name: str
    labels: dict[str, str]
    value: float


class SnapshotCollector:
    def __init__(self):
        self._lock = threading.Lock()
        self._samples: list[Sample] = []

    def publish(self, samples: list[Sample]) -> None:
        with self._lock:
            self._samples = samples

    def samples(self) -> list[Sample]:
        with self._lock:
            return list(self._samples)

    def collect(self):
        by_name: dict[str, list[Sample]] = defaultdict(list)
        for s in self.samples():
            by_name[s.name].append(s)
        for name, samples in by_name.items():
            family = Metric(name, FAMILIES.get(name, name), "gauge")
            for s in samples:
                family.add_sample(name, s.labels, s.value)
            yield family

    def describe(self):
        # Families vary per snapshot; returning nothing keeps registration lazy.
        return []
