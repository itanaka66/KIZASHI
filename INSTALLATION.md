# Installing KIZASHI / KIZASHI インストールガイド

> 日本語ドキュメントは [このページの末尾](#インストールガイド日本語) にあります。

This guide installs the `kizashi` program. To connect it to your Prometheus, configure
targets, and turn on alerts, continue with [SETUP.md](SETUP.md) afterwards.

KIZASHI runs as two processes that share a model directory:

- `kizashi serve`: a long-running service that queries Prometheus every minute and exposes
  predictions on `/metrics` (port 9850 by default)
- `kizashi train`: a job that runs once a day and fits the baselines

Choose one installation method:

| Method | Use when |
|---|---|
| [Linux server with systemd](#linux-server-with-systemd) | Prometheus runs on VMs or bare metal (recommended) |
| [Docker](#docker) | You already run Prometheus and its exporters in containers |
| [Local trial](#local-trial) | You want to try KIZASHI from your workstation first |

Kubernetes manifests and a Helm chart are not provided yet. The Docker image can be used as a
basis; run `serve` as a Deployment and `train` as a CronJob that share a volume.

## Requirements

| Requirement | Details |
|---|---|
| Python | 3.11 or later (not needed for Docker) |
| Network access | KIZASHI must reach the Prometheus HTTP API, and Prometheus must reach KIZASHI on port 9850 |
| Disk | About 3.5 KB of model files per series with a baseline (350 MB for 100,000 series) |
| Memory | About 6 KB per series with a baseline, rising to about 10 KB while a retrained model is being loaded |
| Prometheus | 2.x or 3.x, or a compatible API (Thanos Query, Mimir, VictoriaMetrics) |

The per-series figures were measured for weekly baselines. Add headroom for the query results
of each inference cycle, which grow with `inference.window` and the number of series per target.

Check that the Prometheus API is reachable from the machine that will run KIZASHI:

```sh
curl -s 'http://prometheus.example:9090/api/v1/query?query=up' | head -c 200
# {"status":"success","data":{"resultType":"vector","result":[...
```

## Linux server with systemd

These steps install KIZASHI into `/opt/kizashi`, read the config from `/etc/kizashi`, and
store models in `/var/lib/kizashi`. Run them as root.

**1. Create a service user.**

```sh
useradd --system --no-create-home --shell /usr/sbin/nologin kizashi
```

**2. Install KIZASHI into its own virtual environment.**

```sh
python3 -m venv /opt/kizashi
/opt/kizashi/bin/pip install /path/to/kizashi   # a checkout of this repository
/opt/kizashi/bin/kizashi --help
```

**3. Install the config.**

```sh
install -d -m 755 /etc/kizashi
install -m 640 -g kizashi /path/to/kizashi/examples/kizashi.yaml /etc/kizashi/kizashi.yaml
```

Edit `/etc/kizashi/kizashi.yaml` and set `models.dir` to `/var/lib/kizashi/models`. The
example config already uses that path. Setting the Prometheus URL and targets is covered in
[SETUP.md](SETUP.md).

**4. Install the systemd units.**

```sh
cp /path/to/kizashi/deploy/systemd/kizashi.service \
   /path/to/kizashi/deploy/systemd/kizashi-train.service \
   /path/to/kizashi/deploy/systemd/kizashi-train.timer \
   /etc/systemd/system/
systemctl daemon-reload
```

| Unit | What it does |
|---|---|
| `kizashi.service` | Runs `kizashi serve` and restarts it on failure |
| `kizashi-train.service` | Runs `kizashi train` once |
| `kizashi-train.timer` | Starts `kizashi-train.service` every day at 03:15 |

systemd creates `/var/lib/kizashi` with the right owner on first start
(`StateDirectory=kizashi`).

Environment variables for the config, such as `KIZASHI_LONG_TERM_URL` or the shard settings,
go in `/etc/default/kizashi`. Both services read it if it exists:

```sh
# /etc/default/kizashi
KIZASHI_LONG_TERM_URL=http://thanos-query.example:9090
```

**5. Start the services** after finishing the configuration in [SETUP.md](SETUP.md):

```sh
systemctl start kizashi-train.service      # first training, takes a while
systemctl enable --now kizashi.service kizashi-train.timer
```

Logs:

```sh
journalctl -u kizashi.service -f
journalctl -u kizashi-train.service
```

## Docker

**1. Build the image** from a checkout of this repository:

```sh
docker build -t kizashi:0.1.0 .
```

The image runs as an unprivileged user (UID 10001). By default it runs
`kizashi serve --config /etc/kizashi/kizashi.yaml`.

**2. Prepare the config.**

```sh
mkdir -p /etc/kizashi
cp examples/kizashi.yaml /etc/kizashi/kizashi.yaml
```

Keep `models.dir: /var/lib/kizashi/models` (the default in the example config). That is
where the image expects models.

**3. Run the service** with a named volume for the models:

```sh
docker volume create kizashi-models

docker run -d --name kizashi --restart unless-stopped \
  -p 9850:9850 \
  -v /etc/kizashi:/etc/kizashi:ro \
  -v kizashi-models:/var/lib/kizashi/models \
  kizashi:0.1.0
```

**4. Schedule training** on the host, for example with cron:

```cron
15 3 * * *  docker run --rm -v /etc/kizashi:/etc/kizashi:ro -v kizashi-models:/var/lib/kizashi/models kizashi:0.1.0 train --config /etc/kizashi/kizashi.yaml
```

If Prometheus runs in a Docker network, attach KIZASHI to the same network with
`--network <name>` and use the container name in `prometheus.url`.

Pass environment variables for the config with `-e`, for example
`-e KIZASHI_LONG_TERM_URL=http://thanos-query:9090`.

## Local trial

```sh
git clone <repository URL> kizashi && cd kizashi
python3 -m venv .venv
.venv/bin/pip install .

cp examples/kizashi.yaml kizashi.yaml
# Edit prometheus.url, set models.dir to ./models, and reduce targets to one metric you have.
.venv/bin/kizashi serve -c kizashi.yaml --once
```

`--once` runs one inference cycle and prints the results instead of serving them. Nothing
needs to change in Prometheus for this.

## Verify the installation

With the service running:

```sh
curl -s http://localhost:9850/metrics | grep -E '^kizashi_(last_inference|inference_series|query_errors)'
```

Check that:

- `kizashi_last_inference_timestamp_seconds` is present and updates every minute
- `kizashi_inference_series` is greater than 0 for your targets
- `kizashi_query_errors_total` is absent or not increasing

If `kizashi_inference_series` is missing for a target, its query failed. The service log
shows the reason.

## Upgrading

```sh
# systemd
/opt/kizashi/bin/pip install --upgrade /path/to/kizashi
systemctl restart kizashi.service

# Docker
docker build -t kizashi:<new version> .
docker rm -f kizashi   # then run again with the new tag
```

Model files are kept across upgrades. If the release notes say the model format changed,
run training once after upgrading.

## Uninstalling

```sh
# systemd
systemctl disable --now kizashi.service kizashi-train.timer
rm /etc/systemd/system/kizashi.service /etc/systemd/system/kizashi-train.{service,timer}
systemctl daemon-reload
rm -rf /opt/kizashi /etc/kizashi /var/lib/kizashi /etc/default/kizashi
userdel kizashi

# Docker
docker rm -f kizashi
docker volume rm kizashi-models
```

Also remove the `kizashi` scrape job and the KIZASHI rule files from your Prometheus
configuration.

---

<details>
<summary id="インストールガイド日本語">インストールガイド（日本語）</summary>

## KIZASHI インストールガイド

このガイドでは `kizashi` プログラムのインストール方法を説明します。Prometheus との接続設定、ターゲットの設定、アラートの有効化は、インストール後に [SETUP.md](SETUP.md) を参照してください。

KIZASHI は**モデルディレクトリを共有する 2 つのプロセス**として動作します:

- `kizashi serve`: 常駐サービス。1 分ごとに Prometheus にクエリし、予測結果をポート 9850 の `/metrics` で公開します。
- `kizashi train`: 日次ジョブ。ベースラインと故障確率モデルを学習します。

インストール方法を選択してください:

| 方法 | 適するケース |
|---|---|
| [Linux サーバー（systemd）](#linux-サーバーsystemd) | Prometheus が VM またはベアメタルで動いている（推奨） |
| [Docker](#docker-1) | Prometheus と各エクスポーターをコンテナで動かしている |
| [ローカル試用](#ローカル試用) | まずワークステーションで試したい |

Kubernetes マニフェストと Helm Chart は `deploy/helm/` に含まれています。`serve` を Deployment、`train` を CronJob として、共有ボリュームとともにデプロイしてください。

## 前提条件

| 要件 | 詳細 |
|---|---|
| Python | 3.11 以降（Docker の場合は不要） |
| ネットワーク | KIZASHI から Prometheus HTTP API へのアクセス、Prometheus から KIZASHI のポート 9850 へのアクセスが必要 |
| ディスク | ベースライン1系列あたり約 3.5 KB（10 万系列で約 350 MB） |
| メモリ | 1 系列あたり約 6 KB（モデルリロード中は約 10 KB に増加） |
| Prometheus | 2.x または 3.x（Thanos Query / Mimir / VictoriaMetrics でも可） |

Prometheus API が KIZASHI を実行するマシンから到達できることを確認してください:

```sh
curl -s 'http://prometheus.example:9090/api/v1/query?query=up' | head -c 200
# {"status":"success","data":{"resultType":"vector","result":[...
```

## Linux サーバー（systemd）

KIZASHI を `/opt/kizashi` にインストールし、設定を `/etc/kizashi`、モデルを `/var/lib/kizashi` に保存します。root で実行してください。

**1. サービスユーザーを作成する。**

```sh
useradd --system --no-create-home --shell /usr/sbin/nologin kizashi
```

**2. 仮想環境に KIZASHI をインストールする。**

```sh
python3 -m venv /opt/kizashi
/opt/kizashi/bin/pip install /path/to/kizashi   # このリポジトリのチェックアウト
/opt/kizashi/bin/kizashi --help
```

P4 の LightGBM / SHAP 機能を使う場合:

```sh
/opt/kizashi/bin/pip install '/path/to/kizashi[p4]'
```

**3. 設定ファイルをインストールする。**

```sh
install -d -m 755 /etc/kizashi
install -m 640 -g kizashi /path/to/kizashi/examples/kizashi.yaml /etc/kizashi/kizashi.yaml
```

`/etc/kizashi/kizashi.yaml` を編集して `models.dir` を `/var/lib/kizashi/models` に設定します（サンプルではこのパスが既定値になっています）。Prometheus URL とターゲットの設定は [SETUP.md](SETUP.md) を参照してください。

**4. systemd ユニットをインストールする。**

```sh
cp /path/to/kizashi/deploy/systemd/kizashi.service \
   /path/to/kizashi/deploy/systemd/kizashi-train.service \
   /path/to/kizashi/deploy/systemd/kizashi-train.timer \
   /etc/systemd/system/
systemctl daemon-reload
```

| ユニット | 役割 |
|---|---|
| `kizashi.service` | `kizashi serve` を実行。失敗時に自動再起動 |
| `kizashi-train.service` | `kizashi train` を1回実行 |
| `kizashi-train.timer` | 毎日 03:15 に `kizashi-train.service` を起動 |

systemd は初回起動時に `StateDirectory=kizashi` で `/var/lib/kizashi` を適切なオーナーで作成します。

`KIZASHI_LONG_TERM_URL` やシャード設定などの環境変数は `/etc/default/kizashi` に記述します（両サービスが読み込みます）:

```sh
# /etc/default/kizashi
KIZASHI_LONG_TERM_URL=http://victoriametrics.example:8428
```

**5. [SETUP.md](SETUP.md) の設定が完了したらサービスを起動する。**

```sh
systemctl start kizashi-train.service      # 初回学習（少し時間がかかります）
systemctl enable --now kizashi.service kizashi-train.timer
```

ログの確認:

```sh
journalctl -u kizashi.service -f
journalctl -u kizashi-train.service
```

## Docker

**1. イメージをビルドする。**

```sh
docker build -t kizashi:0.1.0 .
```

イメージは非特権ユーザー（UID 10001）で動作します。デフォルトで `kizashi serve --config /etc/kizashi/kizashi.yaml` を実行します。

**2. 設定ファイルを準備する。**

```sh
mkdir -p /etc/kizashi
cp examples/kizashi.yaml /etc/kizashi/kizashi.yaml
```

`models.dir: /var/lib/kizashi/models` のまま（サンプル設定のデフォルト値）にしてください。

**3. 名前付きボリュームでサービスを起動する。**

```sh
docker volume create kizashi-models

docker run -d --name kizashi --restart unless-stopped \
  -p 9850:9850 \
  -v /etc/kizashi:/etc/kizashi:ro \
  -v kizashi-models:/var/lib/kizashi/models \
  kizashi:0.1.0
```

**4. ホスト上の cron で学習をスケジュールする。**

```cron
15 3 * * *  docker run --rm -v /etc/kizashi:/etc/kizashi:ro -v kizashi-models:/var/lib/kizashi/models kizashi:0.1.0 train --config /etc/kizashi/kizashi.yaml
```

Prometheus が Docker ネットワーク内にある場合は `--network <name>` で同じネットワークに接続し、`prometheus.url` にコンテナ名を使用してください。

## ローカル試用

```sh
git clone <リポジトリURL> kizashi && cd kizashi
python3 -m venv .venv
.venv/bin/pip install .

cp examples/kizashi.yaml kizashi.yaml
# prometheus.url を編集し、models.dir を ./models に設定し、ターゲットを1つに絞る
.venv/bin/kizashi serve -c kizashi.yaml --once
```

`--once` は推論を1サイクル実行して結果を表示します。Prometheus 側の変更は不要です。

## インストールの確認

サービス起動後:

```sh
curl -s http://localhost:9850/metrics | grep -E '^kizashi_(last_inference|inference_series|query_errors)'
```

以下を確認してください:

- `kizashi_last_inference_timestamp_seconds` が存在し、1分ごとに更新されている
- `kizashi_inference_series` がターゲットごとに 0 より大きい
- `kizashi_query_errors_total` がない、または増加していない

`kizashi_inference_series` がターゲットに対してない場合はクエリが失敗しています。サービスのログで原因を確認してください。

## アップグレード

```sh
# systemd
/opt/kizashi/bin/pip install --upgrade /path/to/kizashi
systemctl restart kizashi.service

# Docker
docker build -t kizashi:<新バージョン> .
docker rm -f kizashi   # その後、新しいタグで再起動
```

モデルファイルはアップグレード後も保持されます。リリースノートにモデルフォーマットの変更が記載されている場合は、アップグレード後に一度 `train` を実行してください。

## アンインストール

```sh
# systemd
systemctl disable --now kizashi.service kizashi-train.timer
rm /etc/systemd/system/kizashi.service /etc/systemd/system/kizashi-train.{service,timer}
systemctl daemon-reload
rm -rf /opt/kizashi /etc/kizashi /var/lib/kizashi /etc/default/kizashi
userdel kizashi

# Docker
docker rm -f kizashi
docker volume rm kizashi-models
```

Prometheus の設定から `kizashi` スクレイプジョブと KIZASHI のルールファイルも削除してください。

</details>
