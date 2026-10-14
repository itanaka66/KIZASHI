# Setting up KIZASHI

This guide connects an installed KIZASHI to your Prometheus, from input metrics to
predictive alerts. If KIZASHI is not installed yet, start with
[INSTALLATION.md](INSTALLATION.md).

Paths assume the systemd installation (`/etc/kizashi/kizashi.yaml`). Adjust them for Docker or
a local trial.

## Overview

| Step | What you do | Where |
|---|---|---|
| 1 | Make the input metrics available | Exporters |
| 2 | Load the recording rules | Prometheus |
| 3 | Backfill history for the recorded metrics (optional) | Prometheus |
| 4 | Configure targets and thresholds | `kizashi.yaml` |
| 5 | Run the first training | KIZASHI |
| 6 | Start KIZASHI and scrape it | KIZASHI, Prometheus |
| 7 | Turn on alerts and route them | Prometheus, Alertmanager |
| 8 | Add Grafana panels | Grafana |
| 9 | Tune during the first weeks | All |

Allow about two weeks between step 2 and full accuracy, unless you backfill in step 3.
Baselines need at least 2 days of history for an hour-of-day profile, and 2 weeks for the more
accurate hour-of-week profile. The trend layer works immediately.

## Step 1: Make the input metrics available

KIZASHI analyzes the metrics produced by the recording rules in
[rules/kizashi-recording.yml](rules/kizashi-recording.yml). Those rules read the following
source metrics:

| Recorded metric | Source metric | Typical exporter |
|---|---|---|
| `kizashi:if_in_errors:rate5m` | `ifInErrors` | snmp_exporter, `if_mib` module |
| `kizashi:if_out_discards:rate5m` | `ifOutDiscards` | snmp_exporter, `if_mib` module |
| `kizashi:if_util_in:ratio5m` | `ifHCInOctets`, `ifHighSpeed` | snmp_exporter, `if_mib` module |
| `kizashi:if_util_out:ratio5m` | `ifHCOutOctets`, `ifHighSpeed` | snmp_exporter, `if_mib` module |
| `kizashi:if_flaps:changes1h` | `ifOperStatus` | snmp_exporter, `if_mib` module |
| `kizashi:optic_rx_dbm:avg5m` | `optic_rx_power_dbm` | Vendor-specific, see below |
| `kizashi:optic_tx_dbm:avg5m` | `optic_tx_power_dbm` | Vendor-specific, see below |

