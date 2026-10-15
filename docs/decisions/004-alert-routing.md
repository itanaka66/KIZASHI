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
