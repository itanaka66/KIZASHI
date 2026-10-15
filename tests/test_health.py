"""Tests for kizashi/health.py (P5 service health score)."""

from __future__ import annotations

import pytest

from kizashi.exporter import Sample
from kizashi.health import (
    MAX_ANOMALY_SCORE,
    ComponentDef,
    ServiceDef,
    ServicesConfigError,
    ServicesSpec,
    _extract_anomaly_scores,
    _extract_failure_probs,
    component_key,
    compute_health_samples,
    compute_service_health,
    load_services,
    validate_services_spec,
)


# ──────────────────────────── component_key ──────────────────────────────────


def test_component_key_with_ifname():
    assert component_key("sw1:9116", "Gi0/0") == "sw1:9116:Gi0/0"


def test_component_key_without_ifname():
    assert component_key("sw1:9116", "") == "sw1:9116"


# ──────────────────────────── validate_services_spec ─────────────────────────


def test_validate_minimal_spec():
    spec = validate_services_spec({
        "services": [
            {"name": "WAN", "components": [{"instance": "r1:9116"}]}
        ]
    })
    assert len(spec.services) == 1
    assert spec.services[0].name == "WAN"
    assert spec.services[0].components[0].instance == "r1:9116"


def test_validate_defaults():
    spec = validate_services_spec({
        "services": [
            {"name": "S", "components": [{"instance": "r1"}]}
        ]
    })
    comp = spec.services[0].components[0]
    assert comp.if_name == ""
    assert comp.weight == 1.0
    assert comp.redundancy_group == ""


def test_validate_ifname_alias():
    spec = validate_services_spec({
        "services": [
            {"name": "S", "components": [{"instance": "r1", "ifName": "Gi0/0"}]}
        ]
    })
    assert spec.services[0].components[0].if_name == "Gi0/0"


def test_validate_redundancy_group():
    spec = validate_services_spec({
        "services": [
            {"name": "S", "components": [
                {"instance": "r1", "redundancy_group": "uplink"},
                {"instance": "r2", "redundancy_group": "uplink"},
            ]}
        ]
    })
    assert spec.services[0].components[0].redundancy_group == "uplink"
    assert spec.services[0].components[1].redundancy_group == "uplink"


def test_validate_missing_service_name_raises():
    with pytest.raises(ServicesConfigError, match="name"):
        validate_services_spec({"services": [{"components": [{"instance": "r1"}]}]})


def test_validate_empty_components_raises():
    with pytest.raises(ServicesConfigError, match="component"):
        validate_services_spec({"services": [{"name": "S", "components": []}]})


def test_validate_missing_instance_raises():
    with pytest.raises(ServicesConfigError, match="instance"):
        validate_services_spec({"services": [{"name": "S", "components": [{"weight": 1}]}]})


def test_validate_negative_weight_raises():
    with pytest.raises(ServicesConfigError, match="weight"):
        validate_services_spec({
            "services": [{"name": "S", "components": [{"instance": "r1", "weight": -1}]}]
        })


def test_validate_not_a_dict_raises():
    with pytest.raises(ServicesConfigError):
        validate_services_spec("not a dict")


def test_validate_multiple_services():
    spec = validate_services_spec({
        "services": [
            {"name": "A", "components": [{"instance": "r1"}]},
            {"name": "B", "components": [{"instance": "r2"}]},
        ]
    })
    assert len(spec.services) == 2


# ──────────────────────────── load_services ──────────────────────────────────


def test_load_services_from_file(tmp_path):
    yaml_content = """\
services:
  - name: "WAN"
    components:
      - instance: router1:9116
        if_name: Gi0/0
        weight: 2.0
        redundancy_group: uplink
      - instance: router2:9116
        if_name: Gi0/0
        weight: 2.0
        redundancy_group: uplink
"""
    p = tmp_path / "services.yaml"
    p.write_text(yaml_content)
    spec = load_services(p)
    assert len(spec.services) == 1
    assert spec.services[0].name == "WAN"
    assert spec.services[0].components[0].redundancy_group == "uplink"


