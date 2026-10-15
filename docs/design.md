# KIZASHI 設計書 — Prometheus 向け AI 故障予兆検知

## 1. 目的とスコープ

Splunk ITSI（Predictive Analytics / Adaptive Thresholding）や Splunk MLTK が提供する
「メトリクスとログから機械学習で障害を事前に予測する」機能を、Prometheus エコシステム上に実装する。

| Splunk の機能 | KIZASHI での対応 |
|---|---|
| Adaptive Thresholding（時間帯別しきい値） | 季節性ベースライン + 動的バンド（§5.1） |
| Anomaly Detection（MLTK `DensityFunction` 等） | 単変量 / 多変量異常スコア（§5.1, §5.3） |
| `predict` コマンド（時系列予測） | 予測値 + しきい値到達時間（§5.2） |
| ITSI Predictive Analytics（30分後のヘルススコア予測） | 故障確率モデル（§5.4）+ サービスヘルススコア（§6） |
| Episode Review / Notable Event のグルーピング | トポロジ考慮のアラート集約（§8） |
| ログパターン分析（`cluster` コマンド） | Loki ログのテンプレート化と件数メトリクス化（§5.5） |

**スコープ外**: Prometheus 本体の改修。KIZASHI は Prometheus の外側で動く独立サービスとし、
入出力はすべて Prometheus 標準のインターフェース（HTTP API / exposition / remote_write / ルール）で行う。

### 設計方針

1. **Prometheus ネイティブ**: 予測結果は通常のメトリクスとして書き戻し、アラートは PromQL ルール、
   通知は Alertmanager、可視化は Grafana をそのまま使う。運用者は新しいツールを覚えなくてよい。
2. **段階的な高度化**: 統計手法（学習データ不要）→ 教師なし異常検知 → 教師あり故障予測、の順に導入する。
   各層は単独でも価値を出し、上位モデルが使えないときは下位にフォールバックする。
3. **説明可能性**: すべての予兆アラートに「どのメトリクスが、どれだけ、なぜ」を付ける。
   根拠のないスコアだけのアラートは運用で信用されない。

### 実装状況（2026-10-15 時点）

**P0〜P5 すべて実装済み。** 各フェーズの主な実装ファイルを下表に示す。

| フェーズ | 状態 | 主な実装ファイル |
|---|---|---|
| P0 基盤 | ✅ 完了 | `inference.py`, `exporter.py`, `sources/prometheus.py`, `config.py`, `cli.py` |
| P1 統計層 | ✅ 完了 | `models/baseline.py`, `models/trend.py`, `trainer.py`, `rules/kizashi-alerts.yml` |
| P2 教師なし異常検知 | ✅ 完了 | `models/multivariate.py`, `models/logpattern.py`, `sources/loki.py`, `topology.py` |
| P3 フィードバック | ✅ 完了 | `feedback.py`, `label_store.py`, `rules/kizashi-alerts.yml` |
| P4 教師あり故障確率 | ✅ 完了 | `models/failure.py`, `explain.py`, `models/registry.py` |
| P5 サービスヘルス | ✅ 完了 | `health.py`, `examples/services.yaml`, `rules/kizashi-alerts.yml` |

**実装上の主な設計判断（設計書との差分）:**
- **Feature Store**: 当初 S3/MinIO + Parquet を想定していたが、ラベル量が少ない初期段階では JSONL
  アペンドオンリーファイル（`LabelStore`）をメインにした。Parquet 移行は将来オプション。
- **Model Registry**: P0〜P3 はファイルベース（`models/<layer>/<key>.json` + `.pkl`、アトミック書き込み・
  ホットリロード対応）。P4 で MLflow 連携を追加（`models/registry.py`）したが任意依存のまま。
- **Feedback API 認証**: OIDC は将来オプション。現在は静的 Bearer トークン（env var）。
- **Python バージョン**: 3.12 ではなく 3.11 以上（`pyproject.toml: requires-python = ">=3.11"`）。

---

## 2. 全体アーキテクチャ

