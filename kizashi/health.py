"""P5 Service Health Score — aggregated health signal across network components.

Health score formula
--------------------
  score = 100 - deduction

  deduction = Σ_groups( group_weight × effective_norm_score ) / total_weight × 100

where:
  - ``effective_norm_score`` is the *minimum* normalised anomaly score in a
    redundancy group, so the group only penalises significantly when ALL members
    are degraded (redundant pair is healthy while at least one link is fine).
  - Non-redundant components form singleton groups (each a group by itself).
  - ``normalised_score = clamp(anomaly_score / MAX_ANOMALY_SCORE, 0.0, 1.0)``

Future health score (horizon = "1h")
--------------------------------------
Uses Layer 4 failure probability in place of the normalised anomaly score.
Requires P4 ``kizashi_failure_probability`` samples in the inference snapshot.

Outputs
-------
``kizashi_health_score{service, horizon}``
  horizon = ``"now"`` | ``"1h"``
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kizashi.exporter import Sample

logger = logging.getLogger(__name__)

MAX_ANOMALY_SCORE = 10.0   # kizashi_anomaly_score ceiling used for normalisation
DEGRADED_THRESHOLD = 3.0   # score above which a component is considered degraded


# ─────────────────────────── service definition schema ───────────────────────


class ServicesConfigError(ValueError):
    pass


@dataclass
class ComponentDef:
    instance: str
    if_name: str = ""           # "" = the device itself, no interface filter
    weight: float = 1.0         # relative importance; normalised inside compute_*
    redundancy_group: str = ""  # "" = non-redundant; same string = same group


@dataclass
class ServiceDef:
    name: str
    components: list[ComponentDef] = field(default_factory=list)


@dataclass
class ServicesSpec:
    services: list[ServiceDef] = field(default_factory=list)


def component_key(instance: str, if_name: str) -> str:
    """Canonical lookup key matching the label structure on Prometheus samples."""
    return f"{instance}:{if_name}" if if_name else instance


# ─────────────────────────── loading & validation ────────────────────────────


def _validate_component(raw: Any, svc_name: str, idx: int) -> ComponentDef:
    if not isinstance(raw, dict):
        raise ServicesConfigError(
            f"service {svc_name!r}: component[{idx}] must be a mapping"
        )
    if not raw.get("instance"):
        raise ServicesConfigError(
            f"service {svc_name!r}: component[{idx}]: 'instance' is required"
        )
    weight = float(raw.get("weight", 1.0))
    if weight < 0:
        raise ServicesConfigError(
            f"service {svc_name!r}: component[{idx}]: weight must be >= 0"
        )
    return ComponentDef(
        instance=str(raw["instance"]),
        if_name=str(raw.get("if_name", raw.get("ifName", ""))),
        weight=weight,
        redundancy_group=str(raw.get("redundancy_group", "")),
    )


def _validate_service(raw: Any, idx: int) -> ServiceDef:
    if not isinstance(raw, dict):
        raise ServicesConfigError(f"services[{idx}] must be a mapping")
    name = raw.get("name", "")
    if not name:
        raise ServicesConfigError(f"services[{idx}]: 'name' is required")
    components_raw = raw.get("components") or []
    if not components_raw:
        raise ServicesConfigError(f"service {name!r}: at least one component is required")
    components = [_validate_component(c, name, i) for i, c in enumerate(components_raw)]
    return ServiceDef(name=str(name), components=components)


def validate_services_spec(raw: Any) -> ServicesSpec:
    """Parse and validate a raw dict (from YAML) into ``ServicesSpec``.

    Raises ``ServicesConfigError`` with a descriptive message on any schema violation.
    """
    if not isinstance(raw, dict):
        raise ServicesConfigError("services file must be a YAML mapping with a 'services' key")
    services_raw = raw.get("services") or []
    if not isinstance(services_raw, list):
        raise ServicesConfigError("'services' must be a list")
    return ServicesSpec(
        services=[_validate_service(s, i) for i, s in enumerate(services_raw)]
    )


def load_services(path: str | Path) -> ServicesSpec:
    """Load and validate a service definition YAML file."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return validate_services_spec(raw)


# ─────────────────────────── health score computation ────────────────────────


def _normalize_anomaly(score: float) -> float:
    return max(0.0, min(1.0, score / MAX_ANOMALY_SCORE))


def _group_deduction(
    group_comps: list[ComponentDef],
    norm_scores: list[float],
    *,
    is_singleton: bool,
) -> float:
    """Compute the weighted deduction for one group.

    For redundant groups (multiple components, same redundancy_group), the
    effective score is ``min(norm_scores)``, capturing the intuition that the
    group is only fully penalised when ALL members are degraded.

    For singleton components the score is used directly.
    """
    group_weight = sum(c.weight for c in group_comps)
    effective = min(norm_scores) if not is_singleton else norm_scores[0]
    return group_weight * effective