# ──────────────────────── compute_service_health ─────────────────────────────


def _svc(components: list[ComponentDef]) -> ServiceDef:
    return ServiceDef(name="Test", components=components)


def test_all_healthy_gives_100():
    svc = _svc([ComponentDef("r1", "Gi0/0")])
    result = compute_service_health(svc, {"r1:Gi0/0": 0.0})
    assert result["now"] == 100.0


def test_fully_degraded_component_gives_0():
    svc = _svc([ComponentDef("r1", "Gi0/0", weight=1.0)])
    result = compute_service_health(svc, {"r1:Gi0/0": MAX_ANOMALY_SCORE})
    assert result["now"] == 0.0


def test_half_degraded_gives_50():
    svc = _svc([ComponentDef("r1", "Gi0/0", weight=1.0)])
    result = compute_service_health(svc, {"r1:Gi0/0": MAX_ANOMALY_SCORE / 2})
    assert abs(result["now"] - 50.0) < 0.01


def test_missing_component_defaults_to_healthy():
    svc = _svc([ComponentDef("r1", "Gi0/0")])
    result = compute_service_health(svc, {})
    assert result["now"] == 100.0


def test_score_clamped_to_zero_on_overflow():
    svc = _svc([ComponentDef("r1", "Gi0/0", weight=1.0)])
    result = compute_service_health(svc, {"r1:Gi0/0": MAX_ANOMALY_SCORE * 2})
    assert result["now"] == 0.0


def test_two_non_redundant_components_average():
    svc = _svc([
        ComponentDef("r1", "Gi0/0", weight=1.0),
        ComponentDef("r2", "Gi0/0", weight=1.0),
    ])
    scores = {"r1:Gi0/0": MAX_ANOMALY_SCORE, "r2:Gi0/0": 0.0}
    result = compute_service_health(svc, scores)
    # One of two equal-weight components is fully degraded → 50%
    assert abs(result["now"] - 50.0) < 0.01


def test_redundant_pair_no_penalty_when_one_healthy():
    # Both in same redundancy_group → effective = min(scores)
    # r1 is fine (score=0), r2 is degraded → min=0 → no deduction
    svc = _svc([
        ComponentDef("r1", "Gi0/0", weight=1.0, redundancy_group="uplink"),
        ComponentDef("r2", "Gi0/0", weight=1.0, redundancy_group="uplink"),
    ])
    scores = {"r1:Gi0/0": 0.0, "r2:Gi0/0": MAX_ANOMALY_SCORE}
    result = compute_service_health(svc, scores)
    assert result["now"] == 100.0


def test_redundant_pair_full_penalty_when_both_degraded():
    svc = _svc([
        ComponentDef("r1", "Gi0/0", weight=1.0, redundancy_group="uplink"),
        ComponentDef("r2", "Gi0/0", weight=1.0, redundancy_group="uplink"),
    ])
    scores = {"r1:Gi0/0": MAX_ANOMALY_SCORE, "r2:Gi0/0": MAX_ANOMALY_SCORE}
    result = compute_service_health(svc, scores)
    assert result["now"] == 0.0


def test_redundant_pair_partial_when_min_nonzero():
    # Both at 50% → min = 0.5 → group contributes 50% of its weight
    svc = _svc([
        ComponentDef("r1", "Gi0/0", weight=1.0, redundancy_group="uplink"),
        ComponentDef("r2", "Gi0/0", weight=1.0, redundancy_group="uplink"),
    ])
    scores = {"r1:Gi0/0": MAX_ANOMALY_SCORE / 2, "r2:Gi0/0": MAX_ANOMALY_SCORE / 2}
    result = compute_service_health(svc, scores)
    assert abs(result["now"] - 50.0) < 0.01