```
 ネットワーク機器
  │ SNMP / gNMI / syslog / ICMP
  ▼
┌──────────────────────────────┐        ┌──────────────────────┐
│ snmp_exporter / gnmic         │        │ Loki (syslog)         │
│ blackbox_exporter             │        └─────────┬────────────┘
└──────────────┬───────────────┘                  │ LogQL
               │ scrape                           │
               ▼                                  │
┌──────────────────────────────┐  remote_write   │
│ Prometheus                    │───────────────┐ │
│  - recording rules (特徴量)   │               ▼ ▼
│  - alerting rules (予兆)      │      ┌────────────────────┐
└──────┬───────────────▲───────┘      │ 長期ストレージ        │
       │ alerts        │ scrape       │ Thanos / Mimir       │
       ▼               │ /metrics     └─────────┬──────────┘
┌──────────────┐  ┌────┴─────────────────────────▼──────────────┐
│ Alertmanager │  │ KIZASHI                                      │
└──────┬───────┘  │  ┌───────────┐ ┌────────────┐ ┌───────────┐ │
       │ webhook  │  │ Inference │ │ Trainer    │ │ Feedback  │ │
       └─────────▶│  │ (常駐)    │ │ (日次Job)  │ │ API       │ │
                  │  └─────┬─────┘ └─────┬──────┘ └─────┬─────┘ │
                  │        └────── Model Registry / Feature Store ┘
                  │         (ファイルベース + MLflow 任意 + JSONL)    │
                  └──────────────────────────────────────────────┘
                                     │
                                     ▼
                              Grafana（予兆ダッシュボード）
```

### コンポーネント

| コンポーネント | 役割 | 実行形態 |
|---|---|---|
| **Inference** | 1 分周期で直近ウィンドウを PromQL で取得し、全モデルで推論、結果を `/metrics` で公開 | Deployment（常駐、シャーディング可） |
| **Trainer** | 長期ストレージから学習データを抽出し、モデルを学習・評価・登録 | CronJob（日次 / 週次） |
| **Feedback API** | Alertmanager webhook とオペレーターの判定（正解 / 誤報）を受け取り、ラベルとして保存 | Deployment |
| **Model Registry** | モデル成果物、バージョン、評価指標の管理 | ファイルベース（`models/` ディレクトリ）+ MLflow（P4〜、任意） |
| **Feature Store** | 学習用の特徴量スナップショットと障害ラベル | JSONL アペンドオンリー（`LabelStore`）。Parquet / S3 は将来オプション |

---

## 3. 入力データ

### 3.1 収集対象メトリクス

故障の「予兆」として実績のある指標に絞って収集する。

| 故障モード | 予兆となるメトリクス | 取得元 |
|---|---|---|
| 光モジュール劣化 | Rx/Tx 光パワー (dBm) の緩やかな低下、レーザーバイアス電流の上昇、モジュール温度 | SNMP (ENTITY-SENSOR-MIB) / gNMI |
| 物理層劣化（ケーブル・コネクタ） | `ifInErrors`, CRC エラー, FEC 訂正数の増加傾向 | SNMP (IF-MIB, EtherLike-MIB) |
| リンクフラップ | `ifOperStatus` の変化回数、`ifLastChange` | SNMP / syslog |
| 輻輳・容量逼迫 | 帯域使用率、`ifOutDiscards`、キュー滞留 | SNMP / gNMI |
| 制御プレーン不安定 | CPU 使用率、BGP / OSPF ネイバーの状態遷移回数、経路数の急変 | SNMP (BGP4-MIB) / gNMI |
| メモリリーク | 空きメモリの単調減少 | SNMP (HOST-RESOURCES-MIB / ベンダー MIB) |
| 筐体ハードウェア | 温度、ファン回転数、PSU 電圧・状態 | SNMP (ENTITY-SENSOR-MIB) |
| サービス品質劣化 | RTT, パケットロス, ジッター | blackbox_exporter |
| ソフトウェア異常 | syslog の未知パターン出現、エラーログ件数の急増 | Loki |

### 3.2 特徴量の前処理（Prometheus recording rules）

