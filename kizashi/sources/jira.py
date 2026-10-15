"""Jira Service Management REST API v3 client."""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_SEARCH_JQL = (
    'project = "{project}" AND labels = "kizashi-alert-id:{alert_id}" '
    'AND resolution = Unresolved ORDER BY created DESC'
)


class JiraError(Exception):
    pass


@dataclass
class JiraIssue:
    key: str
    summary: str
    status: str


class JiraClient:
    def __init__(self, url: str, email: str, api_token: str, project_key: str, issue_type: str = "Incident") -> None:
        self._base = url.rstrip("/")
        self._project = project_key
        self._issue_type = issue_type
        _token = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self._auth_header = f"Basic {_token}"

    @classmethod
    def from_config(cls, cfg: "JiraConfig") -> "JiraClient":
        token = os.environ.get(cfg.api_token_env, "")
        if not token:
            raise JiraError(f"environment variable {cfg.api_token_env!r} is not set")
        return cls(cfg.url, cfg.email, token, cfg.project_key, cfg.issue_type)

    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        url = f"{self._base}/rest/api/3{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": self._auth_header,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise JiraError(f"Jira {method} {path} → HTTP {exc.code}: {detail}") from exc

    def find_open_issue(self, alert_id: str) -> JiraIssue | None:
        jql = _SEARCH_JQL.format(project=self._project, alert_id=alert_id)
        result = self._request("GET", f"/issue/search?jql={urllib.parse.quote(jql)}&maxResults=1")
        issues = result.get("issues", [])
        if not issues:
            return None
        i = issues[0]
        return JiraIssue(
            key=i["key"],
            summary=i["fields"]["summary"],
            status=i["fields"]["status"]["name"],
        )

    def create_issue(
        self,
        summary: str,
        description_adf: list[dict],
        labels: list[str],
        priority: str = "Medium",
    ) -> JiraIssue:
        body = {
            "fields": {
                "project": {"key": self._project},
                "issuetype": {"name": self._issue_type},
                "summary": summary,
                "description": {"type": "doc", "version": 1, "content": description_adf},
                "labels": labels,
                "priority": {"name": priority},
            }
        }
        result = self._request("POST", "/issue", body)
        return JiraIssue(key=result["key"], summary=summary, status="Open")

    def add_comment(self, key: str, text: str) -> None:
        adf = [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]
        self._request("POST", f"/issue/{key}/comment", {"body": {"type": "doc", "version": 1, "content": adf}})

    def transition_to_resolved(self, key: str, resolution: str = "Fixed") -> None:
        transitions = self._request("GET", f"/issue/{key}/transitions")
        target_id = None
        for t in transitions.get("transitions", []):
            if t.get("name", "").lower() in ("done", "resolved", "closed"):
                target_id = t["id"]
                break
        if target_id is None:
            logger.warning("jira: no 'Done/Resolved' transition found for %s", key)
            return
        self._request(
            "POST",
            f"/issue/{key}/transitions",
            {"transition": {"id": target_id}, "fields": {"resolution": {"name": resolution}}},
        )


def _adf_paragraph(text: str) -> dict:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def _adf_heading(text: str, level: int = 3) -> dict:
    return {"type": "heading", "attrs": {"level": level}, "content": [{"type": "text", "text": text}]}


def _adf_bullet_list(items: list[str]) -> dict:
    return {
        "type": "bulletList",
        "content": [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": item}]}]}
            for item in items
        ],
    }


def build_issue_fields(
    instance: str,
    if_name: str,
    failure_mode: str,
    anomaly_score: float,
    time_to_threshold_h: float,
    grafana_url: str = "",
    llm_explanation: str = "",
) -> tuple[str, list[dict], list[str], str]:
    """Return (summary, description_adf, labels, priority) for a new Jira issue."""
    eta_label = f"{time_to_threshold_h:.0f}h" if time_to_threshold_h < 999 else "不明"
    summary = f"[KIZASHI] {instance} / {if_name}: {failure_mode} 予兆 (ETA: {eta_label})"

    priority = "High" if time_to_threshold_h < 24 else "Medium"

    desc: list[dict] = [
        _adf_heading("KIZASHI 予兆アラート"),
        _adf_paragraph(f"機器: {instance}"),
        _adf_paragraph(f"インターフェース: {if_name}"),
        _adf_paragraph(f"故障モード: {failure_mode}"),
        _adf_paragraph(f"異常スコア: {anomaly_score:.1f} / 10.0"),
        _adf_paragraph(f"しきい値到達予測: {eta_label} 後"),
    ]
    if grafana_url:
        desc.append(_adf_paragraph(f"Grafana: {grafana_url}"))
    if llm_explanation:
        desc.append(_adf_heading("AI 説明（参考）"))
        desc.append(_adf_paragraph(llm_explanation))

    labels = ["kizashi", "predictive", failure_mode, f"kizashi-alert-id:{instance}:{if_name}:{failure_mode}"]
    return summary, desc, labels, priority


def open_or_create_issue(
    client: JiraClient,
    instance: str,
    if_name: str,
    failure_mode: str,
    anomaly_score: float,
    time_to_threshold_h: float,
    grafana_url: str = "",
    llm_explanation: str = "",
) -> JiraIssue:
    """Open a new Jira issue, or return the existing unresolved one (dedup by alert-id label)."""
    alert_id = f"{instance}:{if_name}:{failure_mode}"
    existing = client.find_open_issue(alert_id)
    if existing:
        logger.debug("jira: existing issue %s for %s", existing.key, alert_id)
        return existing

    summary, desc, labels, priority = build_issue_fields(
        instance, if_name, failure_mode, anomaly_score, time_to_threshold_h, grafana_url, llm_explanation
    )
    issue = client.create_issue(summary, desc, labels, priority)
    logger.info("jira: created %s — %s", issue.key, summary)
    return issue


# urllib.parse is used inside find_open_issue
import urllib.parse  # noqa: E402 (module-level import at bottom is intentional)
