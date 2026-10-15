# ADR-002: 光モジュール DOM 値のベンダーごとの正規化

**ステータス**: 決定済み  
**決定日**: 2026-10-15

---

## 背景

光モジュールの DOM（Digital Optical Monitoring）値（受光パワー・送光パワー・温度・バイアス電流）は、ベンダーごとに異なる MIB / gNMI パスで公開され、値の単位・スケールも異なる。KIZASHI の第 1・2 層は統一されたメトリクス名を期待するため、ベンダーの差異を吸収する正規化レイヤが必要。

対象ベンダー: **Cisco IOS/IOS-XE、Cisco NX-OS、Juniper Junos、Arista EOS**

---

## 決定: snmp_exporter モジュール + Prometheus recording rules の 2 段階正規化

### 正規化後の標準メトリクス名

| メトリクス | 単位 | 説明 |
|---|---|---|
| `kizashi:optic_rx_dbm:avg5m` | dBm | 受光パワー（recording rule から） |
| `kizashi:optic_tx_dbm:avg5m` | dBm | 送光パワー |
| `kizashi:optic_temp_celsius:avg5m` | ℃ | モジュール温度 |
| `kizashi:optic_bias_ma:avg5m` | mA | レーザーバイアス電流 |

すべて `avg_over_time(...[5m])` の 5 分平均とし、インスタンスラベルに `{instance, ifIndex, ifName, vendor}` を付与する。

---

## ベンダー別 MIB / gNMI マッピング

### Cisco IOS / IOS-XE — ENTITY-SENSOR-MIB

snmp_exporter は `ENTITY-SENSOR-MIB::entPhySensorValue` を取得する。  
- `entPhySensorType=15` (dBm) が光パワーと温度をカバー  
- `entPhySensorPrecision` が小数点位置を示す（典型値: 1 → 値を 10 で割る）

```yaml
# snmp_exporter generator.yml (cisco-ios モジュール)
modules:
  cisco_ios_optics:
    walk:
      - ENTITY-SENSOR-MIB::entPhySensorValue
      - ENTITY-SENSOR-MIB::entPhySensorType
      - ENTITY-SENSOR-MIB::entPhySensorPrecision
      - IF-MIB::ifDescr
    overrides:
      entPhySensorValue:
        type: gauge
```

recording rule（Prometheus 側）:

```yaml
# rules/snmp-normalization/cisco-ios.yml
groups:
  - name: kizashi-dom-cisco-ios
    interval: 1m
    rules:
      # Rx power (dBm): sensorType=15 かつ sensor名に "Rx" を含む
      - record: optic_rx_power_dbm_raw
        expr: |
          entPhySensorValue{job="snmp-cisco-ios", entPhySensorType="15"}
            * on(entPhySensorIndex) group_left(ifDescr)
            (entPhySensorPrecision == 1) / 10
        labels:
          vendor: cisco-ios

      - record: kizashi:optic_rx_dbm:avg5m
        expr: avg_over_time(optic_rx_power_dbm_raw{vendor="cisco-ios"}[5m])
```

> **注意**: IOS では sensor エントリに Rx/Tx の区別が entPhySensorDescr ラベルに入る。  
> snmp_exporter の metric_relabel_configs でフィルタする。詳細は `rules/snmp-normalization/cisco-ios.yml` 参照。

---

### Cisco NX-OS — CISCO-ENTITY-SENSOR-MIB

NX-OS は OID ブランチが異なる（`.1.3.6.1.4.1.9.9.91`）が構造は ENTITY-SENSOR-MIB と同様。

```yaml
# snmp_exporter generator.yml (cisco-nxos モジュール)
modules:
  cisco_nxos_optics:
    walk:
      - CISCO-ENTITY-SENSOR-MIB::entSensorValue
      - CISCO-ENTITY-SENSOR-MIB::entSensorType
      - CISCO-ENTITY-SENSOR-MIB::entSensorPrecision
      - IF-MIB::ifDescr
```