レート計算などの単純な前処理は Prometheus 側の recording rule で行い、KIZASHI の負荷とクエリ量を減らす。

```yaml
groups:
  - name: kizashi-features
    interval: 1m
    rules:
      - record: kizashi:if_in_errors:rate5m
        expr: rate(ifInErrors[5m])
      - record: kizashi:if_util_in:ratio5m
        expr: rate(ifHCInOctets[5m]) * 8 / (ifHighSpeed * 1e6)
      - record: kizashi:if_flaps:changes1h
        expr: changes(ifOperStatus[1h])
      - record: kizashi:optic_rx_dbm:avg5m
        expr: avg_over_time(optic_rx_power_dbm[5m])
```

### 3.3 障害ラベル（教師データ）

教師あり学習（§5.4）には「いつ・どこで・何の故障が起きたか」が必要。以下から収集する。

1. **Alertmanager の履歴**: `InterfaceDown`, `DeviceUnreachable` などの確定障害アラート（webhook で受信）
2. **チケットシステム**: ServiceNow / Redmine 等の障害チケット（機器名・発生時刻・原因分類）
3. **オペレーターのフィードバック**: 予兆アラートに対する「正解 / 誤報」判定（§8.3）

ラベルは `(device, interface, failure_mode, started_at)` の形で Feature Store に保存する。

---

## 4. 処理フロー

### 4.1 推論ループ（Inference、1 分周期）

```
1. 対象系列の列挙      : /api/v1/series でシャードに割り当てられた系列を取得（5分ごとにキャッシュ更新）
2. データ取得          : /api/v1/query_range で直近ウィンドウ（例: 6h, step=1m）をバッチ取得
3. 前処理              : 欠損補間、カウンタリセット補正、正規化（学習時の統計量を使用）
4. 推論                : 各モデル層を実行（§5）。モデル未学習の系列は統計層のみ
5. 説明の付与          : 寄与度の大きい特徴量を算出（§7）
6. 公開                : 結果を /metrics のゲージとして更新 → Prometheus が scrape
```

- **書き戻し方式は scrape を基本とする。** remote_write はバックフィル（過去期間の再計算）時のみ使う。
- Prometheus は未来時刻のサンプルを受け付けないため、**予測値は「現在時刻のサンプル」として、
  予測先の時間幅を `horizon` ラベルで表す**（例: `horizon="1h"` は 1 時間後の予測値）。

### 4.2 学習ループ（Trainer、日次）

```
1. 学習データ抽出   : 長期ストレージから過去 4〜8 週間を取得し Parquet 化
2. ラベル結合       : 障害ラベルを時間窓で結合（故障の H 時間前までを正例とする）
3. 学習             : 層ごとにモデルを学習
4. バックテスト     : 直近 1 週間をホールドアウトして評価（§9）
5. 判定             : 現行モデルより評価指標が改善した場合のみ "Production" に昇格
6. 配信             : Inference が Model Registry をポーリングしてホットリロード
```

---

## 5. 予兆検知モデル（4+1 層）

### 5.1 第 1 層: 適応しきい値（統計ベースライン）✅ 実装済み

**学習データ不要・初日から動く層。** Splunk の Adaptive Thresholding に相当する。

- 手法: 系列ごとに季節性プロファイル（1 時間単位のビンごとの中央値）で期待値を推定し、
  残差のロバスト標準偏差（MAD）から上下バンドを算出する。
  履歴が 2 週間以上あれば週次（168 ビン）、2 日以上 2 週間未満なら日次（24 ビン）のプロファイルを使う。
  中央値ベースなので、過去の障害による外れ値でベースラインが歪みにくい。
  複数の季節性が重なる系列で精度が足りない場合は、STL / Holt-Winters への置き換えを検討する。
  （実装: `kizashi/models/baseline.py`）
- 異常スコア: `|実測値 - 期待値| / (1.4826 × MAD)`（ロバスト z スコア）
- 祝日やメンテナンス期間は学習から除外する（Alertmanager の silence 情報を参照）。

