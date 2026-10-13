# -*- coding: utf-8 -*-
"""mitmproxy 插件：自动抓出词达人学生端的三个鉴权头，直接写进 config.json。

用法：

1. 安装：``pip install mitmproxy``
2. 启动：
   ``mitmproxy -s tools/mitm_capture.py``   （命令行 UI）
   ``mitmdump  -s tools/mitm_capture.py``   （只看日志）
3. 让微信走代理（二选一）：
   * 系统代理：mitmproxy 默认监听 127.0.0.1:8080，PC 微信跟随系统代理即可
   * 手机：手机 Wi-Fi 代理指向电脑 IP:8080，并安装 mitm.it 的证书
4. 打开 PC 微信 → 词达人 公众号 → 学生端 → 随便点进一个任务页面
5. 本插件会把 usertoken / abc / authorization-v 打到终端，并合并写入 config.json

⚠️ 只抓自己账号的鉴权头；抓完关掉代理，别长期挂着。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from mitmproxy import http
except ImportError:  # 允许在没有 mitmproxy 的环境里被 import（例如跑自检）
    http = None  # type: ignore

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
CAPTURE_PATH = ROOT / "captured_headers.json"

TARGET_HOST = "vocabgo.com"
HEADER_KEYS = ("usertoken", "abc", "authorization-v")


def _load(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save(token: str, abc: str, auth_v: str) -> None:
    captured = _load(CAPTURE_PATH)
    captured.update({"usertoken": token, "abc": abc, "authorization_v": auth_v})
    CAPTURE_PATH.write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")

    config = _load(CONFIG_PATH)
    config.setdefault("auth", {})
    config["auth"].update({"usertoken": token, "abc": abc, "authorization_v": auth_v})
    config.setdefault("llm", {"url": "", "key": "", "model": ""})
    config.setdefault("study", {"course_id": "CET4_v2", "grade": 2})
    config.setdefault("runtime", {"pace": [1.0, 2.6], "probe": True, "max_score": 10, "bank_path": "bank.json"})
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def request(flow: "http.HTTPFlow") -> None:  # noqa: F821
    """每次请求都会进来：命中词达人域且带 usertoken 就记一份。"""
    if http is None:
        return
    try:
        host = flow.request.pretty_host
    except Exception:
        return
    if TARGET_HOST not in host:
        return

    headers = {key.lower(): value for key, value in flow.request.headers.items()}
    token = headers.get("usertoken")
    if not token:
        return

    abc = headers.get("abc", "")
    auth_v = headers.get("authorization-v", "")
    _save(token, abc, auth_v)

    print(
        "\n🎯 抓到词达人鉴权头\n"
        f"   来源: {flow.request.method} {host}{flow.request.path}\n"
        f"   usertoken      = {token[:16]}...（已写入 config.json）\n"
        f"   abc            = {abc[:24]}{'...' if len(abc) > 24 else ''}\n"
        f"   authorization-v= {auth_v[:24]}{'...' if len(auth_v) > 24 else ''}",
        file=sys.stderr,
    )


def load(loader) -> None:  # mitmproxy 插件入口日志
    print(f"🕵️ 词达人抓包插件已加载，命中 {TARGET_HOST} 且带 usertoken 的请求就会自动落盘", file=sys.stderr)