def test_mixed_redundant_and_singleton():
    # Group (r1+r2 redundant weight=2 total) + singleton r3 (weight=1)
    # r1 healthy, r2 degraded → group deduction = 0
    # r3 degraded → singleton deduction = 1 * 1.0 / 3 total_weight * 100 = 33.3
    svc = _svc([
        ComponentDef("r1", "Gi0/0", weight=1.0, redundancy_group="uplink"),
        ComponentDef("r2", "Gi0/0", weight=1.0, redundancy_group="uplink"),
        ComponentDef("r3", "Gi0/0", weight=1.0),
    ])
    scores = {
        "r1:Gi0/0": 0.0,
        "r2:Gi0/0": MAX_ANOMALY_SCORE,
        "r3:Gi0/0": MAX_ANOMALY_SCORE,
    }
    result = compute_service_health(svc, scores)
    # Only r3 (weight=1 of total=3) is penalised → score = 100 - 1/3*100 ≈ 66.7
    assert abs(result["now"] - (100.0 - 100.0 / 3)) < 0.1


def test_future_score_returned_when_probs_provided():
    svc = _svc([ComponentDef("r1", "Gi0/0", weight=1.0)])
    result = compute_service_health(svc, {}, failure_probs_1h={"r1:Gi0/0": 0.5})
    assert "1h" in result
    assert abs(result["1h"] - 50.0) < 0.01


def test_future_score_absent_when_probs_not_provided():
    svc = _svc([ComponentDef("r1", "Gi0/0")])
    result = compute_service_health(svc, {})
    assert "1h" not in result


def test_empty_service_gives_100():
    # Zero total weight → no deduction possible
    svc = ServiceDef(name="Empty", components=[ComponentDef("r1", "Gi0/0", weight=0.0)])
    result = compute_service_health(svc, {"r1:Gi0/0": MAX_ANOMALY_SCORE})
    assert result["now"] == 100.0


# ─────────────────────── _extract_anomaly_scores ─────────────────────────────


def test_extract_anomaly_scores_basic():
    snapshot = [
        Sample("kizashi_anomaly_score", {"instance": "sw1:9116", "ifName": "Gi0/0", "layer": "baseline"}, 5.0),
        Sample("kizashi_anomaly_score", {"instance": "sw2:9116", "ifName": "Gi0/1", "layer": "baseline"}, 2.0),
    ]
    scores = _extract_anomaly_scores(snapshot)
    assert scores["sw1:9116:Gi0/0"] == 5.0
    assert scores["sw2:9116:Gi0/1"] == 2.0


def test_extract_takes_max_across_layers():
    snapshot = [
        Sample("kizashi_anomaly_score", {"instance": "sw1", "ifName": "Gi0", "layer": "baseline"}, 3.0),
        Sample("kizashi_anomaly_score", {"instance": "sw1", "ifName": "Gi0", "layer": "multivariate"}, 7.0),
    ]
    scores = _extract_anomaly_scores(snapshot)
    assert scores["sw1:Gi0"] == 7.0


def test_extract_ignores_other_metrics():
    snapshot = [
        Sample("kizashi_expected_value", {"instance": "sw1", "ifName": "Gi0"}, 9.0),
        Sample("kizashi_anomaly_score", {"instance": "sw1", "ifName": "Gi0", "layer": "baseline"}, 1.5),
    ]
    scores = _extract_anomaly_scores(snapshot)
    assert len(scores) == 1


def test_extract_no_ifname():
    snapshot = [
        Sample("kizashi_anomaly_score", {"instance": "sw1:9116", "layer": "baseline"}, 4.0),
    ]
    scores = _extract_anomaly_scores(snapshot)
    assert scores["sw1:9116"] == 4.0


# ─────────────────────── _extract_failure_probs ──────────────────────────────