> 簡易版は PromQL だけでも実現できる（前週同時刻との比較）。KIZASHI 導入前の暫定策として使える。
> ```promql
> (kizashi:if_util_in:ratio5m - avg_over_time(kizashi:if_util_in:ratio5m[1h] offset 1w))
>   / stddev_over_time(kizashi:if_util_in:ratio5m[1h] offset 1w)
> ```

### 5.2 第 2 層: トレンド予測としきい値到達時間 ✅ 実装済み

**「あと何時間で限界値に達するか」** を出す層。光パワー劣化・メモリリーク・容量逼迫に有効。

- 手法: 加重最小二乗線形回帰（`models/trend.py`）を基本とし、信頼区間付きで予測する。
  設計当初は Theil-Sen / ETS / Prophet を候補としていたが、軽量性を優先して WLS を採用。
  Prometheus 標準の `predict_linear()` は外れ値に弱く信頼区間も出ないため、それを置き換える位置づけ。
- 出力: 予測値と予測区間（下限・上限）、限界値（例: Rx -14 dBm、メモリ空き 5%）に達するまでの推定秒数。
- 限界値はターゲット設定の `thresholds` セクション（`kizashi.yaml`）で管理する。

### 5.3 第 3 層: 多変量異常検知 ✅ 実装済み

**単体のメトリクスでは正常範囲でも、組み合わせが異常** というケースを捉える層。
例: トラフィックは平常なのに CPU だけ高い、温度上昇とファン回転数低下が同時に起きている。

- 手法: 機器の役割（コア / ディストリビューション / アクセス）× ベンダー機種ごとに
  **グループ単位の共有モデル** を学習する。系列ごとにモデルを持つと数十万モデルになり運用できないため。
  - 標準: **Isolation Forest**（実装済み: `models/multivariate.py`。軽量・解釈しやすい）
  - 大規模環境向けオプション: LSTM / Transformer オートエンコーダ（再構成誤差を異常スコアに使う）
- 入力: 機器単位の特徴量ベクトル（CPU、メモリ、温度、全 IF のエラー率合計、ネイバー遷移数など）を 1 分ごとに作成。
- NaN は列中央値で補完。スコアは `decision_function` をロジスティック変換で 0〜10 スケールに正規化。

### 5.4 第 4 層: 故障確率予測（教師あり）✅ 実装済み

**「今後 H 時間以内にこの機器 / IF が故障する確率」** を出す層。ITSI Predictive Analytics に相当する。
障害ラベルが一定数（目安: 故障モードあたり 30 件以上）たまってから有効化する。

- 手法: 勾配ブースティング（LightGBM）による二値分類。故障モードごとにモデルを分ける。
  （実装: `models/failure.py`、SHAP 説明: `explain.py`、MLflow 連携: `models/registry.py`）
- 特徴量: 第 1〜3 層の出力（異常スコア、トレンドの傾き、到達時間）に加え、
  複数ウィンドウ（1h / 6h / 24h / 7d）での平均・最大・傾き・変化回数、機種・設置年数などの静的属性。
- 予測先: `horizon` = 1h / 24h / 7d。
- クラス不均衡: 負例ダウンサンプリング（デフォルト 10:1）と `scale_pos_weight` で対処。
  評価は Precision-Recall で行う（Accuracy は使わない）。
- 確率の較正: `CalibratedClassifierCV(cv='prefit', method='isotonic')` で補正。
- モデル昇格条件: ウォークフォワード検証で Precision@0.5 ≥ 0.5、Recall ≥ 0.6、リードタイム中央値 ≥ 1h。
- PSI ドリフト検知: 学習時の特徴量分布（パーセンタイル）を保存し、次回学習時に比較。
  PSI ≥ 0.2 の場合は新モデルをシャドーモードで保存（`FailureModelMeta.is_shadow = True`）。
- MLflow 連携はオプション（`kizashi[mlflow]`）。未導入の場合は `NoOpRegistry` を使用。

### 5.5 補助層: ログパターン異常 ✅ 実装済み

- Loki から syslog を取得し、Drain アルゴリズムでテンプレート（例: `%LINK-3-UPDOWN: Interface <*>, changed state to <*>`）に分類する。
  （実装: `models/logpattern.py`、Loki クライアント: `sources/loki.py`）
