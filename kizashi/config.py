"""Configuration loading (kizashi.yaml)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

IMPLEMENTED_LAYERS = {"baseline", "trend"}

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w)")
_DURATION_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_ENV_RE = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    pass


def parse_duration(value: str | int | float) -> float:
    """Parse a Prometheus-style duration ("90s", "1h30m", "28d") into seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    text = value.strip()
    pos, total = 0, 0.0
    for m in _DURATION_RE.finditer(text):
        if m.start() != pos:
            break
        total += float(m.group(1)) * _DURATION_UNITS[m.group(2)]
        pos = m.end()
    if pos != len(text) or not text:
        raise ConfigError(f"invalid duration: {value!r}")
    return total


def format_duration(seconds: float) -> str:
    """Format seconds as a compact label value ("1h", "24h", "90m")."""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            if unit == "d":
                return f"{int(seconds // 3600)}h"
            return f"{int(seconds // size)}{unit}"
    return f"{int(seconds)}s"


def _expand_env(text: str) -> str:
    def repl(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        raise ConfigError(f"environment variable {name} is not set")

    return _ENV_RE.sub(repl, text)


@dataclass
class PrometheusConfig:
    url: str
    long_term_url: str
    timeout: float = 30.0
    bearer_token_file: str | None = None


@dataclass
class ShardConfig:
    total: int = 1
    index: int = 0


@dataclass
class InferenceConfig:
    interval: float = 60.0
    window: float = 6 * 3600.0
    step: float = 60.0
    shard: ShardConfig = field(default_factory=ShardConfig)
    forecast_horizons: list[float] = field(default_factory=lambda: [3600.0, 86400.0])
    # Forecasts are only exported for series that look interesting, to bound cardinality.
    emit_forecast_min_score: float = 3.0


@dataclass
class TrainingConfig:
    period: float = 28 * 86400.0
    step: float = 300.0


@dataclass
class BaselineConfig:
    band_sigma: float = 3.0


@dataclass
class TrendConfig:
    confidence: float = 0.9
    max_points: int = 120
    max_horizon: float = 30 * 86400.0
    min_points: int = 10


@dataclass
class Threshold:
    name: str
    direction: str  # "above" or "below"
    value: float | None = None
    by_label: str | None = None
    values: dict[str, float] = field(default_factory=dict)

    def resolve(self, labels: dict[str, str]) -> float | None:
        if self.by_label is not None:
            return self.values.get(labels.get(self.by_label, ""), self.value)
        return self.value


@dataclass
class TargetConfig:
    metric: str
    match: str = ""
    layers: list[str] = field(default_factory=lambda: ["baseline"])
    thresholds: list[Threshold] = field(default_factory=list)

    @property
    def query(self) -> str:
        return f"{self.metric}{self.match}"


@dataclass
class Config:
    prometheus: PrometheusConfig
    targets: list[TargetConfig]
    listen_address: str = "0.0.0.0"
    listen_port: int = 9850
    model_dir: Path = Path("./models")
    inference: InferenceConfig = field(default_factory=InferenceConfig)
    training: TrainingConfig = field(default_factory=TrainingConfig)
    baseline: BaselineConfig = field(default_factory=BaselineConfig)
    trend: TrendConfig = field(default_factory=TrendConfig)


def _threshold(raw: dict) -> Threshold:
    direction = raw.get("direction", "above")
    if direction not in ("above", "below"):
        raise ConfigError(f"threshold {raw.get('name')}: direction must be 'above' or 'below'")
    t = Threshold(
        name=raw["name"],
        direction=direction,
        value=float(raw["value"]) if "value" in raw else None,
        by_label=raw.get("by_label"),
        values={str(k): float(v) for k, v in (raw.get("values") or {}).items()},
    )
    if t.value is None and not t.values:
        raise ConfigError(f"threshold {t.name}: either 'value' or 'values' is required")
    return t


def _target(raw: dict) -> TargetConfig:
    if "metric" not in raw:
        raise ConfigError("target: 'metric' is required")
    layers = list(raw.get("layers", ["baseline"]))
    unknown = set(layers) - IMPLEMENTED_LAYERS
    if unknown:
        raise ConfigError(f"target {raw['metric']}: unsupported layers {sorted(unknown)}")
    match = raw.get("match", "")
    if match and not (match.startswith("{") and match.endswith("}")):
        raise ConfigError(f"target {raw['metric']}: match must be a label matcher like '{{job=\"snmp\"}}'")
    return TargetConfig(
        metric=raw["metric"],
        match=match,
        layers=layers,
        thresholds=[_threshold(t) for t in raw.get("thresholds", [])],
    )


def _expand_tree(node):
    # Expand after parsing so ${...} inside YAML comments is left alone.
    if isinstance(node, str):
        return _expand_env(node)
    if isinstance(node, dict):
        return {k: _expand_tree(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand_tree(v) for v in node]
    return node


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return parse_config(_expand_tree(raw))


def parse_config(raw: dict) -> Config:
    prom = raw.get("prometheus") or {}
    if "url" not in prom:
        raise ConfigError("prometheus.url is required")
    targets = [_target(t) for t in raw.get("targets", [])]
    if not targets:
        raise ConfigError("at least one target is required")

    inf = raw.get("inference") or {}
    shard = inf.get("shard") or {}
    shard_cfg = ShardConfig(total=int(shard.get("total", 1)), index=int(shard.get("index", 0)))
    if not 0 <= shard_cfg.index < shard_cfg.total:
        raise ConfigError("inference.shard.index must be in [0, total)")

    defaults = InferenceConfig()
    inference = InferenceConfig(
        interval=parse_duration(inf.get("interval", defaults.interval)),
        window=parse_duration(inf.get("window", defaults.window)),
        step=parse_duration(inf.get("step", defaults.step)),
        shard=shard_cfg,
        forecast_horizons=[parse_duration(h) for h in inf.get("forecast_horizons", ["1h", "24h"])],
        emit_forecast_min_score=float(inf.get("emit_forecast_min_score", defaults.emit_forecast_min_score)),
    )

    tr = raw.get("training") or {}
    training = TrainingConfig(
        period=parse_duration(tr.get("period", TrainingConfig.period)),
        step=parse_duration(tr.get("step", TrainingConfig.step)),
    )

    bl = raw.get("baseline") or {}
    td = raw.get("trend") or {}
    trend_defaults = TrendConfig()

    server = raw.get("server") or {}
    host, _, port = str(server.get("listen", "0.0.0.0:9850")).rpartition(":")

    return Config(
        prometheus=PrometheusConfig(
            url=prom["url"].rstrip("/"),
            long_term_url=(prom.get("long_term_url") or prom["url"]).rstrip("/"),
            timeout=parse_duration(prom.get("timeout", "30s")),
            bearer_token_file=prom.get("bearer_token_file"),
        ),
        targets=targets,
        listen_address=host or "0.0.0.0",
        listen_port=int(port),
        model_dir=Path((raw.get("models") or {}).get("dir", "./models")),
        inference=inference,
        training=training,
        baseline=BaselineConfig(band_sigma=float(bl.get("band_sigma", 3.0))),
        trend=TrendConfig(
            confidence=float(td.get("confidence", trend_defaults.confidence)),
            max_points=int(td.get("max_points", trend_defaults.max_points)),
            max_horizon=parse_duration(td.get("max_horizon", trend_defaults.max_horizon)),
            min_points=int(td.get("min_points", trend_defaults.min_points)),
        ),
    )
