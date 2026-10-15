# TODO

実装済み: P0（基盤）、P1（第1層・第2層）。  
以下は未実装の残作業。

---

## P2 — 多変量異常検知・ログパターン（優先度: 高）

設計書 §5.3、§5.5 参照。

- [x] `kizashi/models/multivariate.py` — 第3層: Isolation Forest による多変量異常検知
  - グループキー（role × vendor）ごとに IsolationForest を学習・スコアリング（decision_function で0〜10スケール）
  - NaN は列中央値で補完、特徴量行列は `build_feature_matrix()` で組み立て
  - ModelStore に `save_multivariate` / `load_multivariate` 追加（JSON メタデータ + joblib バイナリ）
- [x] `kizashi/models/logpattern.py` — 補助層: Loki ログのパターン異常検知
  - Drain アルゴリズム（固定深さ前置木）によるオンラインテンプレート分類
  - `mark_known()` / `iter_new()` で新規テンプレート検出、JSON でアトミック永続化
- [x] `kizashi/sources/loki.py` — Loki HTTP クライアント（LogQL クエリ）
  - `query_range` / `label_values` を stdlib のみで実装（追加依存なし）
- [x] トポロジ考慮のアラート集約
  - `rules/kizashi-topology.yml` — LLDP/CDP から `kizashi_topology_parent{instance, parent_instance}` 生成
  - `rules/kizashi-alerts.yml` に `OpticDegradationPredictedWithTopology` 追加（`group_left(parent_instance)`）
  - Alertmanager routing.yml に `equal: [parent_instance]` の inhibit_rules 追加済み（ADR-004）
- [x] P2 向けテスト追加（`tests/test_multivariate.py` 22件、`tests/test_logpattern.py` 24件、計46件）

完了条件: 誤報 ≤ 0.1 件 / 機器 / 週

---

## P3 — フィードバックループ（優先度: 高）

設計書 §8.3 参照。P4 の教師データ収集に直結するため早期着手推奨。

- [x] `kizashi/feedback.py` — Feedback API（FastAPI）
  - Alertmanager webhook 受信エンドポイント（KIZASHI predictive / 確定障害 / resolved を自動分類）
  - オペレーターの「正解 / 誤報 / 対応済み」判定を受け取る REST API
  - Bearer トークン認証（env var）。OIDC は将来オプション
- [x] Feature Store への障害ラベル保存（`kizashi/label_store.py`）
  - `(fingerprint, device, interface, failure_mode, started_at, label)` 形式
  - JSONL アペンドオンリー（Parquet/S3 は将来オプション）
- [x] Alertmanager 履歴から確定障害ラベルを自動収集する仕組み
  - `InterfaceDown`、`DeviceUnreachable` などの確定障害アラートを webhook で受信 → `confirmed_failure` ラベル自動付与
- [x] `kizashi feedback` CLI サブコマンド追加（`kizashi/cli.py`）
- [x] `FeedbackConfig` を `kizashi/config.py` に追加（`examples/kizashi.yaml` に Alertmanager routing 例付きコメント）
- [x] P3 向けテスト追加（`tests/test_feedback.py`、26件）
- [ ] Grafana / Slack 通知ボタンとの連携（オペレーターが予兆アラートを判定するUI）— 将来オプション

---

## P4 — 教師あり故障確率予測（優先度: 中）

設計書 §5.4 参照。P3 でラベルが 故障モードあたり 30 件以上蓄積してから有効化。

- [x] `kizashi/models/failure.py` — 第4層: LightGBM による二値分類
  - 故障モードごとにモデルを分ける
  - 特徴量: 第1〜3層の出力 + 複数ウィンドウ（1h/6h/24h/7d）での統計量 + 機種・設置年数などの静的属性
  - 予測先: `horizon` = 1h / 24h / 7d
  - クラス不均衡対策: 負例ダウンサンプリング + クラス重み付け
  - 評価: Precision-Recall（Accuracy は使わない）
  - 確率較正: Isotonic Regression
  - 出力: `kizashi_failure_probability{failure_mode, horizon}`
- [x] `kizashi/explain.py` — SHAP による寄与度算出
  - 上位3件の寄与特徴量を `kizashi_top_contributor{rank, feature}` として公開
- [x] MLflow Model Registry 連携（`kizashi/models/registry.py`）
  - モデルバージョン管理と評価指標の記録
  - "Production" 昇格フロー（バックテストで現行モデルを上回った場合のみ）
  - `NoOpRegistry` でオプション依存を回避
- [x] シャドーモード実装
  - PSI ≥ 0.2 の場合、新モデルを shadow フラグ付きで保存（アラートなし推論のみ）
  - `FailureModelMeta.is_shadow` + `ModelStore.save_failure(shadow=True)` で管理
- [x] 学習ループ（`kizashi/trainer.py`）の拡張 — `train_failure_models()`
  - 障害ラベルとの時間窓結合（故障の H 時間前までを正例）
  - ウォークフォワード検証（時間順分割）
  - 評価指標: Precision@閾値 ≥ 0.5、Recall ≥ 0.6、リードタイム中央値 ≥ 1h
- [x] データドリフト検知（PSI）→ 閾値超過で再学習トリガー（`psi_from_percentiles`）
- [x] P4 向けテスト追加（`tests/test_failure.py` 30件、`tests/test_explain.py` 17件）

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