- テンプレートごとの出現件数をメトリクス化し、第 1 層（件数の急増）と第 4 層（特徴量）に入力する。
- 過去に出現したことのない **新規テンプレート** の出現も予兆シグナルとして扱う（`mark_known()` / `iter_new()` で管理）。
- テンプレートの状態は JSON でアトミック永続化（`tempfile.mkstemp()` + `os.replace()`）。

### 5.6 フォールバック

| 状態 | 動作 |
|---|---|
| 新規機器（履歴 < 7 日） | 第 1 層は同一グループの平均プロファイルで代替、第 2 層のみ有効 |
| モデル読み込み失敗 | 前バージョンのモデルを使い、`kizashi_model_load_errors_total` を増加させる |
| Prometheus クエリ失敗 | その周期の出力を更新しない（古い値を出し続けないよう、メトリクスを削除して stale にする） |

---

## 6. サービスヘルススコア ✅ 実装済み

ITSI の Service / KPI モデルに倣い、個々の機器の予兆を **サービス単位** にまとめる。
（実装: `kizashi/health.py`、サービス定義サンプル: `examples/services.yaml`）

- サービス定義 YAML（`validate_services_spec()` でスキーマ検証）で「サービス → 構成機器・IF → 重み・冗長グループ」を記述する。
- ヘルススコア式: `score = 100 − Σ_groups(group_weight × effective_norm_score) / total_weight × 100`
  - 冗長グループ（同じ `redundancy_group` を持つコンポーネント）: `effective_score = min(メンバースコア)` — 1 本でも正常なら減点なし。
  - シングルトン: コンポーネント個別に計算。
- 将来のヘルススコア（1h 後）は、第 4 層の `kizashi_failure_probability{horizon="1h"}` が存在する場合のみ出力。
- 出力: `kizashi_health_score{service, horizon}` (horizon = "now" | "1h")
- アラート: `ServiceHealthDegrading` (< 80, 10m), `ServiceHealthCritical` (< 50, 5m) を `rules/kizashi-alerts.yml` に定義。

---

## 7. 出力メトリクス

| メトリクス | 型 | 主なラベル | 意味 |
|---|---|---|---|
| `kizashi_expected_value` | gauge | `metric`, `instance`, `ifName` | 第 1 層の期待値 |
| `kizashi_band_lower` / `kizashi_band_upper` | gauge | 同上 | 適応しきい値の下限 / 上限 |
| `kizashi_anomaly_score` | gauge | 同上 + `layer` | 異常スコア（0 = 正常、3 以上で異常の目安） |
| `kizashi_forecast_value` | gauge | 同上 + `horizon`, `quantile` | 予測値（`quantile` = 0.1 / 0.5 / 0.9） |
| `kizashi_time_to_threshold_seconds` | gauge | 同上 + `threshold` | 限界値に到達するまでの推定秒数（到達しない場合は出力しない） |
| `kizashi_failure_probability` | gauge | `instance`, `ifName`, `failure_mode`, `horizon` | 故障確率（0〜1） |
| `kizashi_top_contributor` | gauge | `instance`, `failure_mode`, `rank`, `feature` | 予兆の根拠となった特徴量の寄与度（SHAP 値、上位 3 件） |
| `kizashi_health_score` | gauge | `service`, `horizon` | サービスヘルススコア（0〜100） |
| `kizashi_model_info` | gauge | `model`, `version`, `trained_at` | 稼働中モデルの情報（値は常に 1） |
| `kizashi_inference_duration_seconds` | histogram | `layer` | 推論処理時間（自己監視用） |

**カーディナリティ対策**: `kizashi_forecast_value` と `kizashi_top_contributor` は、
異常スコアまたは故障確率が一定値を超えた系列だけを出力する。全系列分を常に出力すると系列数が元データの数倍に膨らむため。

---

## 8. アラートと運用

### 8.1 アラートルール例

