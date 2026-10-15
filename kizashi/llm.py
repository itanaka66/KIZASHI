"""Optional Ollama LLM client for natural-language alert explanations."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
あなたはネットワーク運用のエキスパートです。
予兆検知システム KIZASHI の異常情報をもとに、
1. 何が起きているかの簡潔な説明（1〜2文）
2. 推奨される確認・対処手順（箇条書き3〜5項目）
を日本語で出力してください。技術的に正確で、担当者がすぐに行動できる内容にしてください。"""


def _user_prompt(
    instance: str,
    if_name: str,
    failure_mode: str,
    anomaly_score: float,
    time_to_threshold_h: float,
    top_contributors: list[tuple[str, str, float]],
) -> str:
    lines = [
        f"機器: {instance}",
        f"インターフェース: {if_name}",
        f"異常の種類: {failure_mode}",
        f"異常スコア: {anomaly_score:.1f} / 10.0",
    ]
    if time_to_threshold_h < 999:
        lines.append(f"しきい値到達予測: {time_to_threshold_h:.0f}h 後")
    if top_contributors:
        lines.append("主な寄与要因:")
        for i, (feat, val, pct) in enumerate(top_contributors[:3], 1):
            lines.append(f"  {i}. {feat}: {val} (平均比 {pct:.0f}%)")
    return "\n".join(lines)


class LLMError(Exception):
    pass


@dataclass
class LLMConfig:
    url: str
    model: str = "qwen2.5:7b"
    timeout: float = 30.0
    max_tokens: int = 512


class OllamaClient:
    def __init__(self, cfg: LLMConfig) -> None:
        self._cfg = cfg
        self._base = cfg.url.rstrip("/")

    def explain(
        self,
        instance: str,
        if_name: str,
        failure_mode: str,
        anomaly_score: float,
        time_to_threshold_h: float = 999.0,
        top_contributors: list[tuple[str, str, float]] | None = None,
    ) -> str:
        """Return a Japanese explanation string, or raise LLMError."""
        user_msg = _user_prompt(
            instance, if_name, failure_mode, anomaly_score, time_to_threshold_h, top_contributors or []
        )
        payload = {
            "model": self._cfg.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "stream": False,
            "options": {"num_predict": self._cfg.max_tokens},
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self._base}/api/chat",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._cfg.timeout) as resp:
                result = json.loads(resp.read())
        except urllib.error.URLError as exc:
            raise LLMError(f"Ollama request failed: {exc}") from exc

        text = result.get("message", {}).get("content", "").strip()
        if not text:
            raise LLMError("Ollama returned empty content")
        return text


def try_explain(
    client: OllamaClient | None,
    instance: str,
    if_name: str,
    failure_mode: str,
    anomaly_score: float,
    time_to_threshold_h: float = 999.0,
    top_contributors: list[tuple[str, str, float]] | None = None,
) -> str:
    """Call explain(); return empty string on any error (LLM is optional)."""
    if client is None:
        return ""
    try:
        return client.explain(instance, if_name, failure_mode, anomaly_score, time_to_threshold_h, top_contributors)
    except LLMError as exc:
        logger.warning("llm: explanation failed: %s", exc)
        return ""
