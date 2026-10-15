# ADR-005: LLM による予兆の自然言語説明（Ollama オプション機能）

**ステータス**: 決定済み  
**決定日**: 2026-10-15

---

## 背景

KIZASHI が出力する異常スコア・故障確率・寄与特徴量（SHAP）は数値のみであり、ネットワーク運用者が「なぜアラートが上がっているのか」「何をすべきか」を即座に把握しにくい。LLM による自然言語説明をオプション機能として追加することで、担当者のトリアージ時間を削減する。

---

## 決定: Ollama（ローカル LLM）をオプションで利用する

### 採用理由

| 観点 | Ollama（ローカル） | OpenAI API（クラウド） |
|---|---|---|
| データプライバシー | ◎ ネットワーク設計情報がクラウドに出ない | △ 機器名・IPが送信される |
| コスト | ◎ GPU サーバー初期費用のみ | △ トークン従量課金 |
| レイテンシ | ○ GPU あり: 1-3s / CPU: 5-15s | ◎ 1-2s |
| オフライン動作 | ◎ | ✗ |
| 導入の手軽さ | ◎ `docker run` 1コマンド | ◎ |

外部 API に機器名・IP・ベンダー情報を送ることを避けたい環境が多いため、ローカル LLM を第一選択とする。機能は完全オプションであり、`llm.url` が未設定の場合は説明文を生成しない。

---

## 推奨モデル

| モデル | 条件 | 説明品質 | 速度 |
|---|---|---|---|
| `qwen2.5:14b` | GPU (VRAM 16GB 以上) | ◎ 技術的説明が自然 | ◎ |
| `qwen2.5:7b` | CPU / GPU (VRAM 8GB) | ○ | ○ |
| `qwen2.5:3b` | メモリ 8GB 以下の環境 | △ | ◎ |
| `llama3.2:3b` | CPU のみ・省メモリ | △ | ◎ |

Qwen2.5 シリーズは日本語の精度が高く、ネットワーク機器の技術用語も扱いやすいため推奨。

---

## プロンプト設計

KIZASHI は以下の情報を Ollama に送り、日本語の説明文と対処手順を生成する。

### 入力コンテキスト

```
機器: {instance}
インターフェース: {ifName}
異常の種類: {failure_mode}
異常スコア: {anomaly_score:.1f} / 10.0
しきい値到達予測: {time_to_threshold_h:.0f}h 後
主な寄与要因:
  1. {contributor_1}: {value_1} (平均比 {pct_1}%)
  2. {contributor_2}: {value_2} (平均比 {pct_2}%)
  3. {contributor_3}: {value_3} (平均比 {pct_3}%)
```

### システムプロンプト

```
あなたはネットワーク運用のエキスパートです。
予兆検知システム KIZASHI の異常情報をもとに、
1. 何が起きているかの簡潔な説明（1-2文）
2. 推奨される確認・対処手順（箇条書き3-5項目）
を日本語で出力してください。
```

---

## KIZASHI 設定への追加

```yaml
# kizashi.yaml
llm:
  url: http://ollama:11434          # Ollama サーバーのURL
  model: qwen2.5:14b                # 使用モデル
  timeout: 30s                      # タイムアウト
  # max_tokens: 512                 # 最大生成トークン数
```

---

## 出力先

LLM 説明文は以下のルートで配信する:

1. **Slack 通知**: アラートの Alertmanager webhook 経由で `details` フィールドに追記
2. **Jira チケット**: description の末尾に「KIZASHI AI 説明」セクションとして追記
3. **Prometheus メトリクス**: テキスト値はメトリクスにはしない（cardinality 爆発回避）

---

## 実装

実装: `kizashi/llm.py`

---

## 参照

- `kizashi/llm.py` — Ollama クライアント実装
- `kizashi/sources/jira.py` — 説明文の Jira 書き込み
- `docs/decisions/003-jira-integration.md`

---

<details>
<summary>English</summary>

# ADR-005: LLM-Based Natural-Language Explanation (Ollama, Optional)

**Status**: Decided  
**Date**: 2026-10-15

## Background

KIZASHI outputs anomaly scores, failure probabilities, and SHAP top contributors as numbers. Network operators benefit from a concise natural-language summary of "why is this alert firing?" and "what should I do?" to reduce triage time.

## Decision: Ollama (local LLM) as an optional feature

### Rationale

| Criterion | Ollama (local) | OpenAI API (cloud) |
|---|---|---|
| Data privacy | ◎ Device names and IPs never leave the network | △ Sent to third party |
| Cost | ◎ One-time GPU server cost | △ Per-token billing |
| Latency | ○ GPU: 1–3 s / CPU: 5–15 s | ◎ 1–2 s |
| Offline operation | ◎ | ✗ |
| Ease of deployment | ◎ `docker run` single command | ◎ |

Local LLM is the first choice to avoid exposing device names, IPs, and vendor information to external APIs. The feature is completely optional: if `llm.url` is not configured, no explanation is generated.

Implementation: `kizashi/llm.py`

## Recommended Models

| Model | Requirements | Quality | Speed |
|---|---|---|---|
| `qwen2.5:14b` | GPU (≥16 GB VRAM) | ◎ Natural technical explanations | ◎ |
| `qwen2.5:7b` | CPU / GPU (8 GB VRAM) | ○ | ○ |
| `qwen2.5:3b` | ≤8 GB RAM | △ | ◎ |
| `llama3.2:3b` | CPU only, low memory | △ | ◎ |

Qwen2.5 handles Japanese and network engineering terminology well.

## Prompt Design

### Input context sent to Ollama

```
Device: {instance}
Interface: {ifName}
Anomaly type: {failure_mode}
Anomaly score: {anomaly_score:.1f} / 10.0
Estimated time to threshold: {time_to_threshold_h:.0f} h
Top contributing factors:
  1. {contributor_1}: {value_1} ({pct_1}% above average)
  2. {contributor_2}: {value_2} ({pct_2}% above average)
  3. {contributor_3}: {value_3} ({pct_3}% above average)
```

### System prompt

```
You are a network operations expert. Based on the KIZASHI anomaly data,
output in Japanese (or English as configured):
1. A concise explanation of what is happening (1–2 sentences)
2. Recommended inspection / remediation steps (3–5 bullet points)
```

## KIZASHI Configuration

```yaml
llm:
  url: http://ollama:11434
  model: qwen2.5:14b
  timeout: 30s
  # max_tokens: 512
```

## Output Destinations

1. **Slack notification**: appended to the `details` field via the Alertmanager webhook payload.
2. **Jira ticket**: added as a "KIZASHI AI Explanation" section in the ticket description.
3. **Prometheus metrics**: not stored as metrics (would cause cardinality explosion).

## References

- `kizashi/llm.py` — Ollama client implementation
- `kizashi/sources/jira.py` — writes explanation to Jira
- `docs/decisions/003-jira-integration.md`

</details>