```yaml
groups:
  - name: kizashi-alerts
    rules:
      - alert: OpticDegradationPredicted
        expr: kizashi_time_to_threshold_seconds{metric="optic_rx_dbm"} < 72 * 3600
        for: 30m
        labels:
          severity: warning
          kizashi: predictive
        annotations:
          summary: "{{ $labels.instance }} {{ $labels.ifName }} の受光レベルが約 {{ $value | humanizeDuration }} 後に限界値へ到達する見込み"

      - alert: FailurePredicted
        expr: kizashi_failure_probability{horizon="24h"} > 0.7
        for: 15m
        labels:
          severity: critical
          kizashi: predictive
        annotations:
          summary: "{{ $labels.instance }} で 24 時間以内に {{ $labels.failure_mode }} が発生する確率 {{ $value | humanizePercentage }}"
          dashboard: "https://grafana.example/d/kizashi?var-instance={{ $labels.instance }}"

      - alert: ServiceHealthDegrading
        expr: kizashi_health_score{horizon="1h"} < 60
        for: 10m
        labels:
          severity: warning
          kizashi: predictive
```

### 8.2 誤報の抑制（アラート疲れ対策）

予兆アラートは誤報が多いと無視されるようになるため、以下を必須とする。

- **持続条件**: `for:` で一定時間継続した場合のみ発報する。
- **トポロジ集約**: 上流機器に予兆がある場合、配下の機器の予兆は Alertmanager の `inhibit_rules` で抑制する。
  トポロジ情報は LLDP / CDP から生成した `kizashi_topology_parent` メトリクスで渡す。
- **メンテナンス連動**: Alertmanager の silence 中、およびその前後 30 分は学習データから除外し、アラートも出さない。
- **発報上限の監視**: 1 日あたりの予兆アラート件数を監視し、急増したらモデル劣化を疑う（§9.2）。

### 8.3 フィードバックループ

1. Grafana のダッシュボードまたは Slack 通知のボタンから、オペレーターが予兆アラートを「正解 / 誤報 / 対応済み」で判定する。
2. Feedback API が判定結果を Feature Store にラベルとして保存する。
3. 次回の学習で、正解は正例に、誤報は負例（ハードネガティブ）として使う。
4. 「対応済み」（予兆を見て交換した等）は故障が起きないため、正例として扱い、評価指標からは除外する。

---

## 9. 評価と品質管理

### 9.1 オフライン評価（学習時）

| 指標 | 意味 | 昇格の目安 |
|---|---|---|
| Precision@アラート閾値 | 予兆アラートのうち実際に故障した割合 | ≥ 0.5 |
| Recall | 実際の故障のうち事前に予兆を出せた割合 | ≥ 0.6 |
| リードタイム中央値 | 予兆アラートから故障までの時間 | ≥ 1h |
| 誤報数 / 機器 / 週 | 運用負荷 | ≤ 0.1 |

時系列データのため、ランダム分割ではなく **時間順の分割（ウォークフォワード検証）** で評価する。

### 9.2 オンライン監視（稼働中）

- **データドリフト**: 入力特徴量の分布を学習時と比較し（PSI）、閾値超過で再学習を起動する。
- **ネットワーク構成変更**: 機器の追加・交換・回線増速の後は分布が変わるため、該当機器のベースラインを再学習する。
- **シャドーモード**: 新モデルは 1 週間、アラートを出さずに推論のみ行い、現行モデルと比較してから切り替える。

---

## 10. 設定ファイル

```yaml
# kizashi.yaml
prometheus:
  url: http://prometheus:9090
  long_term_url: http://thanos-query:9090
loki:
  url: http://loki:3100

inference:
  interval: 1m
  window: 6h
  shard: { total: 2, index: ${SHARD_INDEX} }

targets:
  - metric: kizashi:optic_rx_dbm:avg5m
    layers: [baseline, trend]
    thresholds:
      - name: rx_low
        by_label: transceiver_type
        values: { "10GBASE-LR": -14.4, "100GBASE-LR4": -10.6 }
  - metric: kizashi:if_in_errors:rate5m
    layers: [baseline]
  - group: device
    match: '{job="snmp"}'
    group_by: [role, vendor_model]
    layers: [multivariate, failure_probability]

failure_modes:
  - name: optic_failure
    label_source: alertmanager
    match: { alertname: OpticModuleFailure }
  - name: link_down
    label_source: alertmanager
    match: { alertname: InterfaceDown }

services:
  - name: osaka-wan
    members:
      - { instance: osaka-rt01, weight: 0.5 }
      - { instance: osaka-rt02, weight: 0.5 }
    redundancy: active-standby
```

