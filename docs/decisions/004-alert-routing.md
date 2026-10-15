# ADR-004: 予兆アラートの通知先と対応フロー

**ステータス**: 決定済み  
**決定日**: 2026-10-15

---

## 背景

KIZASHI が生成する予兆アラートは通常の障害アラートと性質が異なる（「今すぐ壊れている」ではなく「近いうちに壊れる」）。誤報が多いと無視されるため、通知先・エスカレーション・対応 SLA を明確に定義する必要がある。

---

## 通知先と重大度マッピング

| 条件 | Severity | 通知先 | SLA |
|---|---|---|---|
| しきい値到達 > 72h | `warning` | Slack `#network-ops` + Jira 自動起票 | 翌営業日中に確認 |
| しきい値到達 24〜72h | `warning` | Slack `#network-ops` + Jira 自動起票 + メール | 4 時間以内に確認 |
| しきい値到達 < 24h | `critical` | Slack `#network-oncall` + Jira 起票 + PagerDuty | 2 時間以内に対応開始 |
| 異常スコア > 6（15m 継続） | `info` | Slack `#network-monitoring` のみ | 翌営業日 |

---

## 対応フロー

```
KIZASHI → Prometheus alert → Alertmanager
                                │
              ┌─────────────────┼──────────────────┐
              ▼                 ▼                  ▼
        Slack 通知         Jira 自動起票       PagerDuty
         (#network-ops)    (kizashi/jira.py)   (critical のみ)
              │                 │
              ▼                 ▼
        オペレーター確認    担当者アサイン
              │
    ┌─────────┴──────────┐
    │                    │
  正解（予兆確認）      誤報
    │                    │
  保守計画 + 部品手配   Feedback API → 誤報ラベル記録
    │                    │
  対処後: Feedback API   次回学習でハードネガティブに
  「対応済み」記録
```

---

## Alertmanager ルーティング設定

詳細は `deploy/alertmanager/routing.yml` 参照。要点:

- `kizashi="predictive"` ラベルを持つアラートを専用ルートで受け取る
- `inhibit_rules` で上流機器に predictive alert がある場合、配下の alert を抑制（設計書 §8.2）
- メンテナンス中（Alertmanager silence 期間）は `kizashi` アラートも発報しない

---

## Jira チケットのフィールドマッピング

| Jira フィールド | 値 |
|---|---|
| Summary | `[KIZASHI] {instance} / {ifName}: {failure_mode} 予兆 (ETA: {eta})` |
| Priority | critical → High、warning/72h+ → Medium |
| Labels | `kizashi`, `predictive`, `{failure_mode}` |
| Description | Grafana リンク + 異常スコア + トレンドグラフ URL |
| カスタムフィールド | `instance`, `ifName`, `anomaly_score`, `time_to_threshold_hours` |

詳細実装は `kizashi/sources/jira.py` 参照。

---

## フィードバックループ

オペレーターが Jira チケットの Resolution を設定した際:
- `Fixed` / `Done` → 「対応済み」として Feedback API に POST（保守交換後に故障が起きない正例扱い）
- `Won't Fix` / `Invalid` → 「誤報」として POST → 次回学習のハードネガティブに使用

Jira の Automation で Webhook を設定し、チケット解決時に KIZASHI Feedback API を呼ぶ。

---

## 参照

- `deploy/alertmanager/routing.yml` — 実際の Alertmanager 設定
- `kizashi/sources/jira.py` — Jira SM クライアント実装
- `rules/kizashi-alerts.yml` — アラートルール（`kizashi="predictive"` ラベル付き）

---

<details>
<summary>English</summary>

# ADR-004: Predictive Alert Routing and Response Workflow

**Status**: Decided  
**Date**: 2026-10-15

## Background

KIZASHI predictive alerts differ from outage alerts ("will fail soon" vs. "failing now"). Because false positives lead to alert fatigue, notification destinations, escalation paths, and SLAs must be clearly defined.

## Notification Routing by Severity

| Condition | Severity | Destination | SLA |
|---|---|---|---|
| Time-to-threshold > 72 h | `warning` | Slack `#network-ops` + auto Jira ticket | Acknowledge by next business day |
| Time-to-threshold 24–72 h | `warning` | Slack `#network-ops` + Jira + email | Acknowledge within 4 hours |
| Time-to-threshold < 24 h | `critical` | Slack `#network-oncall` + Jira + PagerDuty | Begin response within 2 hours |
| Anomaly score > 6 (15m) | `info` | Slack `#network-monitoring` only | Next business day |

## Response Workflow

```
KIZASHI → Prometheus alert → Alertmanager
                                │
              ┌─────────────────┼──────────────────┐
              ▼                 ▼                  ▼
        Slack notification  Auto Jira ticket   PagerDuty
         (#network-ops)    (kizashi/jira.py)   (critical only)
              │                 │
              ▼                 ▼
       Operator review    Assignee allocation
              │
    ┌─────────┴──────────┐
    │                    │
  True positive        False positive
    │                    │
  Plan maintenance     Feedback API → false-positive label
  + order parts         used as hard negative in next training
    │
  Post-fix: Feedback API
  "resolved" record
```

## Alertmanager Routing Key Points

- Alerts with `kizashi="predictive"` are handled by a dedicated route.
- `inhibit_rules`: suppress downstream device alerts when an upstream device has a predictive alert (`equal: [parent_instance]`, from `kizashi_topology_parent`).
- During Alertmanager silence windows, KIZASHI alerts are also suppressed.

For the full configuration see `deploy/alertmanager/routing.yml`.

## Jira Ticket Field Mapping

| Jira field | Value |
|---|---|
| Summary | `[KIZASHI] {instance} / {ifName}: {failure_mode} prediction (ETA: {eta})` |
| Priority | critical → High; warning/72h+ → Medium |
| Labels | `kizashi`, `predictive`, `{failure_mode}` |
| Description | Grafana link + anomaly score + trend graph URL |
| Custom fields | `instance`, `ifName`, `anomaly_score`, `time_to_threshold_hours` |

## Feedback Loop via Jira Resolution

When an operator sets the ticket Resolution:
- `Fixed` / `Done` → POST "resolved" to Feedback API (treated as a positive example; failure did not occur because of proactive maintenance)
- `Won't Fix` / `Invalid` → POST "false positive" → used as a hard negative in the next training run

A Jira Automation rule sends a webhook to the KIZASHI Feedback API when a ticket is resolved.

## References

- `deploy/alertmanager/routing.yml` — Alertmanager configuration
- `kizashi/sources/jira.py` — Jira SM client implementation
- `rules/kizashi-alerts.yml` — alert rules (with `kizashi="predictive"` label)

</details>