def test_extract_failure_probs_basic():
    snapshot = [
        Sample("kizashi_failure_probability",
               {"instance": "sw1:9116", "ifName": "Gi0/0", "failure_mode": "rx_low", "horizon": "1h"}, 0.7),
    ]
    probs = _extract_failure_probs(snapshot, horizon="1h")
    assert probs["sw1:9116:Gi0/0"] == 0.7


def test_extract_failure_probs_filters_by_horizon():
    snapshot = [
        Sample("kizashi_failure_probability",
               {"instance": "sw1", "ifName": "G0", "failure_mode": "m", "horizon": "24h"}, 0.9),
        Sample("kizashi_failure_probability",
               {"instance": "sw1", "ifName": "G0", "failure_mode": "m", "horizon": "1h"}, 0.3),
    ]
    probs = _extract_failure_probs(snapshot, horizon="1h")
    assert probs["sw1:G0"] == 0.3


# ────────────────────── compute_health_samples ───────────────────────────────


def test_compute_health_samples_returns_sample_objects():
    spec = ServicesSpec(services=[
        ServiceDef("WAN", [ComponentDef("r1:9116", "Gi0/0", weight=1.0)])
    ])
    snapshot = [
        Sample("kizashi_anomaly_score", {"instance": "r1:9116", "ifName": "Gi0/0", "layer": "baseline"}, 2.0),
    ]
    samples = compute_health_samples(spec, snapshot)
    assert any(s.name == "kizashi_health_score" for s in samples)


def test_compute_health_samples_has_now_horizon():
    spec = ServicesSpec(services=[
        ServiceDef("WAN", [ComponentDef("r1", weight=1.0)])
    ])
    samples = compute_health_samples(spec, [])
    horizons = {s.labels["horizon"] for s in samples if s.name == "kizashi_health_score"}
    assert "now" in horizons


def test_compute_health_samples_includes_1h_when_failure_probs_present():
    spec = ServicesSpec(services=[
        ServiceDef("WAN", [ComponentDef("r1:9116", "Gi0/0", weight=1.0)])
    ])
    snapshot = [
        Sample("kizashi_anomaly_score",
               {"instance": "r1:9116", "ifName": "Gi0/0", "layer": "baseline"}, 1.0),
        Sample("kizashi_failure_probability",
               {"instance": "r1:9116", "ifName": "Gi0/0", "failure_mode": "m", "horizon": "1h"}, 0.2),
    ]
    samples = compute_health_samples(spec, snapshot)
    horizons = {s.labels["horizon"] for s in samples if s.name == "kizashi_health_score"}
    assert "1h" in horizons


def test_compute_health_samples_service_label_matches():
    spec = ServicesSpec(services=[
        ServiceDef("My Service", [ComponentDef("r1", weight=1.0)])
    ])
    samples = compute_health_samples(spec, [])
    svc_labels = {s.labels["service"] for s in samples if s.name == "kizashi_health_score"}
    assert "My Service" in svc_labels


def test_compute_health_samples_multiple_services():
    spec = ServicesSpec(services=[
        ServiceDef("SvcA", [ComponentDef("r1", weight=1.0)]),
        ServiceDef("SvcB", [ComponentDef("r2", weight=1.0)]),
    ])
    samples = compute_health_samples(spec, [])
    names = {s.labels["service"] for s in samples if s.name == "kizashi_health_score"}
    assert names == {"SvcA", "SvcB"}


def test_compute_health_samples_score_in_range():
    spec = ServicesSpec(services=[
        ServiceDef("WAN", [ComponentDef("r1:9116", "Gi0/0", weight=1.0)])
    ])
    snapshot = [
        Sample("kizashi_anomaly_score", {"instance": "r1:9116", "ifName": "Gi0/0", "layer": "baseline"}, 5.0),
    ]
    samples = compute_health_samples(spec, snapshot)
    for s in samples:
        if s.name == "kizashi_health_score":
            assert 0.0 <= s.value <= 100.0