---

## 11. 非機能要件

| 項目 | 目標値 / 方針 |
|---|---|
| 規模 | 機器 10,000 台、入力系列 50 万本 |
| 推論遅延 | 1 分周期内に完了（p99 < 45 秒） |
| 可用性 | Inference は 2 シャード × 2 レプリカ。推論はステートレスで、モデルは Registry から取得 |
| Prometheus への負荷 | recording rule で前処理を済ませ、KIZASHI のクエリは 1 シャードあたり 10 req/s 以下に抑える |
| 出力系列数 | 入力系列数の 30% 以下（§7 のカーディナリティ対策） |
| セキュリティ | Prometheus / Loki へは読み取り専用の認証情報。Feedback API は OIDC 認証 |
| 自己監視 | KIZASHI 自身のメトリクスを公開し、推論遅延やモデル読み込み失敗をアラート化する |

---

## 12. 技術スタック

| 用途 | 採用技術 | 理由 |
|---|---|---|
| 実装言語 | Python ≥ 3.11 | 機械学習ライブラリが揃っている |
| 統計 / 予測 | statsmodels（STL, ETS）、scikit-learn（Theil-Sen, Isolation Forest） | 実績があり軽量 |
| 教師あり学習 | LightGBM + SHAP | 表形式データで高精度、寄与度を算出できる |
| 深層学習（任意） | PyTorch | 大規模環境向けオートエンコーダ |
| ログ解析 | Drain3 | オンラインでテンプレート抽出が可能 |
| メトリクス公開 | prometheus_client | 標準ライブラリ |
| API | FastAPI | Feedback API、ヘルスチェック |
| モデル管理 | ファイルベース（必須）+ MLflow（`kizashi[mlflow]`、任意） | バージョン管理と評価指標の記録 |
| データ | JSONL アペンドオンリー（必須）。Parquet / S3 は将来オプション | 障害ラベルの保存 |
| デプロイ | Kubernetes + Helm | Prometheus Operator と同じ環境に載せられる |

---

## 13. リポジトリ構成（P0〜P5 実装済み）

