# TODO

実装済み: P0（基盤）、P1（第1層・第2層）。  
以下は未実装の残作業。

---

## P2 — 多変量異常検知・ログパターン（優先度: 高）

設計書 §5.3、§5.5 参照。

- [ ] `kizashi/models/multivariate.py` — 第3層: Isolation Forest による多変量異常検知
  - 機器の役割（コア/ディストリビューション/アクセス）× ベンダー機種ごとにグループ共有モデルを学習
  - 入力: 機器単位の特徴量ベクトル（CPU、メモリ、温度、全 IF のエラー率合計、ネイバー遷移数など）
  - 出力: `kizashi_anomaly_score{layer="multivariate"}`
  - 大規模環境向けオプションとして LSTM / Transformer オートエンコーダ（再構成誤差）も検討
- [ ] `kizashi/models/logpattern.py` — 補助層: Loki ログのパターン異常検知
  - Drain アルゴリズムでテンプレート分類（オンライン処理）
  - テンプレートごとの出現件数をメトリクス化 → 第1層・第4層の入力として利用
  - 過去未出現の新規テンプレート出現を予兆シグナルとして扱う
- [ ] `kizashi/sources/loki.py` — Loki HTTP クライアント（LogQL クエリ）
- [ ] トポロジ考慮のアラート集約
  - LLDP / CDP から `kizashi_topology_parent` メトリクスを生成する仕組み
  - Alertmanager `inhibit_rules` で上流障害時に配下の予兆を抑制
- [ ] P2 向けテスト追加（`tests/test_multivariate.py`、`tests/test_logpattern.py`）

完了条件: 誤報 ≤ 0.1 件 / 機器 / 週

---

## P3 — フィードバックループ（優先度: 高）

設計書 §8.3 参照。P4 の教師データ収集に直結するため早期着手推奨。

- [ ] `kizashi/feedback.py` — Feedback API（FastAPI）
  - Alertmanager webhook 受信エンドポイント
  - オペレーターの「正解 / 誤報 / 対応済み」判定を受け取る REST API
  - OIDC 認証の実装（設計書 §11）
- [ ] Feature Store への障害ラベル保存
  - `(device, interface, failure_mode, started_at)` 形式
  - S3 / MinIO 上の Parquet ファイルへアトミック書き込み
- [ ] Alertmanager 履歴から確定障害ラベルを自動収集する仕組み
  - `InterfaceDown`、`DeviceUnreachable` などの確定障害アラートを webhook で受信
- [ ] Grafana / Slack 通知ボタンとの連携（オペレーターが予兆アラートを判定するUI）
- [ ] P3 向けテスト追加（`tests/test_feedback.py`）

---

## P4 — 教師あり故障確率予測（優先度: 中）

設計書 §5.4 参照。P3 でラベルが 故障モードあたり 30 件以上蓄積してから有効化。

- [ ] `kizashi/models/failure.py` — 第4層: LightGBM による二値分類
  - 故障モードごとにモデルを分ける
  - 特徴量: 第1〜3層の出力 + 複数ウィンドウ（1h/6h/24h/7d）での統計量 + 機種・設置年数などの静的属性
  - 予測先: `horizon` = 1h / 24h / 7d
  - クラス不均衡対策: 負例ダウンサンプリング + クラス重み付け
  - 評価: Precision-Recall（Accuracy は使わない）
  - 確率較正: Isotonic Regression
  - 出力: `kizashi_failure_probability{failure_mode, horizon}`
- [ ] `kizashi/explain.py` — SHAP による寄与度算出
  - 上位3件の寄与特徴量を `kizashi_top_contributor{rank, feature}` として公開
- [ ] MLflow Model Registry 連携（現在はファイルベース）
  - モデルバージョン管理と評価指標の記録
  - "Production" 昇格フロー（バックテストで現行モデルを上回った場合のみ）
- [ ] シャドーモード実装
  - 新モデルを1週間アラートなしで推論のみ実行し、現行モデルと比較してから切り替え
- [ ] 学習ループ（`kizashi/trainer.py`）の拡張
  - 長期ストレージからの学習データ抽出と Parquet 化
  - 障害ラベルとの時間窓結合（故障の H 時間前までを正例）
  - ウォークフォワード検証（時間順分割）
  - 評価指標: Precision@閾値 ≥ 0.5、Recall ≥ 0.6、リードタイム中央値 ≥ 1h
- [ ] データドリフト検知（PSI）→ 閾値超過で再学習トリガー
- [ ] P4 向けテスト追加（`tests/test_failure.py`、`tests/test_explain.py`）

---

## P5 — サービスヘルススコア（優先度: 低）

設計書 §6 参照。

- [ ] `kizashi/health.py` — サービスヘルススコア算出
  - サービス定義 YAML（サービス → 構成機器・IF → 重み）の読み込み
  - ヘルススコア = 100 − Σ(重み × 正規化異常スコア)
  - 冗長構成の扱い: 冗長系がすべて劣化したときだけ大きく減点
  - 将来ヘルススコア（1h後）: 第4層の故障確率から期待値として算出
  - 出力: `kizashi_health_score{service, horizon}`
- [ ] サービス定義 YAML のスキーマ検証とサンプル追加
- [ ] `rules/kizashi-alerts.yml` に `ServiceHealthDegrading` アラートを追加
- [ ] P5 向けテスト追加（`tests/test_health.py`）

---

## 未決事項（設計書 §15）

実装を始める前に決定が必要な事項。

- [x] 長期ストレージの選定 → **VictoriaMetrics**（13ヶ月保持） `docs/decisions/001-long-term-storage.md`
- [x] 光モジュール DOM 値のベンダーごとの正規化方法 → snmp_exporter + Prometheus recording rules `docs/decisions/002-dom-normalization.md` / `rules/snmp-normalization/`
- [x] 障害チケットシステムとの連携方式 → **Jira Service Management REST API v3** `docs/decisions/003-jira-integration.md` / `kizashi/sources/jira.py`
- [x] 予兆アラートの通知先と対応フロー → Alertmanager routing + Slack/Jira/PagerDuty `docs/decisions/004-alert-routing.md` / `deploy/alertmanager/routing.yml`
- [x] LLM による予兆の自然言語説明 → **Ollama**（オプション機能） `docs/decisions/005-llm-explanation.md` / `kizashi/llm.py`

---

## インフラ・運用

- [x] Kubernetes / Helm Chart の作成（`deploy/helm/`）
- [x] Grafana ダッシュボード JSON の作成（`dashboards/`）
  - [x] 予兆ダッシュボード（時系列 + 適応バンド + 異常スコア）— `dashboards/kizashi-predictive.json`
  - [x] サービスヘルススコアダッシュボード — `dashboards/kizashi-health.json`（P5 完了後パネルが有効化）
- [x] メンテナンス期間の除外処理（Alertmanager silence 情報の参照）
  - `kizashi/sources/alertmanager.py` — AlertmanagerClient
  - `kizashi/config.py` に `AlertmanagerConfig(url, silence_buffer)` 追加
  - `kizashi/trainer.py` でサイレンス期間をマスクしてからベースライン学習
- [x] ネットワーク構成変更検知 → 該当機器のベースライン自動再学習
  - `kizashi/topology.py` — TopologyStore（シリーズキーの差分検知）
  - `kizashi train` 実行時に自動チェック・更新（`--no-topology` で無効化可）
