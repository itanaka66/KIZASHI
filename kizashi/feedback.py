"""P3 Feedback API — FastAPI server.

Endpoints
---------
POST /api/v1/webhook/alertmanager
    Receive Alertmanager webhook payloads.
    - KIZASHI predictive alerts  → stored as LABEL_PENDING.
    - Confirmed failure alerts   → stored as LABEL_CONFIRMED_FAILURE.
    - Resolved alerts            → append LABEL_RESOLVED record.

POST /api/v1/alerts/{fingerprint}/feedback
    Operator judgment: true_positive / false_positive / resolved.

GET  /api/v1/alerts
    List recent alert records.

GET  /api/v1/labels
    Export finalized labels for P4 training data.

Authentication
--------------
Static bearer token set via the environment variable named in
``FeedbackConfig.token_env`` (default: ``KIZASHI_FEEDBACK_TOKEN``).
Pass ``Authorization: Bearer <token>`` with every request.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from kizashi.label_store import (
    LABEL_CONFIRMED_FAILURE,
    LABEL_FALSE_POSITIVE,
    LABEL_PENDING,
    LABEL_RESOLVED,
    LABEL_TRUE_POSITIVE,
    _VALID_OPERATOR_LABELS,
    FeedbackRecord,
    LabelStore,
)

logger = logging.getLogger(__name__)

_security = HTTPBearer(auto_error=True)

# ─────────────────────────────── Pydantic models ──────────────────────────────


class _AmAlert(BaseModel):
    status: str
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str = ""
    endsAt: str = ""
    fingerprint: str = ""


class AlertmanagerWebhook(BaseModel):
    version: str = "4"
    status: str = "firing"
    receiver: str = ""
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    externalURL: str = ""
    alerts: list[_AmAlert] = Field(default_factory=list)


class FeedbackRequest(BaseModel):
    label: str = Field(..., description="true_positive | false_positive | resolved")
    notes: str = ""
    resolution: str = ""


class FeedbackResponse(BaseModel):
    fingerprint: str
    label: str
    labeled_at: float


# ───────────────────────────────── helpers ────────────────────────────────────


def _parse_iso(s: str) -> float:
    """Parse an ISO-8601 timestamp string to unix seconds (returns 0 on failure)."""
    if not s or s.startswith("0001"):
        return 0.0
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp()
    except ValueError:
        return 0.0


def _extract_fields(labels: dict[str, str]) -> tuple[str, str, str]:
    """Return (instance, if_name, failure_mode) from alert labels."""
    instance = labels.get("instance", labels.get("exported_instance", ""))
    if_name = labels.get("ifName", labels.get("if_name", ""))
    failure_mode = labels.get("failure_mode", labels.get("alertname", "unknown"))
    return instance, if_name, failure_mode


# ─────────────────────────────── app factory ──────────────────────────────────


def create_app(
    store: LabelStore,
    token: str,
    confirmed_failure_alert_names: frozenset[str] | None = None,
) -> FastAPI:
    """Create and return the FastAPI application.

    Parameters
    ----------
    store : LabelStore instance (injected for testability).
    token : static bearer token.
    confirmed_failure_alert_names : alert names treated as confirmed failures
        when received via Alertmanager webhook.
    """
    if confirmed_failure_alert_names is None:
        confirmed_failure_alert_names = frozenset(
            ["InterfaceDown", "DeviceUnreachable", "NodeDown", "LinkDown"]
        )

    app = FastAPI(
        title="KIZASHI Feedback API",
        description="P3 feedback loop: operator judgments and confirmed failure labels.",
        version="0.1.0",
    )

    # ── auth dependency ───────────────────────────────────────────────────────

    # NOTE: from __future__ import annotations turns all annotations into lazy strings,
    # so Annotated[str, Depends(...)] aliases defined in this closure cannot be resolved
    # by FastAPI's get_type_hints(). Use Depends(...) as a default value instead.

    async def _verify(
        creds: Annotated[HTTPAuthorizationCredentials, Security(_security)],
    ) -> str:
        if creds.credentials != token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid bearer token",
            )
        return creds.credentials

    _auth_dep = Depends(_verify)

    # ── routes ────────────────────────────────────────────────────────────────

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    @app.post(
        "/api/v1/webhook/alertmanager",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Receive Alertmanager webhook",
    )
    async def webhook_alertmanager(payload: AlertmanagerWebhook, _token: str = _auth_dep) -> None:
        """Ingest Alertmanager firing/resolved events.

        - KIZASHI predictive alerts  (``kizashi="predictive"``) → pending.
        - Confirmed failure alerts   (``InterfaceDown`` etc.)   → confirmed_failure.
        - ``status="resolved"``      → append resolved record.
        """
        now = time.time()
        for alert in payload.alerts:
            labels = alert.labels
            fingerprint = alert.fingerprint or f"fp-{hash(str(sorted(labels.items())))}"
            alert_name = labels.get("alertname", "")
            instance, if_name, failure_mode = _extract_fields(labels)
            started_at = _parse_iso(alert.startsAt) or now

            if alert.status == "resolved":
                record = FeedbackRecord(
                    fingerprint=fingerprint,
                    alert_name=alert_name,
                    instance=instance,
                    if_name=if_name,
                    failure_mode=failure_mode,
                    started_at=started_at,
                    label=LABEL_RESOLVED,
                    labeled_at=now,
                    labeled_by="alertmanager",
                    alert_labels=dict(labels),
                )
            elif labels.get("kizashi") == "predictive":
                record = FeedbackRecord(
                    fingerprint=fingerprint,
                    alert_name=alert_name,
                    instance=instance,
                    if_name=if_name,
                    failure_mode=failure_mode,
                    started_at=started_at,
                    label=LABEL_PENDING,
                    labeled_at=now,
                    labeled_by="alertmanager",
                    alert_labels=dict(labels),
                )
            elif alert_name in confirmed_failure_alert_names:
                record = FeedbackRecord(
                    fingerprint=fingerprint,
                    alert_name=alert_name,
                    instance=instance,
                    if_name=if_name,
                    failure_mode=alert_name,
                    started_at=started_at,
                    label=LABEL_CONFIRMED_FAILURE,
                    labeled_at=now,
                    labeled_by="alertmanager",
                    alert_labels=dict(labels),
                )
            else:
                continue

            store.append(record)
            logger.info(
                "webhook: %s fingerprint=%s instance=%s label=%s",
                alert_name,
                fingerprint,
                instance,
                record.label,
            )

    @app.post(
        "/api/v1/alerts/{fingerprint}/feedback",
        response_model=FeedbackResponse,
        summary="Submit operator feedback for a predictive alert",
    )
    async def post_feedback(
        fingerprint: str,
        body: FeedbackRequest,
        _token: str = _auth_dep,
    ) -> FeedbackResponse:
        """Record an operator's judgment for a KIZASHI predictive alert.

        ``label`` must be one of: ``true_positive``, ``false_positive``, ``resolved``.
        """
        if body.label not in _VALID_OPERATOR_LABELS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"label must be one of {sorted(_VALID_OPERATOR_LABELS)}",
            )
        # Find original alert record to copy its metadata
        originals = store.get_by_fingerprint(fingerprint)
        if not originals:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no alert with fingerprint={fingerprint!r}",
            )
        orig = originals[0]
        now = time.time()
        record = FeedbackRecord(
            fingerprint=fingerprint,
            alert_name=orig.alert_name,
            instance=orig.instance,
            if_name=orig.if_name,
            failure_mode=orig.failure_mode,
            started_at=orig.started_at,
            label=body.label,
            labeled_at=now,
            labeled_by="operator",
            alert_labels=orig.alert_labels,
            notes=body.notes,
            resolution=body.resolution,
        )
        store.append(record)
        logger.info(
            "feedback: fingerprint=%s label=%s by=operator notes=%r",
            fingerprint,
            body.label,
            body.notes,
        )
        return FeedbackResponse(
            fingerprint=fingerprint, label=body.label, labeled_at=now
        )

    @app.get(
        "/api/v1/alerts",
        summary="List recent alert records",
    )
    async def list_alerts(
        _token: str = _auth_dep,
        since: float | None = None,
        limit: int = 100,
    ) -> dict:
        """Return recent label records.

        ``since`` filters by ``labeled_at`` unix timestamp.
        ``limit`` caps the number of records returned (newest first).
        """
        records = store.list_since(since) if since else store.list_all()
        records = sorted(records, key=lambda r: r.labeled_at, reverse=True)[:limit]
        return {"count": len(records), "records": [r.to_dict() for r in records]}

    @app.get(
        "/api/v1/labels",
        summary="Export finalized labels for P4 training",
    )
    async def export_labels(_token: str = _auth_dep, since: float | None = None) -> dict:
        """Return the latest finalized label per fingerprint.

        Only includes ``true_positive``, ``false_positive``, and
        ``confirmed_failure`` records — pending and intermediate states
        are excluded.
        """
        records = store.training_labels(since=since)
        return {"count": len(records), "records": [r.to_dict() for r in records]}

    return app


# ─────────────────────────────── module-level app ─────────────────────────────


def _default_token() -> str:
    """Load the feedback API token from the environment at import time (if available)."""
    return os.environ.get("KIZASHI_FEEDBACK_TOKEN", "")


def build_from_config(config) -> FastAPI:
    """Build the FastAPI app from a ``Config`` with a ``feedback`` section.

    Called by ``kizashi feedback`` CLI command.
    """
    from pathlib import Path  # noqa: PLC0415

    fb = config.feedback
    token = os.environ.get(fb.token_env, "")
    if not token:
        raise RuntimeError(
            f"Feedback API token not set. "
            f"Set the environment variable {fb.token_env!r}."
        )
    store = LabelStore(Path(fb.label_store_path) / "labels.jsonl")
    return create_app(
        store,
        token,
        frozenset(fb.confirmed_failure_alert_names),
    )