精度はほぼ `1`（/10）。recording rule は cisco-ios と同構造で `vendor="cisco-nxos"` に変更。

---

### Juniper Junos — JUNIPER-DOM-MIB

JUNIPER-DOM-MIB は直接 dBm 相当の値を持つが、**0.001 dBm 単位**（1000 倍）。

| OID / ラベル | 意味 | 変換 |
|---|---|---|
| `jnxDomCurrentRxLaserPower` | 受光パワー | ÷ 1000 → dBm |
| `jnxDomCurrentTxLaserOutputPower` | 送光パワー | ÷ 1000 → dBm |
| `jnxDomCurrentModuleTemperature` | モジュール温度 | ÷ 1000 → ℃ |
| `jnxDomCurrentTxLaserBiasCurrent` | バイアス電流 | ÷ 1000 → mA |

```yaml
# snmp_exporter generator.yml (juniper モジュール)
modules:
  juniper_optics:
    walk:
      - JUNIPER-DOM-MIB::jnxDomCurrentRxLaserPower
      - JUNIPER-DOM-MIB::jnxDomCurrentTxLaserOutputPower
      - JUNIPER-DOM-MIB::jnxDomCurrentModuleTemperature
      - JUNIPER-DOM-MIB::jnxDomCurrentTxLaserBiasCurrent
      - IF-MIB::ifDescr
    overrides:
      jnxDomCurrentRxLaserPower:
        type: gauge
```

recording rule:

```yaml
# rules/snmp-normalization/juniper.yml
- record: kizashi:optic_rx_dbm:avg5m
  expr: avg_over_time((jnxDomCurrentRxLaserPower / 1000)[5m])
  labels:
    vendor: juniper

- record: kizashi:optic_tx_dbm:avg5m
  expr: avg_over_time((jnxDomCurrentTxLaserOutputPower / 1000)[5m])
  labels:
    vendor: juniper

- record: kizashi:optic_temp_celsius:avg5m
  expr: avg_over_time((jnxDomCurrentModuleTemperature / 1000)[5m])
  labels:
    vendor: juniper

- record: kizashi:optic_bias_ma:avg5m
  expr: avg_over_time((jnxDomCurrentTxLaserBiasCurrent / 1000)[5m])
  labels:
    vendor: juniper
```

---

### Arista EOS — ENTITY-SENSOR-MIB (precision=4)

Arista は標準 ENTITY-SENSOR-MIB を使うが、`entPhySensorPrecision=4`（÷10,000）。

```yaml
# recording rule: Arista は precision=4
- record: kizashi:optic_rx_dbm:avg5m
  expr: |
    avg_over_time(
      (entPhySensorValue{job="snmp-arista", entPhySensorType="15"} / 10000)[5m]
    )
  labels:
    vendor: arista
```

---

## 代替手段: gNMI（推奨の将来方針）

Juniper と Arista は gNMI / OpenConfig モデルで DOM 値を直接 dBm として公開する。
gnmic を使う場合は MIB 変換不要で値がそのまま使えるため、新規環境では gNMI を推奨する。

| パス | 値 |
|---|---|
| `/interfaces/interface/transceiver/physical-channels/channel/state/input-power/instant` | dBm（float） |
| `/interfaces/interface/transceiver/physical-channels/channel/state/output-power/instant` | dBm（float） |
| `/interfaces/interface/transceiver/state/temperature/instant` | ℃ |

gNMI で取得したメトリクスにも同じ recording rule で `kizashi:optic_rx_dbm:avg5m` を生成すれば KIZASHI 側の変更は不要。

---

## モジュール型番ごとのしきい値管理

`examples/kizashi.yaml` の `thresholds.by_label` で対応:

