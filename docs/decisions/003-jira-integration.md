# ADR-003: 障害チケットシステム（Jira Service Management）との連携

**ステータス**: 決定済み  
**決定日**: 2026-10-15

---

## 背景

予兆アラートを Jira Service Management（JSM）に自動起票することで、担当者アサインと作業追跡を既存の ITSM フローに乗せる。

---

## 決定: Alertmanager webhook → KIZASHI Feedback API → Jira REST API v3

### 採用理由

Alertmanager の webhook receiver はアラートの fire / resolved イベントを KIZASHI に送り、KIZASHI が Jira API を呼ぶ構成とする。これにより:
- Jira 起票と同時に KIZASHI の Feedback ラベル記録を一か所で処理できる
- Alertmanager に Jira 認証情報を持たせずに済む

---

## 原因分類の粒度

Jira issue の `labels` フィールドと、カスタムフィールド `failure_mode` で分類する。

| `failure_mode` | 原因 | 対応の目安 |
|---|---|---|
| `optic_failure` | 光モジュール劣化（Rx パワー低下） | モジュール交換（リードタイム考慮し早期発注） |
| `link_flap` | リンクフラップ（物理層不安定） | ケーブル・コネクタ・SFP 確認 |
| `traffic_saturation` | 帯域逼迫（容量逼迫） | 増速 or トラフィックエンジニアリング |
| `memory_leak` | メモリリーク（空きメモリ単調減少） | 機器再起動・ソフトウェアアップデート |
| `bgp_instability` | BGP 不安定（ネイバー遷移多発） | 経路ポリシー・タイマー確認 |
| `hardware_degradation` | 筐体ハードウェア劣化（温度・PSU） | 現地確認・部品交換 |
| `anomaly_baseline` | ベースライン異常（原因不明） | 調査起票 |

---

## 実装詳細

実装: `kizashi/sources/jira.py`

### Jira API エンドポイント

| 操作 | エンドポイント |
|---|---|
| 既存チケット検索（重複防止） | `GET /rest/api/3/issue/picker?query=...` |
| チケット作成 | `POST /rest/api/3/issue` |
| コメント追加（alert resolved 時） | `POST /rest/api/3/issue/{key}/comment` |
| ステータス遷移 | `POST /rest/api/3/issue/{key}/transitions` |

### 認証

API トークン（Basic 認証): `Authorization: Basic base64(email:api_token)`  
トークンは Kubernetes Secret に格納し、環境変数 `JIRA_API_TOKEN` で渡す。

---

## KIZASHI 設定への追加

```yaml
# kizashi.yaml
jira:
  url: https://your-org.atlassian.net
  email: kizashi-bot@your-org.com
  api_token_env: JIRA_API_TOKEN   # 環境変数名（値は Secret から）
  project_key: NWOPS              # Jira プロジェクトキー
  issue_type: Incident            # または "Service Request"
  # アラートごとの重複チケットを防ぐため instance+ifName+failure_mode で検索
  dedup_label: kizashi-alert-id
```

---

## 参照

- `kizashi/sources/jira.py` — 実装
- `docs/decisions/004-alert-routing.md` — フィードバックループ

---

<details>
<summary>English</summary>

# ADR-003: Failure Ticket System Integration (Jira Service Management)

**Status**: Decided  
**Date**: 2026-10-15

## Background

Automatically creating Jira Service Management (JSM) tickets for predictive alerts integrates the KIZASHI response workflow with the existing ITSM process, enabling assignee management and tracking.

## Decision: Alertmanager webhook → KIZASHI Feedback API → Jira REST API v3

### Rationale

The Alertmanager webhook receiver forwards alert fire / resolved events to KIZASHI, and KIZASHI calls the Jira API. This allows:
- Jira ticket creation and KIZASHI feedback label recording in one place.
- Jira credentials are not stored in Alertmanager.

Implementation: `kizashi/sources/jira.py`

## Failure Mode Classification

| `failure_mode` | Cause | Typical response |
|---|---|---|
| `optic_failure` | Optical module degradation (Rx power drop) | Module replacement (order early given lead times) |
| `link_flap` | Link flapping (physical layer instability) | Check cable / connector / SFP |
| `traffic_saturation` | Bandwidth saturation | Upgrade or traffic engineering |
| `memory_leak` | Memory leak (monotonically decreasing free memory) | Reboot or software update |
| `bgp_instability` | BGP instability (excessive neighbor transitions) | Review route policy / timers |
| `hardware_degradation` | Chassis hardware degradation (temperature / PSU) | On-site inspection / parts replacement |
| `anomaly_baseline` | Baseline anomaly (unknown cause) | Investigation ticket |

## API Endpoints

| Operation | Endpoint |
|---|---|
| Search for existing ticket (deduplication) | `GET /rest/api/3/issue/picker?query=...` |
| Create ticket | `POST /rest/api/3/issue` |
| Add comment (on alert resolved) | `POST /rest/api/3/issue/{key}/comment` |
| Transition status | `POST /rest/api/3/issue/{key}/transitions` |

## Authentication

API token (Basic auth): `Authorization: Basic base64(email:api_token)`  
Token stored in a Kubernetes Secret, passed as env var `JIRA_API_TOKEN`.

## KIZASHI Configuration

```yaml
jira:
  url: https://your-org.atlassian.net
  email: kizashi-bot@your-org.com
  api_token_env: JIRA_API_TOKEN
  project_key: NWOPS
  issue_type: Incident
  dedup_label: kizashi-alert-id
```

## References

- `kizashi/sources/jira.py` — implementation
- `docs/decisions/004-alert-routing.md` — feedback loop

</details>
