# -*- coding: utf-8 -*-
"""扫码授权取 UserToken（不用抓包，纯接口流程；GUI 与命令行共用）。

链路（原始实现来自 cdr，但域名/路径已按现状修正）：

1. 本地生成 32 位随机 user_token（词达人把「身份」绑到这个字符串上）
2. 调 {host}/Auth/Thirdpart/Authorize 换出 redirect_url（微信 OAuth 链接）
3. 把 redirect_url 变成二维码 → 微信扫码 → 词达人授权给这个 user_token
4. 用业务接口（带着这个 token）确认授权是否生效
5. 写进 config.json

注意：这个接口以前在 gateway.vocabgo.com，现在在 app.vocabgo.com（老域名返回 404），
所以这里两个域名都试一遍并记住哪个能用，将来再搬家也不至于直接挂掉。
"""

from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from typing import Any, Callable

import requests

from . import net
from .client import DEFAULT_UA, derive_auth_headers
from .crypto import SALT, VERSION, md5
from .paths import config_path

AUTHORIZE_PATH = "/Auth/Thirdpart/Authorize"
AUTHORIZE_HOSTS = (
    "https://app.vocabgo.com",       # 现状（2026-10 实测可用）
    "https://gateway.vocabgo.com",   # 老域名，留作兜底
)
RETURN_URL = "https://app.vocabgo.com/overall/#/student/home"

# 令牌是否生效：打真实业务接口，code==1 才算有效
VALIDATE_ENDPOINTS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    (
        "POST",
        "https://app.vocabgo.com/studentv1/api/Student/ClassTask/PageTask",
        {"json": {"search_type": "0", "page_count": 1, "page_size": 1}},
    ),
    ("GET", "https://app.vocabgo.com/Student/Main", {}),
)

_working_host: str | None = None  # 记住哪个域名能用，下次直接用


def now_ms() -> int:
    return int(time.time() * 1000)


def headers_for(token: str = "") -> dict[str, str]:
    """请求头：和业务请求保持一致（UA + 按 UA 推算的两个鉴权字段）。"""
    abc, auth_v = derive_auth_headers(DEFAULT_UA)
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "https://app.vocabgo.com",
        "Referer": "https://app.vocabgo.com/",
        "User-Agent": DEFAULT_UA,
        "Abc": abc,
        "Authorization-V": auth_v,
        "X-Requested-With": "XMLHttpRequest",
    }
    if token:
        headers["UserToken"] = token
        headers["usertoken"] = token
    return headers


def token_alive(token: str) -> bool:
    """令牌是否生效：打真实业务接口，返回 code==1 才算有效。"""
    if not token:
        return False
    for method, url, extra in VALIDATE_ENDPOINTS:
        kwargs = dict(extra)
        if method == "GET":
            kwargs["params"] = {"timestamp": now_ms(), "versions": VERSION}
        try:
            resp = net.request(method, url, headers=headers_for(token), timeout=15, **kwargs)
            if resp.status_code >= 400:
                continue
            body = resp.json()
            if body.get("code") == 1:
                return True
        except Exception:
            continue
    return False


def new_token(seed: str = "") -> str:
    alphabet = "abcde0123456789"
    return md5(seed + "".join(random.choice(alphabet) for _ in range(32)) + str(now_ms()))


def authorize(token: str) -> str:
    """换取授权链接（微信里打开即完成绑定）。会按顺序试所有已知域名。"""
    global _working_host

    timestamp = now_ms()
    data = {
        "return_url": RETURN_URL,
        "auth_type": 1,
        "user_token": token,
        "timestamp": timestamp,
        "versions": VERSION,
    }
    payload = {**data, "sign": md5("&".join(f"{k}={data[k]}" for k in sorted(data)) + SALT)}

    hosts: list[str] = []
    if _working_host:
        hosts.append(_working_host)
    hosts.extend(host for host in AUTHORIZE_HOSTS if host not in hosts)

    problems: list[str] = []
    for host in hosts:
        url = host + AUTHORIZE_PATH
        try:
            resp = net.request("POST", url, json=payload, headers=headers_for(), timeout=20)
        except requests.RequestException as exc:
            problems.append(f"{url} → {net.friendly_error(exc)}")
            continue

        if resp.status_code == 404:
            problems.append(f"{url} → HTTP 404（这个域名下接口已不存在）")
            continue
        try:
            body = resp.json()
        except ValueError:
            problems.append(f"{url} → HTTP {resp.status_code} 返回的不是 JSON：{resp.text[:80]}")
            continue
        if body.get("code") == 1 and body.get("data"):
            _working_host = host
            return str(body["data"]["redirect_url"])
        problems.append(f"{url} → code={body.get('code')} msg={body.get('msg')}")

    raise RuntimeError(
        "扫码授权接口目前都不可用：\n   "
        + "\n   ".join(problems)
        + "\n   → 请改用抓包方式获取令牌（说明书里的「方式 B」）"
    )


def render_qr_png(url: str) -> bytes | None:
    """把授权链接画成二维码，直接在内存里返回 PNG 字节（不落盘）。

    没有 qrcode 库时返回 None —— 调用方要准备好「只用链接」的兜底，别假装二维码存在。
    """
    try:
        import io

        import qrcode  # type: ignore

        qr = qrcode.QRCode(version=3, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=8, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        buffer = io.BytesIO()
        qr.make_image().save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:
        return None


def save_qr_png(url: str, out_path: Path) -> bool:
    """把二维码存成文件（命令行模式用）。"""
    data = render_qr_png(url)
    if not data:
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    return True


def open_file(path: Path) -> None:
    try:
        os.startfile(path)  # type: ignore[attr-defined]  # Windows 直接弹图
    except Exception:
        pass


def write_token(config_file: Path, token: str) -> Path:
    """把 token 写进配置文件（保留其余配置）。"""
    from .settings import DEFAULT_SETTINGS

    config: dict[str, Any] = {}
    if config_file.exists():
        try:
            config = json.loads(config_file.read_text(encoding="utf-8"))
        except Exception:
            config = {}
    for section, defaults in DEFAULT_SETTINGS.items():
        config.setdefault(section, dict(defaults) if not isinstance(defaults, dict) else defaults)
    config.setdefault("auth", {})["usertoken"] = token
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_file


def start_login(
    *,
    config_file: Path | None = None,
    timeout: int = 300,
    on_url: Callable[[str, bytes | None], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[bool, str]:
    """完整授权流程。返回 (是否成功, token)。

    on_url(url, png_bytes) 让界面自己把二维码显示出来（png_bytes 可能是 None）；
    on_status 是进度回调；should_stop 返回 True 则中途放弃。
    """
    config_file = config_file or config_path()

    def status(message: str) -> None:
        if on_status:
            on_status(message)

    def stopped() -> bool:
        return bool(should_stop and should_stop())

    token = new_token()
    url = authorize(token)
    status(f"授权链接已生成（user_token={token[:10]}…）")

    if on_url:
        on_url(url, render_qr_png(url))

    deadline = time.time() + timeout
    while time.time() < deadline:
        if stopped():
            return False, token
        if token_alive(token):
            write_token(config_file, token)
            status(f"授权成功，已写入 {config_file}")
            return True, token
        time.sleep(3)
    status("等待超时，请重试")
    return False, token


def load_existing_token(config_file: Path | None = None) -> str:
    config_file = config_file or config_path()
    if not config_file.exists():
        return ""
    try:
        return str((json.loads(config_file.read_text(encoding="utf-8")).get("auth") or {}).get("usertoken") or "")
    except Exception:
        return ""
