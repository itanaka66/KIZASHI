# ADR-001: 長期ストレージの選定

**ステータス**: 決定済み  
**決定日**: 2026-10-15

---

## 背景

KIZASHI のベースライン学習（§4.2）は過去 4〜8 週間のデータを必要とし、P4 の教師あり学習では障害ラベルの蓄積のため 1 年以上のヒストリが望ましい。Prometheus 単体は通常 15〜30 日程度の保持にとどまるため、長期ストレージが必要。

候補: Thanos、Mimir、**VictoriaMetrics**

---

## 決定: VictoriaMetrics を採用する

### 採用理由

| 観点 | VictoriaMetrics | Thanos | Mimir |
|---|---|---|---|
| 導入コスト | ◎ シングルバイナリから始められる | △ サイドカー + Store + Query | △ コンポーネント多数 |
| Prometheus 互換 API | ◎ `/api/v1/query_range` をそのまま使える | ◎ | ◎ |
| ストレージ効率 | ◎ Prometheus TSDB の 2〜4 倍の圧縮率 | ○ | ○ |
| 保持期間設定 | ◎ 起動引数 `-retentionPeriod=13` のみ | △ compactor で設定 | △ |
| 大規模時のスケールアウト | ○（クラスタ版で対応） | ◎ | ◎ |
| オブジェクトストレージ依存 | △（クラスタ版は必要）| 必須 | 必須 |

VictoriaMetrics はシングルバイナリで起動でき、Prometheus の `remote_write` を受け取るだけで動作する。KIZASHI は Prometheus 互換 HTTP API を使うため、`long_term_url` を向け直すだけで統合できる。

---

## アーキテクチャ

```
Prometheus (保持 15d)
  │
  └── remote_write ──▶ VictoriaMetrics (保持 13 ヶ月)
                              │
                              │ /api/v1/query_range (Prometheus 互換)
                              ▼
                        KIZASHI (long_term_url: http://victoriametrics:8428)
```

Prometheus は推論用（直近 6h を query_range するため引き続き使用）、VictoriaMetrics は学習用の長期クエリ先として機能する。

---

## 保持期間

| 層 | 保持期間 | 理由 |
|---|---|---|
| Prometheus（RAM/SSD） | 15 日 | 推論用直近ウィンドウ（6h）+ バッファ |
| VictoriaMetrics（ローカルディスク） | **13 ヶ月** | P1 学習 4〜8 週 + P4 障害ラベル 1 年以上 + 余裕 1 ヶ月 |

---

## デプロイ構成

### シングルノード（初期〜中規模）

```yaml
# docker-compose または Kubernetes Deployment
victoria-metrics:
  image: victoriametrics/victoria-metrics:v1.102.0
  command:
    - -retentionPeriod=13          # 13 ヶ月
    - -storageDataPath=/data/vm
    - -httpListenAddr=:8428
    - -maxConcurrentInserts=16
  ports:
    - "8428:8428"
  volumes:
    - vm-data:/data/vm
```

### Prometheus の remote_write 設定

```yaml
# prometheus.yml に追加
remote_write:
  - url: http://victoriametrics:8428/api/v1/write
    queue_config:
      max_samples_per_send: 10000
      capacity: 50000
      max_shards: 30
```

### クラスタ版（大規模、10,000 台超）

vminsert / vmstorage / vmselect に分離。vmstorage を複数に増やして水平スケールアウト。  
詳細: https://docs.victoriametrics.com/Cluster-VictoriaMetrics.html

---

## KIZASHI 設定

```yaml
# kizashi.yaml
prometheus:
  url: http://prometheus:9090              # 推論用（直近 6h）
  long_term_url: http://victoriametrics:8428  # 学習用（13 ヶ月）
```

VictoriaMetrics は `/api/v1/query_range` を Prometheus と同じインターフェースで提供するため、KIZASHI 側の変更は不要。

---

## Helm chart / Kubernetes

VictoriaMetrics 公式 Helm chart を使用:

```sh
helm repo add vm https://victoriametrics.github.io/helm-charts/
helm install victoria-metrics vm/victoria-metrics-single \
  --set server.retentionPeriod=13 \
  --set server.persistentVolume.size=500Gi
```

---

## 未決事項

- ディスク容量の見積もり（50 万系列 × 13 ヶ月の圧縮後サイズ確認）
- 将来的なクラスタ版への移行タイミング