The interface metrics come from the standard `if_mib` module of
[snmp_exporter](https://github.com/prometheus/snmp_exporter). Check that they exist:

```promql
count by (job) (ifHCInOctets)
```

Series must carry an interface name label (`ifName` in the examples). The default `if_mib`
module adds it through a lookup on `ifIndex`.

### Optical power

Optical power is not in a standard MIB that every vendor implements in the same way. Depending
on your devices, it comes from:

- **gNMI streaming telemetry** (for example with gnmic), using the OpenConfig
  `openconfig-platform-transceiver` model. Values are already in dBm.
- **SNMP**, using `ENTITY-SENSOR-MIB` or a vendor MIB (for example `CISCO-ENTITY-SENSOR-MIB`).
  Values often need scaling, and sensors are indexed by entity rather than by interface.

Whatever the source, adapt the `kizashi:optic_*` rules so that the recorded metric:

1. **is in dBm.** If your source reports milliwatts, convert with `10 * log10(<metric>)`. If it
   reports scaled integers (for example tenths of a dBm), divide accordingly.
2. **has `instance` and `ifName` labels** that match the interface metrics.
3. **has a `transceiver_type` label** if you want per-type limits (step 4). It usually comes
   from an inventory or info metric, joined in the recording rule:

```yaml
- record: kizashi:optic_rx_dbm:avg5m
  expr: |
    avg_over_time(optic_rx_power_dbm[5m])
      * on (instance, ifName) group_left (transceiver_type)
    max by (instance, ifName, transceiver_type) (last_over_time(transceiver_info[1d]))
```

`transceiver_info` stands for any metric with value 1 that describes the module in each port.
Without such a metric, drop the join and use a single fixed limit in step 4.

You do not need every metric in the table. Remove the rules and targets you have no data for.

## Step 2: Load the recording rules

Copy the rule files next to your Prometheus configuration and add them to `prometheus.yml`:

```yaml
rule_files:
  - rules/kizashi-recording.yml
```

Load only the recording rules for now. The alert rules come in step 7, once predictions exist.

Validate and reload:

```sh
promtool check rules rules/kizashi-recording.yml
curl -X POST http://prometheus.example:9090/-/reload   # requires --web.enable-lifecycle
# or: systemctl reload prometheus
```

Check that the recorded metrics appear (it can take a few minutes):

```promql
count by (__name__) ({__name__=~"kizashi:.*"})
```

## Step 3: Backfill history (optional)

Prometheus records the new metrics only from the moment the rules are loaded. Training then has
to wait for history to build up. If the source metrics already have weeks of history in
Prometheus, you can compute the recorded metrics for the past and start with full accuracy.

1. Note the time when the rules were loaded in step 2, for example `2026-10-14T09:00:00Z`.
2. Generate blocks for the past 4 weeks up to that time:

   ```sh
   promtool tsdb create-blocks-from rules \
     --url=http://prometheus.example:9090 \
     --start=$(date -u -d '28 days ago' +%s) \
     --end=2026-10-14T09:00:00Z \
     --output-dir=backfill \
     rules/kizashi-recording.yml
   ```

3. Move the generated block directories into the Prometheus data directory (the
   `--storage.tsdb.path` of your Prometheus) and restart Prometheus to load them:

   ```sh
   mv backfill/* /var/lib/prometheus/            # your --storage.tsdb.path
   chown -R prometheus:prometheus /var/lib/prometheus
   systemctl restart prometheus
   ```

   Prometheus 2.38 and earlier also need `--storage.tsdb.allow-overlapping-blocks`.

Backfilling evaluates every rule once per minute over the whole period. For large networks, run
it outside business hours and consider a shorter `--start`.

If training reads from long-term storage (Thanos, Mimir) instead of Prometheus, backfill
there, following the documentation of that system.

## Step 4: Configure targets and thresholds

Edit `/etc/kizashi/kizashi.yaml`, starting from [examples/kizashi.yaml](examples/kizashi.yaml).
All options are listed in the [README](README.md#configuration). The settings you must review:

**Prometheus URLs.**

```yaml
prometheus:
  url: http://prometheus.example:9090
  # Training reads 28 days of history. If Prometheus keeps less, point this at long-term storage.
  long_term_url: http://thanos-query.example:9090
```

**Targets.** One entry per recorded metric you have data for. Use `match` to limit a target to
part of your network, for example during a trial:

```yaml
targets:
  - metric: kizashi:optic_rx_dbm:avg5m
    match: '{job="snmp-core"}'
    layers: [baseline, trend]
```

**Thresholds.** Use `baseline` for metrics where "unusual" is what matters (error rates, flaps).
Add `trend` and `thresholds` for metrics with a hard limit (optical power, utilization).

For optical power, use the receiver sensitivity from the datasheet of each module type, plus a
safety margin. A link starts dropping frames near the sensitivity limit, so predict the time to
reach the margin, not the limit itself:

```yaml
    thresholds:
      - name: rx_low
        direction: below
        by_label: transceiver_type
        values:
          # Datasheet receiver sensitivity + 2 dB margin. Replace with your modules' values.
          10GBASE-LR: -12.4
          100GBASE-LR4: -8.6
        value: -10.0   # fallback for module types not listed
```

The values in `examples/kizashi.yaml` are placeholders, not recommendations.

**Check the config.** This runs one inference cycle and prints the results without starting
the service:

```sh
sudo -u kizashi /opt/kizashi/bin/kizashi serve -c /etc/kizashi/kizashi.yaml --once
```

| Output | Meaning |
|---|---|
| Lines starting with `kizashi_` | Working. Without training, only trend output appears. |
| No output, no errors | Queries returned no current data. Check the metric names and `match`. |
| `query failed for …` | Prometheus unreachable or a bad query. The message shows the reason. |
| `environment variable … is not set` | Set it in `/etc/default/kizashi`, or give the placeholder a default (`${VAR:-default}`). |

Seeing no `kizashi_time_to_threshold_seconds` lines is normal. They appear only when a series
is clearly trending toward a limit.

## Step 5: Run the first training

```sh
systemctl start kizashi-train.service
journalctl -u kizashi-train.service
```

The log shows the result for each target:

```
trained kizashi:optic_rx_dbm:avg5m: 1480 series fitted, 52 skipped (insufficient history)
```

Series with less than 2 days of history are skipped. They are picked up by later runs, and the
daily timer (`kizashi-train.timer`) keeps all baselines current.

## Step 6: Start KIZASHI and scrape it

```sh
systemctl enable --now kizashi.service kizashi-train.timer
curl -s http://localhost:9850/metrics | grep -c '^kizashi_'
```

Add the scrape job to `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: kizashi
    honor_labels: true
    scrape_interval: 1m
    static_configs:
      - targets: ["kizashi.example:9850"]
```

`honor_labels: true` is required. Predictions carry the `instance` and `job` labels of the
device they describe. Without it, Prometheus replaces them with KIZASHI's own, and every
prediction appears to be about KIZASHI.

If you run several shards (see the [README](README.md#scaling)), list every replica as a target.

Reload Prometheus and check:

```promql
up{job="kizashi"}                                   # 1
time() - kizashi_last_inference_timestamp_seconds   # under 120
kizashi_anomaly_score                               # one series per trained, reporting series
```

## Step 7: Turn on alerts and route them

Add the alert rules to `prometheus.yml` and reload:

```yaml
rule_files:
  - rules/kizashi-recording.yml
  - rules/kizashi-alerts.yml
```

```sh
promtool check rules rules/kizashi-alerts.yml
```

The alerts are described in the [README](README.md#alert-rules). Predictive alerts carry the
label `kizashi="predictive"`. They mean "this will need attention in hours or days," not
"something is broken now," so route them away from the on-call pager. Example
`alertmanager.yml`:

```yaml
route:
  receiver: oncall-pager
  group_by: [alertname, instance]
  routes:
    # Predictions: one notification per interface, repeated once a day.
    - matchers: ['kizashi="predictive"']
      receiver: network-tickets
      group_by: [alertname, instance, ifName]
      group_wait: 5m
      group_interval: 1h
      repeat_interval: 24h
    # KIZASHI's own health
    - matchers: ['alertname=~"Kizashi.*"']
      receiver: monitoring-team

inhibit_rules:
  # Once an interface is actually down, its predictions are no longer useful.
  - source_matchers: ['alertname="InterfaceDown"']
    target_matchers: ['kizashi="predictive"']
    equal: [instance, ifName]

receivers:
  - name: oncall-pager
    # ...
  - name: network-tickets
    # ...
  - name: monitoring-team
    # ...
```

Replace `InterfaceDown` with the name of your existing link-down alert. Check the routing with
amtool:

```sh
amtool check-config alertmanager.yml
amtool config routes test --config.file=alertmanager.yml alertname=OpticDegradationPredicted kizashi=predictive
# network-tickets
```

During maintenance windows, silence the affected devices in Alertmanager as usual.

## Step 8: Add Grafana panels

There is no packaged dashboard yet. These panels cover the common questions, using the
Prometheus data source and dashboard variables `$instance` and `$ifName`.

**Interface with its adaptive band** (time series panel, three queries):

```promql
kizashi:if_util_in:ratio5m{instance="$instance", ifName="$ifName"}
kizashi_band_upper{metric="kizashi:if_util_in:ratio5m", instance="$instance", ifName="$ifName"}
kizashi_band_lower{metric="kizashi:if_util_in:ratio5m", instance="$instance", ifName="$ifName"}
```

Use the Grafana "Fill below to" override to shade the area between the bands.

**Optics expected to degrade** (table panel, instant query, sorted ascending):

```promql
kizashi_time_to_threshold_seconds{threshold="rx_low"}
```

Set the unit to "seconds" so Grafana shows "2.8 days" instead of raw numbers.

**Most anomalous interfaces now** (table or bar gauge panel, instant query):

```promql
topk(20, kizashi_anomaly_score{layer="baseline"})
```

**Forecast with its confidence band** (time series panel):

```promql
kizashi_forecast_value{instance="$instance", ifName="$ifName", horizon="24h"}
```

Forecasts exist only for series that are trending toward a limit or are anomalous, so this
panel is often empty. That is expected.

## Step 9: Tune during the first weeks

Predictive alerts are only useful if people trust them. Review them weekly for the first month.

**Too many `MetricAnomalous` alerts.** Raise the alert threshold in `rules/kizashi-alerts.yml`
(default: score above 6), or widen the `for:` duration. If training had less than 2 weeks of
history, the baselines are hour-of-day profiles that do not know weekends are quieter. This
improves by itself once 2 weeks of history exist.

**Predictions that never came true.** Look at the forecast for the affected series. Short-lived
drops, such as during a fiber cleaning, can produce a trend for a few hours. Increase `for:` on
`OpticDegradationPredicted`, or lengthen `inference.window` so a longer history is fitted.

**Failures without a prediction.** Check that the series had a matching threshold. Remember
that the trend layer only predicts gradual degradation; sudden failures are out of its scope.

**Planned changes.** After hardware replacement or a large traffic change, the baselines of the
affected interfaces are wrong until enough new history exists. Expect anomaly alerts for those
interfaces for a few days, and silence them if needed.

## Verification checklist

| Check | How |
|---|---|
| Recorded metrics exist | `count by (__name__) ({__name__=~"kizashi:.*"})` returns your targets |
| Training ran | `kizashi_model_trained_timestamp_seconds` exists for each baseline target |
| Inference is current | `time() - kizashi_last_inference_timestamp_seconds < 120` |
| No query errors | `increase(kizashi_query_errors_total[1h]) == 0` or no result |
| Labels are preserved | `kizashi_anomaly_score` has device `instance` values, not KIZASHI's address |
| Alerts are loaded | Prometheus UI → Alerts lists the `kizashi-predictive` and `kizashi-self` groups |
| Routing is correct | `amtool config routes test` sends predictive alerts to the right receiver |
| Training is scheduled | `systemctl list-timers kizashi-train.timer` shows the next run |
