"""Tests for kizashi/feedback.py (P3 Feedback API)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient

from kizashi.feedback import create_app
from kizashi.label_store import (
    LABEL_CONFIRMED_FAILURE,
    LABEL_FALSE_POSITIVE,
    LABEL_PENDING,
    LABEL_RESOLVED,
    LABEL_TRUE_POSITIVE,
    FeedbackRecord,
    LabelStore,
)

# ─────────────────────────────── fixtures ─────────────────────────────────────

TOKEN = "test-secret-token"


@pytest.fixture
def store(tmp_path: Path) -> LabelStore:
    return LabelStore(tmp_path / "labels.jsonl")


@pytest.fixture
def client(store: LabelStore) -> TestClient:
    app = create_app(store, TOKEN)
    return TestClient(app, raise_server_exceptions=True)


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ─────────────────────────────── auth tests ───────────────────────────────────


class TestAuth:
    def test_no_token_returns_401_or_403(self, client: TestClient):
        r = client.get("/api/v1/alerts")
        assert r.status_code in (401, 403)

    def test_wrong_token_returns_401(self, client: TestClient):
        r = client.get("/api/v1/alerts", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_correct_token_accepted(self, client: TestClient):
        r = client.get("/api/v1/alerts", headers=_auth())
        assert r.status_code == 200

    def test_healthz_no_auth_needed(self, client: TestClient):
        r = client.get("/healthz")
        assert r.status_code == 200


# ────────────────────────── webhook endpoint tests ───────────────────────────


def _predictive_payload(
    status: str = "firing",
    fingerprint: str = "fp1",
    alert_name: str = "OpticDegradationPredicted",
    instance: str = "sw1:9116",
    if_name: str = "Gi0/0",
    failure_mode: str = "rx_low",
) -> dict:
    return {
        "version": "4",
        "status": status,
        "receiver": "kizashi-feedback",
        "groupLabels": {},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
        "alerts": [
            {
                "status": status,
                "labels": {
                    "alertname": alert_name,
                    "kizashi": "predictive",
                    "instance": instance,
                    "ifName": if_name,
                    "failure_mode": failure_mode,
                },
                "annotations": {},
                "startsAt": "2026-01-01T00:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "fingerprint": fingerprint,
            }
        ],
    }


class TestAlertmanagerWebhook:
    def test_predictive_firing_creates_pending_record(
        self, client: TestClient, store: LabelStore
    ):
        r = client.post(
            "/api/v1/webhook/alertmanager",
            json=_predictive_payload(),
            headers=_auth(),
        )
        assert r.status_code == 204
        records = store.get_by_fingerprint("fp1")
        assert len(records) == 1
        assert records[0].label == LABEL_PENDING
        assert records[0].labeled_by == "alertmanager"
        assert records[0].instance == "sw1:9116"
        assert records[0].if_name == "Gi0/0"
        assert records[0].failure_mode == "rx_low"

    def test_resolved_creates_resolved_record(
        self, client: TestClient, store: LabelStore
    ):
        # First fire
        client.post(
            "/api/v1/webhook/alertmanager",
            json=_predictive_payload(status="firing"),
            headers=_auth(),
        )
        # Then resolve
        r = client.post(
            "/api/v1/webhook/alertmanager",
            json=_predictive_payload(status="resolved"),
            headers=_auth(),
        )
        assert r.status_code == 204
        records = store.get_by_fingerprint("fp1")
        assert len(records) == 2
        assert records[-1].label == LABEL_RESOLVED

    def test_confirmed_failure_alert_stored(
        self, client: TestClient, store: LabelStore
    ):
        payload = {
            "version": "4",
            "status": "firing",
            "receiver": "kizashi-feedback",
            "groupLabels": {},
            "commonLabels": {},
            "commonAnnotations": {},
            "externalURL": "",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "InterfaceDown",
                        "instance": "sw2:9116",
                        "ifName": "Gi0/1",
                    },
                    "annotations": {},
                    "startsAt": "2026-01-01T01:00:00Z",
                    "endsAt": "0001-01-01T00:00:00Z",
                    "fingerprint": "fp-ifd",
                }
            ],
        }
        r = client.post(
            "/api/v1/webhook/alertmanager", json=payload, headers=_auth()
        )
        assert r.status_code == 204
        records = store.get_by_fingerprint("fp-ifd")
        assert records[0].label == LABEL_CONFIRMED_FAILURE
        assert records[0].failure_mode == "InterfaceDown"

    def test_unknown_alert_not_stored(
        self, client: TestClient, store: LabelStore
    ):
        payload = {
            "version": "4",
            "status": "firing",
            "receiver": "other",
            "groupLabels": {},
            "commonLabels": {},
            "commonAnnotations": {},
            "externalURL": "",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "HighCPU", "instance": "sw3"},
                    "annotations": {},
                    "startsAt": "2026-01-01T00:00:00Z",
                    "endsAt": "0001-01-01T00:00:00Z",
                    "fingerprint": "fp-cpu",
                }
            ],
        }
        r = client.post(
            "/api/v1/webhook/alertmanager", json=payload, headers=_auth()
        )
        assert r.status_code == 204
        assert store.get_by_fingerprint("fp-cpu") == []

    def test_empty_alerts_list_ok(self, client: TestClient):
        payload = {
            "version": "4",
            "status": "firing",
            "receiver": "r",
            "groupLabels": {},
            "commonLabels": {},
            "commonAnnotations": {},
            "externalURL": "",
            "alerts": [],
        }
        r = client.post(
            "/api/v1/webhook/alertmanager", json=payload, headers=_auth()
        )
        assert r.status_code == 204


# ────────────────────────── feedback endpoint tests ──────────────────────────


class TestPostFeedback:
    def _seed_pending(self, client: TestClient, fingerprint: str = "fp1") -> None:
        client.post(
            "/api/v1/webhook/alertmanager",
            json=_predictive_payload(fingerprint=fingerprint),
            headers=_auth(),
        )

    def test_true_positive_accepted(
        self, client: TestClient, store: LabelStore
    ):
        self._seed_pending(client)
        r = client.post(
            "/api/v1/alerts/fp1/feedback",
            json={"label": LABEL_TRUE_POSITIVE, "notes": "optic replaced"},
            headers=_auth(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["fingerprint"] == "fp1"
        assert body["label"] == LABEL_TRUE_POSITIVE

    def test_false_positive_accepted(
        self, client: TestClient, store: LabelStore
    ):
        self._seed_pending(client)
        r = client.post(
            "/api/v1/alerts/fp1/feedback",
            json={"label": LABEL_FALSE_POSITIVE},
            headers=_auth(),
        )
        assert r.status_code == 200

    def test_resolved_accepted(self, client: TestClient):
        self._seed_pending(client)
        r = client.post(
            "/api/v1/alerts/fp1/feedback",
            json={"label": LABEL_RESOLVED},
            headers=_auth(),
        )
        assert r.status_code == 200

    def test_invalid_label_rejected(self, client: TestClient):
        self._seed_pending(client)
        r = client.post(
            "/api/v1/alerts/fp1/feedback",
            json={"label": "maybe"},
            headers=_auth(),
        )
        assert r.status_code == 422

    def test_unknown_fingerprint_returns_404(self, client: TestClient):
        r = client.post(
            "/api/v1/alerts/no-such-fp/feedback",
            json={"label": LABEL_TRUE_POSITIVE},
            headers=_auth(),
        )
        assert r.status_code == 404

    def test_record_persisted_to_store(
        self, client: TestClient, store: LabelStore
    ):
        self._seed_pending(client)
        client.post(
            "/api/v1/alerts/fp1/feedback",
            json={"label": LABEL_TRUE_POSITIVE, "resolution": "cable_replaced"},
            headers=_auth(),
        )
        records = store.get_by_fingerprint("fp1")
        # pending + feedback
        assert len(records) == 2
        assert records[-1].label == LABEL_TRUE_POSITIVE
        assert records[-1].labeled_by == "operator"
        assert records[-1].resolution == "cable_replaced"

    def test_metadata_copied_from_original(
        self, client: TestClient, store: LabelStore
    ):
        self._seed_pending(client, fingerprint="fp2")
        client.post(
            "/api/v1/alerts/fp2/feedback",
            json={"label": LABEL_TRUE_POSITIVE},
            headers=_auth(),
        )
        latest = store.get_by_fingerprint("fp2")[-1]
        assert latest.instance == "sw1:9116"
        assert latest.if_name == "Gi0/0"
        assert latest.failure_mode == "rx_low"


# ────────────────────────── list / labels tests ──────────────────────────────


class TestListAlerts:
    def test_empty_returns_empty_list(self, client: TestClient):
        r = client.get("/api/v1/alerts", headers=_auth())
        assert r.status_code == 200
        assert r.json()["count"] == 0

    def test_returns_ingested_records(self, client: TestClient):
        client.post(
            "/api/v1/webhook/alertmanager",
            json=_predictive_payload(fingerprint="fp-a"),
            headers=_auth(),
        )
        r = client.get("/api/v1/alerts", headers=_auth())
        assert r.json()["count"] == 1

    def test_limit_respected(self, client: TestClient):
        for i in range(5):
            client.post(
                "/api/v1/webhook/alertmanager",
                json=_predictive_payload(fingerprint=f"fp-{i}"),
                headers=_auth(),
            )
        r = client.get("/api/v1/alerts?limit=3", headers=_auth())
        assert r.json()["count"] == 3

    def test_since_filter(self, client: TestClient, store: LabelStore):
        early = FeedbackRecord(
            fingerprint="early",
            alert_name="X",
            instance="sw1",
            if_name="Gi0/0",
            failure_mode="rx_low",
            started_at=1000.0,
            label=LABEL_PENDING,
            labeled_at=1000.0,
            labeled_by="alertmanager",
        )
        late = FeedbackRecord(
            fingerprint="late",
            alert_name="X",
            instance="sw1",
            if_name="Gi0/0",
            failure_mode="rx_low",
            started_at=3000.0,
            label=LABEL_PENDING,
            labeled_at=3000.0,
            labeled_by="alertmanager",
        )
        store.append(early)
        store.append(late)
        r = client.get("/api/v1/alerts?since=2000", headers=_auth())
        result = r.json()
        assert result["count"] == 1
        assert result["records"][0]["fingerprint"] == "late"


class TestExportLabels:
    def test_pending_excluded_from_training_export(
        self, client: TestClient, store: LabelStore
    ):
        client.post(
            "/api/v1/webhook/alertmanager",
            json=_predictive_payload(fingerprint="fp-pending"),
            headers=_auth(),
        )
        r = client.get("/api/v1/labels", headers=_auth())
        assert r.json()["count"] == 0

    def test_true_positive_included_in_export(
        self, client: TestClient, store: LabelStore
    ):
        client.post(
            "/api/v1/webhook/alertmanager",
            json=_predictive_payload(fingerprint="fp-tp"),
            headers=_auth(),
        )
        client.post(
            "/api/v1/alerts/fp-tp/feedback",
            json={"label": LABEL_TRUE_POSITIVE},
            headers=_auth(),
        )
        r = client.get("/api/v1/labels", headers=_auth())
        assert r.json()["count"] == 1
        assert r.json()["records"][0]["label"] == LABEL_TRUE_POSITIVE

    def test_confirmed_failure_included_in_export(
        self, client: TestClient, store: LabelStore
    ):
        payload = {
            "version": "4",
            "status": "firing",
            "receiver": "kf",
            "groupLabels": {},
            "commonLabels": {},
            "commonAnnotations": {},
            "externalURL": "",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "DeviceUnreachable", "instance": "sw5"},
                    "annotations": {},
                    "startsAt": "2026-01-01T00:00:00Z",
                    "endsAt": "0001-01-01T00:00:00Z",
                    "fingerprint": "fp-cf",
                }
            ],
        }
        client.post("/api/v1/webhook/alertmanager", json=payload, headers=_auth())
        r = client.get("/api/v1/labels", headers=_auth())
        assert r.json()["count"] == 1
        assert r.json()["records"][0]["label"] == LABEL_CONFIRMED_FAILURE

    def test_latest_label_wins_for_same_fingerprint(
        self, client: TestClient, store: LabelStore
    ):
        client.post(
            "/api/v1/webhook/alertmanager",
            json=_predictive_payload(fingerprint="fp-dup"),
            headers=_auth(),
        )
        # First judge false positive…
        client.post(
            "/api/v1/alerts/fp-dup/feedback",
            json={"label": LABEL_FALSE_POSITIVE},
            headers=_auth(),
        )
        # …then operator reconsiders and marks true positive
        client.post(
            "/api/v1/alerts/fp-dup/feedback",
            json={"label": LABEL_TRUE_POSITIVE},
            headers=_auth(),
        )
        r = client.get("/api/v1/labels", headers=_auth())
        assert r.json()["count"] == 1
        assert r.json()["records"][0]["label"] == LABEL_TRUE_POSITIVE
