# -*- coding: utf-8 -*-
"""词达人自动答题工具（vocabgo / 微信词达人）。

模块划分：
  crypto    请求签名 + 响应 jv 混淆解码
  client    班级任务 / 自学任务 两套 HTTP 接口
  matcher   题干解析与规则匹配
  bank      本地题库缓存
  solver    答题主流程（策略链 + 可中断）
  llm       DeepSeek 等 OpenAI 兼容模型：答题兜底 + 读图 + 体检
  vision    读图：图片判定 + OCR（LLM / tesseract / 百度）
  qrlogin   扫码授权取 token
  settings  配置中心（默认值 ← config.json ← 环境变量）
  paths     路径解析（打包后写到 exe 同目录）
  gui       tkinter 图形界面
  cli       命令行入口
"""

from .bank import Bank
from .client import VocabgoClient, VocabgoError
from .crypto import VERSION, decode_jv, encode_jv, sign, unwrap
from .llm import PROVIDER_PRESETS, LlmHelper
from .paths import app_dir, config_path, is_frozen
from .settings import load_settings, resolve_llm, save_settings
from .solver import Solver, Stats
from .vision import ImageReader, detect_image, hydrate_topic

__version__ = "2.0.3"
__all__ = [
    "Bank",
    "ImageReader",
    "LlmHelper",
    "PROVIDER_PRESETS",
    "Solver",
    "Stats",
    "VERSION",
    "VocabgoClient",
    "VocabgoError",
    "app_dir",
    "config_path",
    "decode_jv",
    "detect_image",
    "encode_jv",
    "hydrate_topic",
    "is_frozen",
    "load_settings",
    "resolve_llm",
    "save_settings",
    "sign",
    "unwrap",
]
