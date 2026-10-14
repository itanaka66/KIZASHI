import pytest

from kizashi.config import ConfigError, format_duration, load_config, parse_config, parse_duration

MINIMAL = {"prometheus": {"url": "http://prom:9090/"}, "targets": [{"metric": "up"}]}


@pytest.mark.parametrize(
    "text,seconds",
    [("30s", 30), ("1m", 60), ("6h", 21600), ("28d", 28 * 86400), ("1w", 604800), ("1h30m", 5400), (90, 90)],
)
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "5", "1x", "h1", "1h foo"])
def test_parse_duration_invalid(text):
    with pytest.raises(ConfigError):
        parse_duration(text)


def test_format_duration():
    assert [format_duration(s) for s in (60, 3600, 86400, 5400, 45)] == ["1m", "1h", "24h", "90m", "45s"]


def test_defaults():
    cfg = parse_config(MINIMAL)
    assert cfg.prometheus.url == "http://prom:9090"
    assert cfg.prometheus.long_term_url == "http://prom:9090"
    assert cfg.listen_port == 9850
    assert cfg.inference.window == 6 * 3600
    assert cfg.targets[0].layers == ["baseline"]


def test_threshold_by_label():
    cfg = parse_config(
        {
            **MINIMAL,
            "targets": [
                {
                    "metric": "rx",
                    "match": '{job="snmp"}',
                    "layers": ["trend"],
                    "thresholds": [
                        {"name": "rx_low", "direction": "below", "by_label": "type", "values": {"LR": -14.4}}
                    ],
                }
            ],
        }
    )
    t = cfg.targets[0]
    assert t.query == 'rx{job="snmp"}'
    assert t.thresholds[0].resolve({"type": "LR"}) == -14.4
    assert t.thresholds[0].resolve({"type": "SR"}) is None


@pytest.mark.parametrize(
    "target",
    [
        {"metric": "x", "layers": ["failure_probability"]},
        {"metric": "x", "match": 'job="snmp"'},
        {"metric": "x", "thresholds": [{"name": "t", "direction": "sideways", "value": 1}]},
        {"metric": "x", "thresholds": [{"name": "t"}]},
    ],
)
def test_invalid_targets(target):
    with pytest.raises(ConfigError):
        parse_config({**MINIMAL, "targets": [target]})


def test_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARD_INDEX", "1")
    path = tmp_path / "k.yaml"
    path.write_text(
        "# ${UNSET_IN_COMMENT} is ignored\n"
        "prometheus: {url: 'http://${PROM_HOST:-localhost}:9090'}\n"
        "inference:\n  shard:\n    total: 2\n    index: ${SHARD_INDEX}\n"
        "targets: [{metric: up}]\n"
    )
    cfg = load_config(path)
    assert cfg.prometheus.url == "http://localhost:9090"
    assert cfg.inference.shard.index == 1