def _deduction_from_snapshot(
    svc: ServiceDef,
    anomaly_scores: dict[str, float],
    failure_probs: dict[str, float] | None,
) -> tuple[float, float | None]:
    """Return (deduction_now, deduction_1h) for the service.

    Both values are in the same unit as the weights (unnormalised). The caller
    divides by total_weight × 100 to get the final health score deduction.
    """
    # Build groups: named redundancy groups + singleton groups (unique per component)
    by_group: dict[str, list[ComponentDef]] = {}
    for i, comp in enumerate(svc.components):
        gk = comp.redundancy_group if comp.redundancy_group else f"\x00{i}"
        by_group.setdefault(gk, []).append(comp)

    deduction_now = 0.0
    deduction_1h = 0.0 if failure_probs is not None else None

    for gk, group_comps in by_group.items():
        is_singleton = not group_comps[0].redundancy_group  # singletons have "" group
        keys = [component_key(c.instance, c.if_name) for c in group_comps]

        norm_now = [_normalize_anomaly(anomaly_scores.get(k, 0.0)) for k in keys]
        deduction_now += _group_deduction(group_comps, norm_now, is_singleton=is_singleton)

        if failure_probs is not None:
            # Failure probability is already in [0, 1]; treat it as normalised directly
            probs_1h = [max(0.0, min(1.0, failure_probs.get(k, 0.0))) for k in keys]
            deduction_1h += _group_deduction(group_comps, probs_1h, is_singleton=is_singleton)

    return deduction_now, deduction_1h


def compute_service_health(
    svc: ServiceDef,
    anomaly_scores: dict[str, float],
    failure_probs_1h: dict[str, float] | None = None,
) -> dict[str, float]:
    """Compute health scores for a single service.

    Parameters
    ----------
    svc : ServiceDef
    anomaly_scores : ``{component_key: anomaly_score}`` — from
        ``kizashi_anomaly_score`` samples.
    failure_probs_1h : ``{component_key: probability}`` — from
        ``kizashi_failure_probability{horizon="1h"}`` samples. Optional.

    Returns
    -------
    dict with key ``"now"`` (always present) and ``"1h"`` (when
    ``failure_probs_1h`` is supplied).
    """
    total_weight = sum(c.weight for c in svc.components)
    if total_weight <= 0:
        result: dict[str, float] = {"now": 100.0}
        if failure_probs_1h is not None:
            result["1h"] = 100.0
        return result

    deduction_now, deduction_1h = _deduction_from_snapshot(svc, anomaly_scores, failure_probs_1h)

    score_now = max(0.0, min(100.0, 100.0 - (deduction_now / total_weight) * 100.0))
    result = {"now": round(score_now, 4)}
    if deduction_1h is not None:
        score_1h = max(0.0, min(100.0, 100.0 - (deduction_1h / total_weight) * 100.0))
        result["1h"] = round(score_1h, 4)
    return result


def _extract_anomaly_scores(snapshot: list[Sample]) -> dict[str, float]:
    """Extract ``{component_key: max_anomaly_score}`` from an inference snapshot.

    Takes the maximum score across all anomaly layers for each (instance, ifName).
    """
    scores: dict[str, float] = {}
    for s in snapshot:
        if s.name != "kizashi_anomaly_score":
            continue
        inst = s.labels.get("instance", "")
        if_name = s.labels.get("ifName", s.labels.get("if_name", ""))
        key = component_key(inst, if_name)
        if key and s.value > scores.get(key, 0.0):
            scores[key] = s.value
    return scores


def _extract_failure_probs(
    snapshot: list[Sample],
    horizon: str = "1h",
) -> dict[str, float]:
    """Extract ``{component_key: max_failure_probability}`` for the given horizon."""
    probs: dict[str, float] = {}
    for s in snapshot:
        if s.name != "kizashi_failure_probability":
            continue
        if s.labels.get("horizon") != horizon:
            continue
        inst = s.labels.get("instance", "")
        if_name = s.labels.get("ifName", s.labels.get("if_name", ""))
        key = component_key(inst, if_name)
        if key and s.value > probs.get(key, 0.0):
            probs[key] = s.value
    return probs


def compute_health_samples(
    spec: ServicesSpec,
    snapshot: list[Sample],
    failure_horizon: str = "1h",
) -> list[Sample]:
    """Compute ``kizashi_health_score`` samples from the current inference snapshot.

    Parameters
    ----------
    spec : ServicesSpec loaded from the services definition YAML.
    snapshot : Current inference snapshot from ``SnapshotCollector.samples()``.
    failure_horizon : Horizon label to use for Layer 4 failure probabilities.

    Returns
    -------
    List of ``kizashi_health_score{service, horizon}`` samples ready for
    publication via ``SnapshotCollector.publish()``.
    """
    anomaly_scores = _extract_anomaly_scores(snapshot)
    failure_probs = _extract_failure_probs(snapshot, horizon=failure_horizon)
    use_failure = bool(failure_probs)

    samples: list[Sample] = []
    for svc in spec.services:
        scores = compute_service_health(
            svc,
            anomaly_scores,
            failure_probs_1h=failure_probs if use_failure else None,
        )
        for horizon_label, value in scores.items():
            samples.append(
                Sample(
                    "kizashi_health_score",
                    {"service": svc.name, "horizon": horizon_label},
                    value,
                )
            )
        logger.debug(
            "health: service=%r now=%.1f%s",
            svc.name,
            scores["now"],
            f" 1h={scores['1h']:.1f}" if "1h" in scores else "",
        )
    return samples
