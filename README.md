# KIZASHI

**AI-based failure prediction for network monitoring on Prometheus.**

KIZASHI (兆し, "early sign") brings IT-Service-Intelligence-style adaptive thresholds and predictive
alerts to a Prometheus stack. It learns what "normal" looks like for every interface and
device, flags deviations, and estimates how long until a degrading metric — optical power,
error rate, utilization — crosses a limit, so you can act before the link fails.

It needs no changes to Prometheus. KIZASHI reads metrics through the Prometheus HTTP API and
exposes predictions as ordinary metrics on `/metrics`. Prometheus scrapes them back, so your
existing alert rules, Alertmanager routing, and Grafana dashboards keep working.

```
 SNMP / gNMI exporters ──scrape──▶ Prometheus ──alerts──▶ Alertmanager
                                    │      ▲
                       query_range  │      │ scrape /metrics (predictions)
                                    ▼      │
                                   KIZASHI ┘
                                   ├─ serve : inference loop (every 1m)
                                   └─ train : baseline fitting (daily)
```

The full design, including the planned machine-learning layers, is in
[docs/design.md](docs/design.md) (Japanese).

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Getting started](#getting-started)
- [Configuration](#configuration)
- [Output metrics](#output-metrics)
- [Alert rules](#alert-rules)
- [Running in production](#running-in-production)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)
- [Development](#development)
- [License](#license)

## Features

| Layer | What it answers | Status |
|---|---|---|
| 1. Adaptive thresholds | "Is this value unusual *for this time of week*?" | ✅ Available |
| 2. Trend forecast | "Where is this heading, and when does it hit the limit?" | ✅ Available |
| 3. Multivariate anomaly detection | "Is this combination of metrics unusual?" | ✅ Available |
| 4. Failure probability | "How likely is this device to fail in the next 24h?" | ✅ Available |
| Log pattern anomalies (Loki) | "Are new or unusual syslog messages appearing?" | ✅ Available |

Also included:

- Prometheus recording rules that turn raw SNMP counters into model inputs
- Predictive alert rules, plus alerts on KIZASHI's own health
- Horizontal sharding across replicas
- Hot reload of retrained models without restarting

## How it works

### Layer 1: Adaptive thresholds

A fixed threshold such as "alert above 80% utilization" fires every business day on a busy
link and misses a quiet link that suddenly doubles at 3 a.m. KIZASHI instead learns a
**seasonal profile** for each series:

- `kizashi train` reads the configured history (28 days by default) and computes the median
  value for each hour of the week (168 bins). With 2 days to 2 weeks of history, it uses
  hour of day (24 bins) instead. Series with less than 2 days are skipped.
- The spread is the **median absolute deviation (MAD)** of the residuals. Unlike a standard
  deviation, it is barely affected by past incidents in the training data.
- At inference time, the latest value is compared with the expected value for the current
  hour. The result is a robust z-score: `|value − expected| / (1.4826 × MAD)`. A score
  around 0 is normal; 3 or more is unusual.

### Layer 2: Trend forecast and time-to-threshold

For slowly degrading components — optics losing power, memory leaking, links filling up —
the useful question is *when* the limit will be reached.

- Every cycle, KIZASHI fits a trend over the last 6 hours using the **Theil-Sen estimator**
  (the median of pairwise slopes). Unlike PromQL's `predict_linear()`, which uses least
  squares, a few spikes do not swing the result.
- It computes a confidence interval for the slope. A time-to-threshold is published **only
  when the whole interval points toward the limit**, so flat or noisy series stay quiet.
- Forecasts at each horizon (1h and 24h by default) come with a 10–90% band that widens with
  distance.

### Why predictions use a `horizon` label

Prometheus does not accept samples with future timestamps. A forecast for "24 hours from now"
is therefore published as a *current* sample with `horizon="24h"`.

## Requirements

- Python 3.11 or later
- Prometheus 2.x or 3.x (or a compatible API such as Thanos Query, Mimir, or VictoriaMetrics)
- Enough history for training: at least 2 days, ideally 2 weeks or more. If Prometheus
  retention is short, point training at long-term storage (`prometheus.long_term_url`).

## Installation

For production installs (systemd units, Docker, upgrading), see [INSTALLATION.md](INSTALLATION.md).
For a quick try from source:

```sh
git clone <this repository> kizashi && cd kizashi
python -m venv .venv
.venv/bin/pip install .
```

With Docker:

```sh
docker build -t kizashi .
```

## Getting started

This is the short version. [SETUP.md](SETUP.md) walks through each step in detail, including
mapping optical power metrics, backfilling history, Alertmanager routing, and Grafana panels.

**1. Load the recording rules.** [rules/kizashi-recording.yml](rules/kizashi-recording.yml)
turns raw counters into model inputs such as `kizashi:if_in_errors:rate5m`. The right-hand
side metric names (for example `optic_rx_power_dbm`) depend on your SNMP or gNMI exporter
setup. Edit the expressions to match your metrics, but keep the recorded names.

**2. Write a config.** Start from the example and set `prometheus.url` and your targets:

```sh
cp examples/kizashi.yaml kizashi.yaml
```

**3. Train baselines.** This step is optional at first; without baselines, only the trend
layer runs.

```sh
kizashi train -c kizashi.yaml
# kizashi:optic_rx_dbm:avg5m: 1532 series
```

**4. Try one inference cycle.** This prints the results instead of serving them:

```sh
kizashi serve -c kizashi.yaml --once
```

```
kizashi_anomaly_score{ifName="Te0/2",instance="rt01",layer="baseline",metric="kizashi:optic_rx_dbm:avg5m",...} 12.5487
kizashi_time_to_threshold_seconds{ifName="Te0/2",instance="rt01",metric="kizashi:optic_rx_dbm:avg5m",threshold="rx_low",...} 245944
```

**5. Run the service and scrape it.**

```sh
kizashi serve -c kizashi.yaml   # serves /metrics on :9850
```

Add the scrape job and the rule files to `prometheus.yml`
([examples/prometheus-scrape.yml](examples/prometheus-scrape.yml)):

```yaml
scrape_configs:
  - job_name: kizashi
    honor_labels: true      # required, see below
    scrape_interval: 1m
    static_configs:
      - targets: ["kizashi:9850"]

rule_files:
  - rules/kizashi-recording.yml
  - rules/kizashi-alerts.yml
```

> **`honor_labels: true` is required.** Predictions carry the labels of the source series,
> such as `instance="rt01"`. Without it, Prometheus overwrites `instance` and `job` with
> KIZASHI's own, and you can no longer tell which device a prediction is about.

## Configuration

`kizashi.yaml` supports `${VAR}` and `${VAR:-default}` environment variable expansion in
values. Durations use Prometheus syntax (`30s`, `5m`, `6h`, `28d`, `1w`, `1h30m`). A
complete example is in [examples/kizashi.yaml](examples/kizashi.yaml).

### Top-level options

| Key | Default | Description |
|---|---|---|
| `prometheus.url` | *required* | Prometheus used for inference |
| `prometheus.long_term_url` | `prometheus.url` | API used by `train` (Thanos / Mimir for long history) |
| `prometheus.timeout` | `30s` | HTTP timeout per query |
| `prometheus.bearer_token_file` | — | File containing a bearer token for authentication |
| `server.listen` | `0.0.0.0:9850` | Address for `/metrics` |
| `models.dir` | `./models` | Where trained baselines are stored |
| `inference.interval` | `1m` | How often the inference loop runs |
| `inference.window` | `6h` | History used for the trend fit |
| `inference.step` | `1m` | Resolution of inference queries |
| `inference.shard.total` / `.index` | `1` / `0` | Sharding (see [Scaling](#scaling)) |
| `inference.forecast_horizons` | `[1h, 24h]` | Horizons published in `kizashi_forecast_value` |
| `inference.emit_forecast_min_score` | `3` | Minimum anomaly score for publishing forecasts |
| `training.period` | `28d` | History used to fit baselines |
| `training.step` | `5m` | Resolution of training queries |
| `baseline.band_sigma` | `3` | Width of the adaptive band, in robust standard deviations |
| `trend.confidence` | `0.9` | Confidence level for the slope interval and forecast band |
| `trend.max_points` | `120` | Points used per trend fit (the window is thinned evenly) |
| `trend.min_points` | `10` | Minimum points needed to fit a trend |
| `trend.max_horizon` | `30d` | Time-to-threshold values further out are not published |

### Targets

Each target is one metric (usually a recording rule) to analyze:

```yaml
targets:
  - metric: kizashi:optic_rx_dbm:avg5m
    match: '{job="snmp"}'        # optional label matcher appended to the query
    layers: [baseline, trend]    # default: [baseline]
    thresholds:
      - name: rx_low             # becomes the `threshold` label
        direction: below         # alert when the trend falls below the limit
        by_label: transceiver_type
        values:                  # limit per value of the label
          10GBASE-LR: -14.4
          100GBASE-LR4: -10.6
        # value: -14.0           # fallback for label values not listed above

  - metric: kizashi:if_util_in:ratio5m
    layers: [baseline, trend]
    thresholds:
      - name: util_90pct
        direction: above
        value: 0.9               # single limit for every series
```

| Key | Description |
|---|---|
| `metric` | Metric name to query |
| `match` | Optional label matcher, for example `'{job="snmp",role="core"}'` |
| `layers` | `baseline` and/or `trend` |
| `thresholds[].name` | Name exposed in the `threshold` label |
| `thresholds[].direction` | `above` or `below` |
| `thresholds[].value` | Fixed limit, or the fallback when `by_label` has no match |
| `thresholds[].by_label` / `values` | Per-series limits, chosen by the value of a label |

Series with no matching threshold still get trend analysis, but no time-to-threshold.

## Output metrics

All prediction metrics carry the labels of the source series, plus `metric` (the source
metric name). If the source series already has a `metric` label, it is renamed to
`exported_metric`.

| Metric | Extra labels | Description |
|---|---|---|
| `kizashi_expected_value` | — | Expected value for the current hour of the week |
| `kizashi_band_lower` / `kizashi_band_upper` | — | Adaptive thresholds (expected ± `band_sigma` × σ) |
| `kizashi_anomaly_score` | `layer` | Robust z-score of the latest value |
| `kizashi_time_to_threshold_seconds` | `threshold` | Estimated seconds until the trend crosses the limit (0 if already past) |
| `kizashi_forecast_value` | `horizon`, `quantile` | Forecast at now + horizon; quantiles `0.1`, `0.5`, `0.9` |
| `kizashi_model_info` | `layer`, `version` | Model in use (always 1) |
| `kizashi_model_trained_timestamp_seconds` | `layer` | When the model in use was trained |

To keep cardinality bounded, `kizashi_forecast_value` is only published for series that are
heading toward a threshold or have an anomaly score of at least `emit_forecast_min_score`.

Series disappear from `/metrics` when they cannot be computed: the query failed, the device
stopped reporting more than about 5 minutes ago, or there is not enough data. Prometheus
marks them stale, so outdated predictions are never kept alive.

### Self-monitoring metrics

| Metric | Description |
|---|---|
| `kizashi_inference_duration_seconds{target}` | Time per target per cycle (histogram) |
| `kizashi_inference_series{target}` | Series processed by this shard in the last cycle |
| `kizashi_query_errors_total{target}` | Failed Prometheus queries |
| `kizashi_last_inference_timestamp_seconds` | Time of the last completed cycle |

### Example queries

```promql
# Links expected to fall below optical limits within 3 days, in hours
kizashi_time_to_threshold_seconds{threshold="rx_low"} / 3600 < 72

# Most anomalous interfaces right now
topk(10, kizashi_anomaly_score{metric="kizashi:if_in_errors:rate5m"})

# Grafana: actual value with its adaptive band
kizashi:if_util_in:ratio5m{instance="rt01", ifName="Te0/1"}
kizashi_band_upper{metric="kizashi:if_util_in:ratio5m", instance="rt01", ifName="Te0/1"}
kizashi_band_lower{metric="kizashi:if_util_in:ratio5m", instance="rt01", ifName="Te0/1"}
```

## Alert rules

[rules/kizashi-alerts.yml](rules/kizashi-alerts.yml) contains:

| Alert | Fires when | Severity |
|---|---|---|
| `OpticDegradationPredicted` | An optic is expected to reach its limit within 72h (for 30m) | warning |
| `ResourceExhaustionPredicted` | Any other metric is expected to reach its limit within 24h (for 30m) | warning |
| `MetricAnomalous` | Anomaly score above 6 (for 15m) | info |
| `KizashiInferenceStalled` | No completed inference cycle for 10 minutes | warning |
| `KizashiQueryErrors` | Queries for a target keep failing for 15 minutes | warning |
| `KizashiModelStale` | A baseline has not been retrained for 3 days | warning |

Predictive alerts carry the label `kizashi="predictive"`, so Alertmanager can route them
separately from outage alerts, for example to a ticket queue instead of the on-call pager.
Tune the thresholds and `for:` durations to your environment; a predictive alert that fires
too often is soon ignored.

## Running in production

### Scheduled training

Run `train` once a day, for example from cron or a Kubernetes CronJob. It writes models
atomically, and the running `serve` process picks them up on its next cycle. If a query
fails, `train` exits with status 1 and leaves the previous models in place.

```cron
15 3 * * *  kizashi train -c /etc/kizashi/kizashi.yaml
```

`serve` and `train` must share `models.dir`, for example through a shared volume.

### Docker

```sh
docker run -d --name kizashi -p 9850:9850 \
  -v /etc/kizashi:/etc/kizashi:ro \
  -v kizashi-models:/var/lib/kizashi/models \
  kizashi

docker run --rm \
  -v /etc/kizashi:/etc/kizashi:ro \
  -v kizashi-models:/var/lib/kizashi/models \
  kizashi train --config /etc/kizashi/kizashi.yaml
```

The image runs as an unprivileged user and expects models in `/var/lib/kizashi/models`. Set
`models.dir` to that path in your config.

### Scaling

One process handles all series by default. For large networks, run several replicas with the
same config and a different shard index each. Every series is assigned to exactly one shard
by a hash of its labels.

```sh
KIZASHI_SHARD_TOTAL=2 KIZASHI_SHARD_INDEX=0 kizashi serve -c kizashi.yaml
KIZASHI_SHARD_TOTAL=2 KIZASHI_SHARD_INDEX=1 kizashi serve -c kizashi.yaml
```

This requires the `${KIZASHI_SHARD_TOTAL}` / `${KIZASHI_SHARD_INDEX}` placeholders used in
the example config. Each replica still queries all series and discards the ones that belong
to other shards, so sharding spreads compute, not query load. Watch
`kizashi_inference_duration_seconds`; a cycle should finish well within `inference.interval`.

## Troubleshooting

**No `kizashi_anomaly_score` or band metrics.** Baselines are missing. Run `kizashi train` and
check its output: series with less than 2 days of history are skipped. Also check that
`models.dir` is the same for `train` and `serve`.

**Baselines stopped matching after a relabeling change.** Models are keyed by the full label
set of each series. When labels change, retrain.

**No `kizashi_time_to_threshold_seconds`.** This is expected unless a series is clearly trending
toward one of its thresholds within `trend.max_horizon`. Also check that `thresholds` resolve
for the series: with `by_label`, the label must exist and its value must be in `values`, or a
fallback `value` must be set.

**Predictions show KIZASHI's `instance` label instead of the device's.** Set
`honor_labels: true` on the scrape job.

**Log shows `inference cycle took …s, longer than interval`.** Reduce `trend.max_points`,
narrow targets with `match`, or add shards.

**Expected values are off by one hour after a daylight saving change.** Profiles are binned
by UTC hour. They realign once the next training run includes enough data from after the change.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| P0 | Foundation: recording rules, inference loop, `/metrics` | ✅ Done |
| P1 | Adaptive thresholds, trend forecast, alert rules | ✅ Done |
| P2 | Multivariate anomaly detection, log patterns, topology-aware alert grouping | ✅ Done |
| P3 | Feedback API and failure label collection | ✅ Done |
| P4 | Supervised failure probability with explanations, MLflow model registry | ✅ Done |
| P5 | Service health scores | ✅ Done |

All six phases are complete. See [docs/design.md](docs/design.md) §14 for details.

## Development

```sh
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The tests use an in-memory fake of the Prometheus HTTP API, so no running Prometheus is needed.

Project layout:

```
kizashi/
├── cli.py              # `kizashi serve` / `kizashi train` / `kizashi feedback`
├── config.py           # config loading and validation
├── inference.py        # inference loop
├── trainer.py          # baseline + failure model training job
├── exporter.py         # /metrics snapshot collector
├── feedback.py         # Feedback API (FastAPI) — operator labels + Alertmanager webhook
├── label_store.py      # JSONL append-only failure label store
├── health.py           # service health score (P5)
├── explain.py          # SHAP top-K contributors (P4)
├── llm.py              # Ollama LLM explanation (optional)
├── topology.py         # topology change detection
├── sources/
│   ├── prometheus.py
│   ├── loki.py         # Loki LogQL client
│   ├── alertmanager.py # silence exclusion
│   └── jira.py         # Jira Service Management client
└── models/
    ├── baseline.py     # layer 1
    ├── trend.py        # layer 2
    ├── multivariate.py # layer 3 — Isolation Forest
    ├── logpattern.py   # auxiliary — Drain log templates
    ├── failure.py      # layer 4 — LightGBM failure probability
    ├── registry.py     # MLflow model registry (optional)
    └── store.py        # model files, atomic read/write
rules/                  # Prometheus recording and alert rules
examples/               # example configs, services.yaml
deploy/                 # systemd, Helm chart, Alertmanager routing
dashboards/             # Grafana dashboard JSON
docs/design.md          # design document (Japanese + English)
docs/decisions/         # architectural decision records (ADR-001–005)
```

## License

GNU General Public License v3.0 (GPL-3.0-or-later) — Copyright (C) 2026 株式会社エージーネディア / agNedia Inc. — see [LICENSE](LICENSE).

---

<details>
<summary>日本語ドキュメント</summary>

# KIZASHI — Prometheus 向け AI 故障予兆検知

**KIZASHI**（兆し）は、Splunk ITSI 相当の適応しきい値・予兆アラート機能を Prometheus エコシステム上で実現するツールです。インターフェースや機器ごとに「平常」を学習し、値の逸脱を検知して「あと何時間でこの光パワーが限界値に達するか」を推定します。担当者がリンク断が起きる前に行動できます。

Prometheus 本体の改修は不要です。KIZASHI は Prometheus HTTP API でメトリクスを取得し、予測結果を通常のメトリクスとして `/metrics` で公開します。Prometheus がそれをスクレイプするだけで、既存のアラートルール・Alertmanager・Grafana をそのまま使えます。

```
SNMP / gNMI エクスポーター ──scrape──▶ Prometheus ──alerts──▶ Alertmanager
                                        │      ▲
                           query_range  │      │ scrape /metrics（予測結果）
                                        ▼      │
                                       KIZASHI ┘
                                       ├─ serve : 推論ループ（1分周期）
                                       └─ train : ベースライン学習（日次）
```

設計の全体像（4+1 層モデル、MLflow 連携、サービスヘルススコアなど）は [docs/design.md](docs/design.md) に記載されています。

## 目次

- [機能](#機能)
- [動作原理](#動作原理)
- [前提条件](#前提条件)
- [インストール](#インストール-1)
- [はじめに](#はじめに)
- [設定リファレンス](#設定リファレンス)
- [出力メトリクス一覧](#出力メトリクス一覧)
- [アラートルール](#アラートルール-1)
- [本番運用](#本番運用)
- [トラブルシューティング](#トラブルシューティング-1)
- [ロードマップ](#ロードマップ-1)
- [開発者向け](#開発者向け)

## 機能

| 層 | 答える問い | 状態 |
|---|---|---|
| 1. 適応しきい値 | 「この曜日・時間帯にしては異常か？」 | ✅ 利用可能 |
| 2. トレンド予測 | 「このままいくといつ限界値に達するか？」 | ✅ 利用可能 |
| 3. 多変量異常検知 | 「このメトリクスの組み合わせは異常か？」 | ✅ 利用可能 |
| 4. 故障確率予測 | 「この機器は今後24時間以内に故障する確率は？」 | ✅ 利用可能 |
| ログパターン異常（Loki） | 「新しい・異常なsyslogメッセージが出ているか？」 | ✅ 利用可能 |

その他:
- Prometheus recording rules（生 SNMP カウンターを特徴量に変換）
- 予兆アラートルール（OpticDegradationPredicted、ServiceHealthDegrading など）
- 水平シャーディングによるスケールアウト
- 再学習後のモデルをサービス無停止でホットリロード
- サービスヘルススコア（ITSI 風の冗長グループ考慮）
- SHAP による予兆の根拠説明
- Ollama 連携による自然言語説明（任意）

## 動作原理

### 第1層: 適応しきい値

「帯域使用率80%を超えたらアラート」のような固定しきい値は、業務時間帯の混雑で毎日発報し、深夜に急増した静かなリンクには反応しません。KIZASHI は系列ごとに **季節性プロファイル** を学習します。

- `kizashi train` は設定した学習期間（デフォルト28日）を読み込み、曜日×時間単位（168ビン）の中央値を計算します。2日〜2週間の履歴は日次（24ビン）を使用します。
- ばらつきは残差の **MAD（中央絶対偏差）** で表します。過去の障害による外れ値でベースラインが歪みにくい特性があります。
- 推論時は直近値を現在時刻の期待値と比較し、ロバスト z スコア `|実測値 − 期待値| / (1.4826 × MAD)` を算出します。0 前後が正常、3 以上で異常の目安です。

### 第2層: トレンド予測・しきい値到達時間

光パワーの劣化・メモリリーク・帯域逼迫など、**緩やかに進む故障** には「あと何時間で限界か」が重要です。

- 直近6時間に加重最小二乗回帰（WLS）を当て、信頼区間付きで推定します。傾きが全体的に限界値に向いている場合のみ到達時間を公開します。平坦・ノイジーな系列には出力しません。
- 各ホライゾン（1h、24h など）の予測値は10〜90%信頼区間とともに公開します。

### 第3層: 多変量異常検知

単体のメトリクスでは正常範囲内でも、**組み合わせが異常** なケースを捉えます（例: トラフィックは平常なのに CPU だけ高い）。機器の役割（コア / ディストリビューション / アクセス）× ベンダー機種ごとに Isolation Forest を学習します。

### 第4層: 故障確率予測

障害ラベルが故障モードあたり30件以上蓄積されると有効化します。LightGBM バイナリ分類器でホライゾン（1h / 24h / 7d）ごとに故障確率を出力します。SHAP で上位3件の寄与特徴量を `kizashi_top_contributor` メトリクスとして公開します。

### 補助層: ログパターン異常

Loki から syslog を取得し、Drain アルゴリズムでテンプレートに分類します。テンプレートごとの件数急増と、**過去に出現したことのない新規テンプレート** の出現を予兆シグナルとして扱います。

### サービスヘルススコア（P5）

ITSI の Service/KPI モデルに倣い、機器の異常スコアをサービス単位に集約します。冗長構成（`redundancy_group`）では、いずれか1本が正常なら減点しません。スコアは 0〜100 で、`kizashi_health_score{service, horizon}` として公開します。

## 前提条件

- Python 3.11 以降
- Prometheus 2.x / 3.x（Thanos Query、Mimir、VictoriaMetrics でも可）
- 学習用の履歴: 最低2日、理想は2週間以上。Prometheus のデータ保持期間が短い場合は `prometheus.long_term_url` で長期ストレージ（VictoriaMetrics 推奨）を指定してください。

## インストール

本番インストール（systemd / Docker / アップグレード）は [INSTALLATION.md](INSTALLATION.md) を参照してください。ソースからの簡易試用:

```sh
git clone <このリポジトリ> kizashi && cd kizashi
python -m venv .venv
.venv/bin/pip install .
```

Docker の場合:

```sh
docker build -t kizashi .
```

P4 の LightGBM / SHAP を使う場合は追加インストールが必要です:

```sh
pip install 'kizashi[p4]'          # LightGBM + SHAP
pip install 'kizashi[p4,mlflow]'   # + MLflow model registry
```

## はじめに

**1. recording rules を読み込む。** [rules/kizashi-recording.yml](rules/kizashi-recording.yml) が生の SNMP カウンターを `kizashi:if_in_errors:rate5m` などの特徴量に変換します。右辺のメトリクス名はご利用の snmp_exporter / gNMI エクスポーターに合わせて変更してください（記録後のメトリクス名はそのまま）。

**2. 設定ファイルを作成する。** サンプルをコピーして `prometheus.url` とターゲットを設定します:

```sh
cp examples/kizashi.yaml kizashi.yaml
```

**3. ベースラインを学習する。**（最初は省略可。ベースライン未学習の場合はトレンド層のみ動作します）

```sh
kizashi train -c kizashi.yaml
```

**4. 推論を試す。** `--once` を付けると1サイクル実行して結果を標準出力に表示します:

```sh
kizashi serve -c kizashi.yaml --once
```

**5. サービスを起動してスクレイプ設定を追加する。**

```sh
kizashi serve -c kizashi.yaml   # ポート 9850 で /metrics を公開
```

`prometheus.yml` に以下を追加してください:

```yaml
scrape_configs:
  - job_name: kizashi
    honor_labels: true      # 必須（下記参照）
    scrape_interval: 1m
    static_configs:
      - targets: ["kizashi:9850"]

rule_files:
  - rules/kizashi-recording.yml
  - rules/kizashi-alerts.yml
```

> **`honor_labels: true` は必須です。** 予測結果には元の系列のラベル（`instance="rt01"` など）が含まれます。これを設定しないと Prometheus が `instance` と `job` を KIZASHI 自身のもので上書きし、どの機器の予測かが分からなくなります。

## 設定リファレンス

`kizashi.yaml` では値の中で `${VAR}` や `${VAR:-default}` で環境変数を展開できます。期間は Prometheus 構文（`30s`、`5m`、`6h`、`28d` など）で記述します。完全なサンプルは [examples/kizashi.yaml](examples/kizashi.yaml) を参照してください。

### 主要オプション

| キー | デフォルト | 説明 |
|---|---|---|
| `prometheus.url` | 必須 | 推論用 Prometheus |
| `prometheus.long_term_url` | prometheus.url | `train` 用 API（Thanos / Mimir / VictoriaMetrics） |
| `server.listen` | `0.0.0.0:9850` | `/metrics` のリスンアドレス |
| `models.dir` | `./models` | 学習済みモデルの保存先 |
| `inference.interval` | `1m` | 推論ループの実行間隔 |
| `inference.window` | `6h` | トレンド回帰に使う直近ウィンドウ |
| `inference.shard.total` / `.index` | `1` / `0` | シャーディング（スケールアウト参照） |
| `training.period` | `28d` | ベースライン学習に使う期間 |
| `baseline.band_sigma` | `3` | 適応バンドの幅（ロバスト標準偏差の倍数） |
| `trend.max_horizon` | `30d` | これ以上先の到達時間は公開しない |

### ターゲット設定

```yaml
targets:
  - metric: kizashi:optic_rx_dbm:avg5m
    match: '{job="snmp"}'
    layers: [baseline, trend]
    thresholds:
      - name: rx_low
        direction: below
        by_label: transceiver_type
        values:
          10GBASE-LR:   -14.4
          100GBASE-LR4: -10.6
```

## 出力メトリクス一覧

| メトリクス | 説明 |
|---|---|
| `kizashi_expected_value` | 現在時刻の期待値 |
| `kizashi_band_lower` / `kizashi_band_upper` | 適応しきい値の下限 / 上限 |
| `kizashi_anomaly_score` | ロバスト z スコア（`layer` ラベル付き） |
| `kizashi_time_to_threshold_seconds` | 限界値到達までの推定秒数（`threshold` ラベル付き） |
| `kizashi_forecast_value` | 予測値（`horizon`、`quantile` ラベル付き） |
| `kizashi_failure_probability` | 故障確率 0〜1（`failure_mode`、`horizon` ラベル付き） |
| `kizashi_top_contributor` | 予兆の主要寄与特徴量（SHAP 値、上位3件） |
| `kizashi_health_score` | サービスヘルススコア 0〜100（`service`、`horizon` ラベル付き） |
| `kizashi_model_info` | 稼働中モデルの情報（値は常に1） |

カーディナリティ対策として `kizashi_forecast_value` と `kizashi_top_contributor` は異常スコア / 故障確率が一定値を超えた系列のみ出力します。

## アラートルール

[rules/kizashi-alerts.yml](rules/kizashi-alerts.yml) の主なアラート:

| アラート | 発報条件 | 重大度 |
|---|---|---|
| `OpticDegradationPredicted` | 光モジュールが72時間以内に限界値到達（30分継続） | warning |
| `ResourceExhaustionPredicted` | その他のメトリクスが24時間以内に限界値到達（30分継続） | warning |
| `MetricAnomalous` | 異常スコア > 6（15分継続） | info |
| `ServiceHealthDegrading` | サービスヘルススコア < 80（10分継続） | warning |
| `ServiceHealthCritical` | サービスヘルススコア < 50（5分継続） | critical |
| `KizashiInferenceStalled` | 推論サイクルが10分間完了しない | warning |
| `KizashiQueryErrors` | クエリエラーが15分継続 | warning |
| `KizashiModelStale` | ベースラインが3日間再学習されていない | warning |

予兆アラートには `kizashi="predictive"` ラベルが付いています。Alertmanager で障害アラートとは別ルート（Slack の別チャンネル、Jira 自動起票など）に振り分けられます。

## 本番運用

### 定期学習

`train` を1日1回実行してください（cron または Kubernetes CronJob）。モデルはアトミックに書き込まれ、稼働中の `serve` プロセスが次サイクルにホットリロードします。

```cron
15 3 * * *  kizashi train -c /etc/kizashi/kizashi.yaml
```

### スケールアウト（シャーディング）

大規模環境では同じ設定で複数レプリカを起動し、シャードインデックスを変えます:

```sh
KIZASHI_SHARD_TOTAL=2 KIZASHI_SHARD_INDEX=0 kizashi serve -c kizashi.yaml
KIZASHI_SHARD_TOTAL=2 KIZASHI_SHARD_INDEX=1 kizashi serve -c kizashi.yaml
```

### Kubernetes / Helm

```sh
helm install kizashi deploy/helm/ -f your-values.yaml
```

## トラブルシューティング

**`kizashi_anomaly_score` が出ない。** ベースラインが未学習です。`kizashi train` を実行して履歴が2日以上あることを確認してください。`models.dir` が `train` と `serve` で同じであることも確認してください。

**`kizashi_time_to_threshold_seconds` が出ない。** 系列が `trend.max_horizon` 内で明確に限界値に向いているときのみ出力します。`thresholds` の設定（`by_label` と `values`）が系列に解決されているかも確認してください。

**予測メトリクスに機器の `instance` ではなく KIZASHI の `instance` が付いている。** スクレイプジョブに `honor_labels: true` を設定してください。

**推論サイクルが interval より遅い。** `trend.max_points` を減らす、`match` でターゲットを絞る、またはシャードを追加してください。

## ロードマップ

| フェーズ | 内容 | 状態 |
|---|---|---|
| P0 | 基盤: recording rules、推論ループ、`/metrics` | ✅ 完了 |
| P1 | 適応しきい値、トレンド予測、アラートルール | ✅ 完了 |
| P2 | 多変量異常検知、ログパターン、トポロジ集約 | ✅ 完了 |
| P3 | Feedback API、障害ラベル収集 | ✅ 完了 |
| P4 | LightGBM 故障確率予測、SHAP 説明、MLflow 連携 | ✅ 完了 |
| P5 | サービスヘルススコア | ✅ 完了 |

全フェーズ実装済み。詳細は [docs/design.md](docs/design.md) §14 を参照してください。

## 開発者向け

```sh
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

テストは Prometheus HTTP API のインメモリフェイクを使うため、実際の Prometheus は不要です。P4 の LightGBM / SHAP テストは `pytest.importorskip` を使用しており、これらのライブラリが未インストールの場合は自動的にスキップされます。

## ライセンス

GNU General Public License v3.0 (GPL-3.0-or-later) — Copyright (C) 2026 株式会社エージーネディア / agNedia Inc. — [LICENSE](LICENSE) 参照。

</details>