```
KIZASHI/
├── kizashi/
│   ├── config.py              # 設定ファイルの読み込み（PrometheusConfig, LokiConfig, FeedbackConfig 等）
│   ├── cli.py                 # `kizashi` CLI（train / feedback / serve サブコマンド）
│   ├── inference.py           # 推論ループ（1分周期）
│   ├── trainer.py             # 学習ジョブ（train_baselines / train_failure_models）
│   ├── exporter.py            # /metrics 公開（SnapshotCollector + FAMILIES 定義）
│   ├── feedback.py            # Feedback API（FastAPI、Alertmanager webhook + オペレーター判定）
│   ├── label_store.py         # 障害ラベルの JSONL アペンドオンリーストア
│   ├── health.py              # サービスヘルススコア算出（P5）
│   ├── explain.py             # SHAP による寄与度算出（P4）
│   ├── llm.py                 # LLM による自然言語説明（Ollama、任意）
│   ├── topology.py            # ネットワークトポロジ変更検知（TopologyStore）
│   ├── sources/
│   │   ├── prometheus.py      # Prometheus HTTP クライアント
│   │   ├── loki.py            # Loki HTTP クライアント（LogQL）
│   │   ├── alertmanager.py    # Alertmanager クライアント（silence 情報参照）
│   │   └── jira.py            # Jira Service Management REST API v3 クライアント
│   ├── models/
│   │   ├── store.py           # ModelStore（アトミック読み書き、ホットリロード）
│   │   ├── baseline.py        # 第1層: 季節性ベースライン + 動的バンド
│   │   ├── trend.py           # 第2層: WLS トレンド予測・到達時間
│   │   ├── multivariate.py    # 第3層: Isolation Forest 多変量異常検知
│   │   ├── logpattern.py      # 補助層: Drain アルゴリズム ログパターン
│   │   ├── failure.py         # 第4層: LightGBM 故障確率（PSI、ウォークフォワード検証）
│   │   └── registry.py        # MLflow Model Registry 連携（NoOpRegistry でオプション化）
├── examples/
│   ├── kizashi.yaml           # 設定ファイルサンプル
│   └── services.yaml          # サービス定義サンプル（P5）
├── rules/
│   ├── kizashi-features.yml   # Prometheus recording rules（特徴量前処理）
│   ├── kizashi-alerts.yml     # アラートルール（predictive / health / self-monitor）
│   ├── kizashi-topology.yml   # LLDP/CDP から topology_parent メトリクス生成
│   └── snmp-normalization/    # ベンダー別 DOM 値正規化 recording rules（ADR-002）
├── dashboards/
│   ├── kizashi-predictive.json
│   └── kizashi-health.json
├── deploy/
│   ├── helm/                  # Kubernetes Helm Chart
│   └── alertmanager/
│       └── routing.yml        # Alertmanager routing + inhibit_rules（ADR-004）
├── docs/
│   ├── design.md              # 本ドキュメント
│   └── decisions/             # アーキテクチャ決定記録（ADR）001〜005
└── tests/
    ├── test_baseline.py
    ├── test_trend.py
    ├── test_multivariate.py
    ├── test_logpattern.py
    ├── test_feedback.py
    ├── test_failure.py
    ├── test_explain.py
    └── test_health.py
```

---

## 14. 導入ロードマップ

| フェーズ | 期間目安 | 内容 | 完了条件 |
|---|---|---|---|
| **P0: 基盤** ✅ | 2 週 | recording rule、Inference の骨格、`/metrics` 公開、Grafana ダッシュボード | 対象メトリクスを取得して公開できる |
| **P1: 統計層** ✅ | 3 週 | 第 1 層（適応しきい値）と第 2 層（到達時間）、アラートルール | 光パワー劣化を 72 時間前に検知できる |
| **P2: 教師なし** ✅ | 4 週 | 第 3 層（多変量）、ログパターン、トポロジ集約 | 誤報 ≤ 0.1 件 / 機器 / 週 |
| **P3: フィードバック** ✅ | 3 週 | Feedback API、障害ラベルの収集、Alertmanager 連携 | ラベルが継続的に蓄積される |
| **P4: 教師あり** ✅ | 4 週 | 第 4 層（故障確率）、SHAP による説明、シャドーモード | §9.1 の昇格基準を満たす |
| **P5: サービス** ✅ | 3 週 | サービスヘルススコア、将来ヘルス予測 | 主要サービスのヘルススコアを表示 |

**全 6 フェーズ実装完了（2026-10-15 時点）。**

---

## 15. 解決済み事項（旧: 未決事項）

設計開始時の未決事項はすべて解決済み。詳細は `docs/decisions/` の各 ADR を参照。

| # | 事項 | 決定内容 | ADR |
|---|---|---|---|
| 1 | 長期ストレージの選定 | **VictoriaMetrics**（13ヶ月保持） | `docs/decisions/001-long-term-storage.md` |
| 2 | 光モジュール DOM 値の正規化 | snmp_exporter + Prometheus recording rules（`rules/snmp-normalization/`） | `docs/decisions/002-dom-normalization.md` |
| 3 | 障害チケットシステム連携 | **Jira Service Management REST API v3**（`kizashi/sources/jira.py`） | `docs/decisions/003-jira-integration.md` |
| 4 | 予兆アラートの通知先・対応フロー | Alertmanager routing + Slack/Jira/PagerDuty（`deploy/alertmanager/routing.yml`） | `docs/decisions/004-alert-routing.md` |
| 5 | LLM による自然言語説明 | **Ollama**（オプション機能、`kizashi/llm.py`） | `docs/decisions/005-llm-explanation.md` |
