# Installing KIZASHI

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
