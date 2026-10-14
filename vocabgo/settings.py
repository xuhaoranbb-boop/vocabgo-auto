# -*- coding: utf-8 -*-
"""配置中心：默认值 → config.json → 环境变量，三层覆盖；GUI 与 CLI 共用一份。

结构（config.json）::

    {
      "auth":   { "usertoken": "", "abc": "", "authorization_v": "" },
      "llm":    { "provider": "deepseek", "url": "", "key": "", "model": "",
                  "vision_model": "", "thinking": "auto" },
      "vision": { "backend": "auto", "tesseract_cmd": "" },
      "study":  { "course_id": "CET4_v2", "grade": 2 },
      "runtime":{ "pace": [1.0, 2.6], "probe": true, "max_score": 10, "bank_path": "" }
    }

`llm.provider` 决定预设（见 llm.PROVIDER_PRESETS）：填了 provider 就不用记 base_url，
只填 key 即可；显式填了 url/model 则以显式值为准。
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from .paths import config_path

DEFAULT_SETTINGS: dict[str, Any] = {
    "auth": {"usertoken": "", "abc": "", "authorization_v": ""},
    "llm": {
        "provider": "deepseek",
        "url": "",
        "key": "",
        "model": "",
        "vision_model": "",
        "thinking": "",  # 留空 = 跟随服务商预设（DeepSeek 预设是 off）
    },
    "vision": {"backend": "auto", "tesseract_cmd": ""},
    "network": {
        "proxy": "",  # 留空 = 直连（推荐）；要走代理才填，如 http://127.0.0.1:7890
        "use_system_proxy": False,  # 勾上才去读环境变量 / Windows 系统代理
    },
    "study": {"course_id": "CET4_v2", "grade": 2},
    "runtime": {
        "pace": [1.0, 2.6],
        "probe": True,
        "max_score": 10,
        "bank_path": "",
        "loop": 3,          # 练习轮数
        "kind": "class",    # 上次用的任务类型
        "ocr": True,        # 上次的读图开关
        "last_task_id": "", # 上次刷的任务，下次自动帮你选中
        "token_saved_at": "", # 令牌是什么时候拿到的（词达人令牌约 6 小时过期，用来提前提醒）
        "probe_on_class": False, # 班级任务是否允许「试答案」。默认关：班级任务只有一次机会，
                                 # 故意答错会永久留在成绩里，得不偿失。
        "prepare_class": True,   # 班级任务开跑前，先刷对应自学任务把题库喂饱（同一套词表）
        "prepare_rounds": 3,     # 备题刷几轮
        "glossary": True,        # 查词典（在线词典 + 本地词库）：不靠记忆，靠查证
        "glossary_path": "",     # 词库缓存文件，留空 = 跟题库放一起（glossary.json）
    },
}

# 环境变量覆盖表：环境变量名 → (配置段, 配置键)
ENV_MAP = {
    "VOCABGO_TOKEN": ("auth", "usertoken"),
    "VOCABGO_ABC": ("auth", "abc"),
    "VOCABGO_AUTH_V": ("auth", "authorization_v"),
    "VOCABGO_LLM_PROVIDER": ("llm", "provider"),
    "VOCABGO_LLM_URL": ("llm", "url"),
    "VOCABGO_LLM_KEY": ("llm", "key"),
    "VOCABGO_LLM_MODEL": ("llm", "model"),
    "VOCABGO_VISION_MODEL": ("llm", "vision_model"),
    "VOCABGO_VISION_BACKEND": ("vision", "backend"),
}

REQUIRED_AUTH_FIELDS = ("usertoken",)

# ── 推荐配置 ──
# 给「不想研究参数、就想稳定拿高分」的人用：界面上一键填入，说明书里也是这张表。
RECOMMENDED: dict[str, Any] = {
    "llm": {
        "provider": "deepseek",
        "url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "vision_model": "deepseek-flash",
        "thinking": "off",
    },
    "vision": {"backend": "auto", "tesseract_cmd": ""},
    "network": {"proxy": "", "use_system_proxy": False},
    "study": {"course_id": "CET4_v2", "grade": 2},
    "runtime": {
        "pace": [1.5, 3.5],
        "probe": True,
        "ocr": True,
        "max_score": 10,
        "loop": 3,
        "kind": "class",
    },
}
# 每一项「为什么这么设」——界面日志与说明书共用，避免两处说法不一致
RECOMMENDED_NOTES: list[tuple[str, str, str]] = [
    ("服务商", "DeepSeek", "答题兜底 + 读图一把 key 搞定，价格低"),
    ("答题模型", "deepseek-v4-flash", "从选项里挑答案这种短任务，够用、快、便宜"),
    ("读图模型", "deepseek-flash", "支持图片输入，题干万一变成图片也能认出来"),
    ("思考模式", "关闭", "每题都推理会又慢又贵，短答提取不需要"),
    ("作答间隔", "1.5 ~ 3.5 秒", "比默认再慢一点，更像人在做题，降低被风控关注"),
    ("试答案兜底", "开启", "生词也能先判对再提交，分数不会掉"),
    ("读图", "开启（自动）", "有模型时用模型，没有就退回本地识别，不影响流程"),
    ("练习轮数", "3", "自动重练没满分的词，把分追满"),
    ("满分阈值", "10", "10 分制下的满分线"),
]


def apply_recommended(config: dict[str, Any]) -> dict[str, Any]:
    """把推荐配置合并进现有配置；不碰令牌、密钥这类个人凭据。"""
    import copy as _copy

    for section, values in RECOMMENDED.items():
        bucket = config.setdefault(section, {})
        for key, value in _copy.deepcopy(values).items():
            bucket[key] = value
    return config


def recommended_lines() -> list[str]:
    """推荐配置的文字说明（界面日志 / 说明书复用一个来源）。"""
    return [f"　{name}：{value}　— {why}" for name, value, why in RECOMMENDED_NOTES]


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_settings(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """读配置：默认值 ← config.json ← 环境变量（后者优先）。"""
    config = copy.deepcopy(DEFAULT_SETTINGS)
    config_file = config_path(path)
    if config_file.exists():
        try:
            _deep_merge(config, json.loads(config_file.read_text(encoding="utf-8")))
        except Exception as exc:
            print(f"⚠️ 配置读取失败（{config_file}）：{exc}，改用默认值")

    for env_name, (section, key) in ENV_MAP.items():
        value = os.environ.get(env_name)
        if value:
            config.setdefault(section, {})[key] = value

    config["_path"] = str(config_file)
    return config


def save_settings(config: dict[str, Any], path: str | os.PathLike[str] | None = None) -> Path:
    """写回配置（丢掉内部键，保证文件干净）。"""
    config_file = config_path(path or config.get("_path"))
    payload = {
        section: {k: v for k, v in values.items()}
        for section, values in config.items()
        if isinstance(values, dict) and not section.startswith("_")
    }
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_file


def missing_auth_fields(config: dict[str, Any]) -> list[str]:
    auth = config.get("auth") or {}
    return [field for field in REQUIRED_AUTH_FIELDS if not str(auth.get(field) or "").strip()]


def resolve_llm(config: dict[str, Any]) -> dict[str, str]:
    """把 provider 预设 + 显式配置 + 环境变量合成一份最终 LLM 参数。"""
    from .llm import PROVIDER_PRESETS  # 延迟导入避免循环依赖

    llm = config.get("llm") or {}
    provider = str(llm.get("provider") or "").strip().lower()
    preset = PROVIDER_PRESETS.get(provider, {}) if provider else {}

    url = str(llm.get("url") or preset.get("url") or "").strip()
    model = str(llm.get("model") or preset.get("model") or "").strip()
    vision_model = str(llm.get("vision_model") or preset.get("vision_model") or model).strip()
    thinking = str(llm.get("thinking") or preset.get("thinking") or "auto").strip().lower()

    key = str(llm.get("key") or "").strip()
    if not key:
        for env_name in (preset.get("env_key"), "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
            if env_name and os.environ.get(env_name):
                key = os.environ[env_name].strip()
                break

    return {
        "provider": provider or "custom",
        "url": url,
        "key": key,
        "model": model,
        "vision_model": vision_model,
        "thinking": thinking,
    }


def apply_runtime_overrides(config: dict[str, Any], args: Any) -> dict[str, Any]:
    """CLI 参数覆盖运行时段（pace/probe 等），GUI 不走这里。"""
    runtime = config.setdefault("runtime", {})
    if getattr(args, "fast", False):
        runtime["pace"] = [0.0, 0.0]
    if getattr(args, "no_probe", False):
        runtime["probe"] = False
    if getattr(args, "bank", None):
        runtime["bank_path"] = args.bank
    return config
