# -*- coding: utf-8 -*-
"""璇嶈揪浜鸿嚜鍔ㄧ瓟棰樺伐鍏凤紙vocabgo / 寰俊璇嶈揪浜猴級銆?
妯″潡鍒掑垎锛?  crypto    璇锋眰绛惧悕 + 鍝嶅簲 jv 娣锋穯瑙ｇ爜
  client    鐝骇浠诲姟 / 鑷浠诲姟 涓ゅ HTTP 鎺ュ彛
  matcher   棰樺共瑙ｆ瀽涓庤鍒欏尮閰?  bank      鏈湴棰樺簱缂撳瓨
  solver    绛旈涓绘祦绋嬶紙绛栫暐閾?+ 鍙腑鏂級
  llm       DeepSeek 绛?OpenAI 鍏煎妯″瀷锛氱瓟棰樺厹搴?+ 璇诲浘 + 浣撴
  vision    璇诲浘锛氬浘鐗囧垽瀹?+ OCR锛圠LM / tesseract / 鐧惧害锛?  qrlogin   鎵爜鎺堟潈鍙?token
  settings  閰嶇疆涓績锛堥粯璁ゅ€?鈫?config.json 鈫?鐜鍙橀噺锛?  paths     璺緞瑙ｆ瀽锛堟墦鍖呭悗鍐欏埌 exe 鍚岀洰褰曪級
  gui       tkinter 鍥惧舰鐣岄潰
  cli       鍛戒护琛屽叆鍙?"""

from .bank import Bank
from .client import VocabgoClient, VocabgoError
from .crypto import VERSION, decode_jv, encode_jv, sign, unwrap
from .llm import PROVIDER_PRESETS, LlmHelper
from .paths import app_dir, config_path, is_frozen
from .settings import load_settings, resolve_llm, save_settings
from .solver import Solver, Stats
from .vision import ImageReader, detect_image, hydrate_topic

__version__ = "2.0.8"
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
