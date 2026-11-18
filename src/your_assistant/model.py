"""OpenAI 兼容的 LLM 适配器。失败时返回 None，调用方必须能降级。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from .config import LlmSettings

log = logging.getLogger("your-assistant.model")


def _extract_json(text: str) -> dict[str, Any] | None:
    cleaned = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.MULTILINE).strip()
    candidates = [cleaned]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        candidates.append(cleaned[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


class HttpLlm:
    def __init__(self, settings: LlmSettings) -> None:
        self.settings = settings

    @property
    def available(self) -> bool:
        return self.settings.available

    def _chat(
        self, task: str, prompt: str, *, max_tokens: int = 12000, temperature: float = 0.2
    ) -> str | None:
        if not self.available:
            return None
        url = self.settings.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.settings.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是用户的个人助手，回答使用简体中文。"
                        "只依据给定材料，不编造；不确定的内容直接省略。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        proxy = self.settings.proxy or None
        try:
            with httpx.Client(
                proxy=proxy, trust_env=bool(proxy), timeout=self.settings.timeout
            ) as client:
                response = client.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {self.settings.api_key}"},
                )
                response.raise_for_status()
                data = response.json()
        except Exception as exc:  # noqa: BLE001 - 网络/模型问题一律降级
            log.warning("LLM 调用失败（%s）：%s: %s", task, type(exc).__name__, exc)
            return None
        message = (data.get("choices") or [{}])[0].get("message") or {}
        content = (message.get("content") or "").strip()
        return content or None

    def complete_text(self, task: str, prompt: str) -> str | None:
        return self._chat(task, prompt)

    def complete_json(self, task: str, prompt: str) -> dict[str, Any] | None:
        content = self._chat(task, prompt)
        return _extract_json(content) if content else None


def build_llm(settings: LlmSettings) -> HttpLlm | None:
    return HttpLlm(settings) if settings.available else None
