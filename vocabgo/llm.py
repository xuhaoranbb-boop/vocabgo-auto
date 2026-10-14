# -*- coding: utf-8 -*-
"""LLM 兜底与读图（DeepSeek / OpenAI / Ollama …，只要是 OpenAI 兼容接口）。

DeepSeek 两个要点（2026 年的现状，别用老写法）：
  * 模型名：`deepseek-chat` / `deepseek-reasoner` 已于 2026-07-24 停用，
    现在是 `deepseek-v4-flash`（原 chat 行为）/ `deepseek-v4-pro`；
    思考模式拆成了独立参数（`thinking`），V4 默认开启，做短答提取建议关掉。
  * 图片：`deepseek-flash` 支持图片输入（识别截图文字、描述图片），
    所以同一把 key 既能当答题兜底、也能当读图后端。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

import requests

from . import net
from .matcher import display_stem, topic_mode, topic_options, topic_remark, topic_stem

# provider 预设：填了 provider 就只需再填 key
# short = 界面上显示的中文名；配置里存的一律是 key（deepseek / openai / …）
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {
        "url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "vision_model": "deepseek-flash",
        "thinking": "off",
        "env_key": "DEEPSEEK_API_KEY",
        "label": "DeepSeek（答题 + 读图都能干）",
        "short": "DeepSeek（推荐）",
    },
    "openai": {
        "url": "https://api.openai.com",
        "model": "gpt-4o-mini",
        "vision_model": "gpt-4o-mini",
        "thinking": "auto",
        "env_key": "OPENAI_API_KEY",
        "label": "OpenAI",
        "short": "OpenAI",
    },
    "ollama": {
        "url": "http://127.0.0.1:11434",
        "model": "qwen2.5:7b",
        "vision_model": "qwen2.5vl",
        "thinking": "auto",
        "env_key": "",
        "label": "本地 Ollama（不联网、不花钱）",
        "short": "本地 Ollama（免费离线）",
    },
    "custom": {
        "url": "",
        "model": "",
        "vision_model": "",
        "thinking": "auto",
        "env_key": "",
        "label": "自定义（任意 OpenAI 兼容端点）",
        "short": "自定义接口",
    },
}

# 思考模式的中文显示名（配置里仍是 auto / off / on）
THINKING_LABELS = {
    "auto": "自动（听服务端默认）",
    "off": "关闭（更快更省）",
    "on": "开启（更慢更贵）",
}


def provider_label(provider: str) -> str:
    """内部标识 → 中文显示名。"""
    if not provider:
        return "自定义接口"
    preset = PROVIDER_PRESETS.get(provider)
    return str(preset.get("short") or preset.get("label")) if preset else f"自定义（{provider}）"


def provider_from_label(label: str) -> str:
    """中文显示名 → 内部标识（已经是标识就原样返回）。"""
    for key, preset in PROVIDER_PRESETS.items():
        if label in (preset.get("short"), preset.get("label"), key):
            return key
    return label


def thinking_label(value: str) -> str:
    return THINKING_LABELS.get(value, THINKING_LABELS["auto"])


def thinking_from_label(label: str) -> str:
    for key, text in THINKING_LABELS.items():
        if label == text or label == key:
            return key
    return "auto"

THINKING_PAYLOAD = {
    "off": {"thinking": {"type": "disabled"}},
    "on": {"thinking": {"type": "enabled"}},
}


class LlmHelper:
    """一次调用只干一件事：把题干丢给模型，拿回答案或读图结果。"""

    def __init__(
        self,
        url: str = "",
        key: str = "",
        model: str = "",
        *,
        provider: str = "",
        thinking: str = "auto",
        vision_model: str = "",
        timeout: float = 60.0,
        logger: Callable[[str], None] = print,
    ) -> None:
        self.url = (url or "").rstrip("/")
        self.key = (key or "").strip()
        self.model = (model or "").strip()
        self.provider = (provider or "").strip().lower()
        self.thinking = (thinking or "auto").strip().lower()
        self.vision_model = (vision_model or self.model).strip()
        self.timeout = timeout
        self.log = logger

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.key and self.model)

    # ── 端点 ──
    def _endpoint(self, path: str) -> str:
        base = self.url.rstrip("/")
        return f"{base}{path}" if base.endswith("/v1") else f"{base}/v1{path}"

    def endpoint_chat(self) -> str:
        return self._endpoint("/chat/completions")

    def endpoint_models(self) -> str:
        return self._endpoint("/models")

    # ── 底层 ──
    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"}

    def _post(self, payload: dict[str, Any], *, path: str = "/chat/completions") -> tuple[dict[str, Any] | None, str]:
        """发请求；带 thinking 参数被拒时自动退回不带该参数再试一次。"""
        url = self._endpoint(path)
        attempts: list[dict[str, Any]] = [payload]
        if "thinking" in payload:
            stripped = {k: v for k, v in payload.items() if k != "thinking"}
            attempts.append(stripped)

        last_error = ""
        for index, body in enumerate(attempts):
            try:
                resp = net.request("POST", url, headers=self._headers(), json=body, timeout=self.timeout)
            except Exception as exc:
                last_error = net.friendly_error(exc, url)
                continue
            if resp.status_code >= 400:
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                # 只有「thinking 参数不被接受」才值得换个姿势重试，其余错误直接返回
                if index == 0 and "thinking" in body and "thinking" in resp.text.lower():
                    self.log("    ℹ️ 该模型不接受「思考模式」参数，已自动去掉重试")
                    continue
                return None, last_error
            try:
                return resp.json(), ""
            except ValueError:
                return None, f"返回不是 JSON: {resp.text[:200]}"
        return None, last_error

    def _payload(self, messages: list[dict[str, Any]], model: str, max_tokens: int) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if self.thinking in THINKING_PAYLOAD:
            payload.update(THINKING_PAYLOAD[self.thinking])
        return payload

    @staticmethod
    def _content_of(body: dict[str, Any]) -> str:
        choices = body.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        # 思考型模型会把推理放在 reasoning_content，答案仍在 content
        return str(message.get("content") or "").strip()

    def _chat(self, prompt: str, *, model: str = "", max_tokens: int = 512) -> str | None:
        body, error = self._post(
            self._payload(
                [
                    {"role": "system", "content": f"[{uuid.uuid4()}] 你是英语词汇专家，只输出答案本身，不解释。"},
                    {"role": "user", "content": prompt},
                ],
                model or self.model,
                max_tokens,
            )
        )
        if body is None:
            self.log(f"    ⚠️ 大模型调用失败: {error}")
            return None
        text = self._content_of(body)
        return text or None

    # ── 业务能力 ──
    def ask_choice(self, topic: dict[str, Any], word_defs: dict[str, list[str]], hint: str = "") -> Any | None:
        """选择题：返回选项下标（int）。hint 是词典查到的权威释义。"""
        options = topic_options(topic)
        if not options:
            return None
        text = self._chat(self.build_prompt(topic, word_defs, hint), max_tokens=16)
        if not text:
            return None
        match = re.search(r"\d+", text)
        if not match:
            return None
        index = int(match.group())
        return index if 0 <= index < len(options) else None

    def ask_phrase(self, topic: dict[str, Any], word_defs: dict[str, list[str]], hint: str = "") -> str | None:
        """组词题：返回 "in,many,instances"。"""
        text = self._chat(self.build_prompt(topic, word_defs, hint), max_tokens=48)
        if not text:
            return None
        tokens = [tok for tok in re.split(r"[\s,，]+", text.strip()) if tok]
        return ",".join(tokens) if tokens else None

    def describe_image(self, data_uri: str, prompt: str) -> str | None:
        """读图：把 data URI 图片交给多模态模型识别（DeepSeek 用 deepseek-flash）。"""
        if not self.enabled:
            return None
        body, error = self._post(
            self._payload(
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_uri}},
                        ],
                    }
                ],
                self.vision_model or self.model,
                1024,
            )
        )
        if body is None:
            self.log(f"    ⚠️ 读图失败: {error}")
            return None
        return self._content_of(body) or None

    def list_models(self) -> list[str]:
        """拉模型列表（用于确认 key 能用、以及有哪些模型名可用）。"""
        try:
            resp = net.request("GET", self.endpoint_models(), headers=self._headers(), timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json().get("data") or []
            return [str(item.get("id") or "") for item in data if item.get("id")]
        except Exception as exc:
            self.log(f"    ⚠️ 拉取模型列表失败: {net.friendly_error(exc, self.endpoint_models())}")
            return []

    def diagnose(self, image_data_uri: str = "") -> dict[str, Any]:
        """体检：模型列表 + 一次真实对话 +（可选）一次真实读图。"""
        report: dict[str, Any] = {
            "provider": self.provider or "custom",
            "endpoint": self.endpoint_chat(),
            "model": self.model,
            "vision_model": self.vision_model,
            "thinking": self.thinking,
        }
        if not self.enabled:
            report.update({"ok": False, "reason": "缺少 url / key / model"})
            return report

        report["models"] = self.list_models()
        answer = self._chat("Reply with exactly: OK", max_tokens=8)
        report["chat_ok"] = bool(answer)
        report["chat_reply"] = answer
        if image_data_uri:
            text = self.describe_image(image_data_uri, "只输出图中的英文文字，不要解释。")
            report["vision_ok"] = bool(text)
            report["vision_text"] = text
        report["ok"] = bool(report["chat_ok"])
        return report

    # ── 提示词 ──
    @staticmethod
    def build_prompt(topic: dict[str, Any], word_defs: dict[str, list[str]], hint: str = "") -> str:
        mode = topic_mode(topic)
        stem = topic_stem(topic)
        remark = topic_remark(topic)
        options = topic_options(topic)
        opts_text = "\n".join(f"{i}. {opt.get('content', '')}" for i, opt in enumerate(options))

        glossary = ""
        # 在线词典/本地词库查到的释义优先采信 —— 比模型凭记忆靠谱
        if hint.strip():
            glossary += f"权威词典释义（与被考单词匹配的优先采信）: {hint.strip()}\n\n"
        if word_defs:
            lines = [f"  {w}: {'; '.join(ds)}" for w, ds in list(word_defs.items())[:40]]
            glossary += "已知单词释义:\n" + "\n".join(lines) + "\n\n"

        if mode == 32:
            return (
                f"{glossary}题目: 用下面的选项词组成短语, 中文含义是「{remark}」\n"
                f"空格数: {stem}\n选项:\n{opts_text}\n\n"
                "请给出正确的词及其顺序。只输出逗号分隔的词, 例如 in,many,instances, 不要解释。"
            )
        if mode == 31:
            return (
                f"{glossary}题目: 下面哪个是单词「{stem}」的正确释义?\n选项:\n{opts_text}\n\n"
                "只输出选项编号, 例如 0, 不要解释。"
            )
        if mode == 11:
            reference = f"句子中文翻译(参考): {remark}\n" if isinstance(remark, str) and remark else ""
            return (
                f"{glossary}题目: 判断句中 {{}} 内单词在该句中的具体含义, 选出正确释义\n"
                f"句子: {stem}\n{reference}选项:\n{opts_text}\n\n"
                "注意一词多义与词性, 必须结合上下文。只输出选项编号, 例如 0, 不要解释。"
            )
        return (
            f"{glossary}题目 (mode={mode}) 题干: {display_stem(topic)}\n备注: {remark}\n选项:\n{opts_text}\n\n"
            "选择题输出选项编号; 组词题输出逗号分隔的词。不要解释。"
        )


# 视觉模型的常见命名特征，用来从模型列表里认出「哪个能读图」
VISION_HINTS = ("vision", "vl", "visual", "omni", "image")


def guess_models(ids: list[str], provider: str = "") -> tuple[str, str]:
    """从接口拉回来的模型列表里，猜出「答题模型」和「读图模型」。

    优先级：服务商预设点名的模型 → 名字带视觉特征的 → 第一个可用模型。
    """
    if not ids:
        return "", ""
    preset = PROVIDER_PRESETS.get(provider) or {}
    text = preset.get("model") if preset.get("model") in ids else ""
    vision = preset.get("vision_model") if preset.get("vision_model") in ids else ""

    if not vision:
        vision = next((item for item in ids if any(hint in item.lower() for hint in VISION_HINTS)), "")
    if not text:
        text = next((item for item in ids if item != vision and not any(h in item.lower() for h in VISION_HINTS)), ids[0])
    if not vision:
        vision = text  # 兜底：只有一个模型时，答题读图都用它
    return text, vision


def load_llm_from_config(config: dict[str, Any], logger: Callable[[str], None] = print) -> LlmHelper:
    """按 provider 预设 + 显式配置合成一个 LlmHelper。"""
    from .settings import resolve_llm

    resolved = resolve_llm(config)
    return LlmHelper(
        url=resolved["url"],
        key=resolved["key"],
        model=resolved["model"],
        provider=resolved["provider"],
        thinking=resolved["thinking"],
        vision_model=resolved["vision_model"],
        logger=logger,
    )


def preset_choices() -> list[tuple[str, str]]:
    """给界面用的下拉项：[(内部标识, 中文名)]。"""
    return [(key, str(value.get("short") or value.get("label") or key)) for key, value in PROVIDER_PRESETS.items()]


def dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