```yaml
targets:
  - metric: kizashi:optic_rx_dbm:avg5m
    layers: [baseline, trend]
    thresholds:
      - name: rx_low
        direction: below
        by_label: transceiver_type   # snmp_exporter が付与するラベル
        values:
          10GBASE-LR:     -14.4   # datasheet minimum receiver sensitivity
          10GBASE-SR:      -9.9
          100GBASE-LR4:   -10.6
          100GBASE-CWDM4: -13.0
          400GBASE-DR4:   -11.0
```

`transceiver_type` ラベルは snmp_exporter の `metric_relabel_configs` で `entPhySensorDescr` または Juniper/Arista の該当 OID から付与する。

---

<details>
<summary>English</summary>

# ADR-002: Vendor-Specific Optical Module DOM Value Normalization

**Status**: Decided  
**Date**: 2026-10-15

## Background

Digital Optical Monitoring (DOM) values — receive power, transmit power, temperature, and bias current — are exposed by each vendor under different MIBs and gNMI paths, with different units and scaling factors. KIZASHI's Layer 1 and Layer 2 expect unified metric names, so a normalization layer is required.

Target vendors: **Cisco IOS/IOS-XE, Cisco NX-OS, Juniper Junos, Arista EOS**

## Decision: Two-stage normalization — snmp_exporter module + Prometheus recording rules

### Normalized standard metric names

| Metric | Unit | Description |
|---|---|---|
| `kizashi:optic_rx_dbm:avg5m` | dBm | Receive optical power (from recording rule) |
| `kizashi:optic_tx_dbm:avg5m` | dBm | Transmit optical power |
| `kizashi:optic_temp_celsius:avg5m` | °C | Module temperature |
| `kizashi:optic_bias_ma:avg5m` | mA | Laser bias current |

All are 5-minute averages (`avg_over_time(...[5m])`), labeled with `{instance, ifIndex, ifName, vendor}`.

## Vendor Mapping

### Cisco IOS / IOS-XE — ENTITY-SENSOR-MIB

snmp_exporter reads `entPhySensorValue`. `entPhySensorType=15` covers optical power and temperature. `entPhySensorPrecision=1` means divide by 10.

### Cisco NX-OS — CISCO-ENTITY-SENSOR-MIB

Different OID branch (`.1.3.6.1.4.1.9.9.91`) but same structure. Precision is typically 1 (÷10).

### Juniper Junos — JUNIPER-DOM-MIB

Values are in 0.001 dBm / 0.001 °C / 0.001 mA — divide by 1000.

| OID | Description | Conversion |
|---|---|---|
| `jnxDomCurrentRxLaserPower` | Receive power | ÷ 1000 → dBm |
| `jnxDomCurrentTxLaserOutputPower` | Transmit power | ÷ 1000 → dBm |
| `jnxDomCurrentModuleTemperature` | Temperature | ÷ 1000 → °C |
| `jnxDomCurrentTxLaserBiasCurrent` | Bias current | ÷ 1000 → mA |

### Arista EOS — ENTITY-SENSOR-MIB (precision=4)

Standard ENTITY-SENSOR-MIB but `entPhySensorPrecision=4` (÷10,000).

## Alternative: gNMI / OpenConfig (recommended for new environments)

Juniper and Arista expose DOM values directly in dBm via gNMI OpenConfig paths. No MIB conversion is needed; the same recording rules can generate the unified metric names.

```
/interfaces/interface/transceiver/physical-channels/channel/state/input-power/instant
```

## Per-module Threshold Management

Configured in `examples/kizashi.yaml` via `thresholds.by_label`:

```yaml
targets:
  - metric: kizashi:optic_rx_dbm:avg5m
    thresholds:
      - name: rx_low
        direction: below
        by_label: transceiver_type
        values:
          10GBASE-LR:     -14.4
          10GBASE-SR:      -9.9
          100GBASE-LR4:   -10.6
          100GBASE-CWDM4: -13.0
          400GBASE-DR4:   -11.0
```

The `transceiver_type` label is applied via `metric_relabel_configs` in snmp_exporter from `entPhySensorDescr` or the equivalent OID for Juniper / Arista.

</details>
