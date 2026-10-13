# -*- coding: utf-8 -*-
"""网络层：统一代理策略 + 人话错误信息。

为什么单独一层：
  requests 默认会读环境变量（HTTP_PROXY / HTTPS_PROXY / ALL_PROXY）和 Windows 系统代理。
  很多人机器上残留着 Clash / V2Ray 的 127.0.0.1:7890，代理程序一关，所有请求全挂，
  报出来还是 JSONDecodeError / ProxyError 这种看不懂的东西，看起来像程序坏了。

  词达人本身是国内服务，所以这里**默认直连**（trust_env = False，忽略系统/环境代理）；
  只有你在配置里显式填了代理，才会走代理。
"""

from __future__ import annotations

import requests

_state: dict[str, object] = {"proxy": "", "use_system_proxy": False}


def configure(proxy: str = "", use_system_proxy: bool = False) -> None:
    """设置全局代理策略（程序启动 / 保存配置时调用）。"""
    _state["proxy"] = (proxy or "").strip()
    _state["use_system_proxy"] = bool(use_system_proxy)


def proxies() -> dict[str, str] | None:
    """显式配置的代理；没配就返回 None（= 直连）。"""
    proxy = str(_state.get("proxy") or "").strip()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def trust_env() -> bool:
    """是否允许 requests 读环境变量/系统代理。默认 False。"""
    return bool(_state.get("use_system_proxy"))


def make_session(headers: dict[str, str] | None = None) -> requests.Session:
    """建一个遵守本模块代理策略的会话。"""
    session = requests.Session()
    session.trust_env = trust_env()
    if headers:
        session.headers.update(headers)
    proxy = proxies()
    if proxy:
        session.proxies.update(proxy)
    return session


def request(method: str, url: str, *, timeout: float = 20.0, headers: dict[str, str] | None = None, **kwargs) -> requests.Response:
    """一次性请求（内部按需建会话，顺便隔离 cookie）。"""
    session = make_session(headers)
    try:
        return session.request(method, url, timeout=timeout, **kwargs)
    finally:
        session.close()


def detect_proxy() -> str:
    """探测环境变量/系统里配置的代理 —— 只用于给用户提示，不影响请求走法。"""
    try:
        found = requests.utils.getproxies()
    except Exception:
        return ""
    for key in ("https", "http", "all"):
        value = found.get(key)
        if value:
            return str(value)
    return ""


def is_noise_proxy(exc: BaseException) -> str:
    """如果异常是「代理连不上」，返回代理地址；否则空串。"""
    text = f"{type(exc).__name__}: {exc}"
    if "ProxyError" in text or "proxy" in text.lower():
        proxy = str(_state.get("proxy") or "") or detect_proxy()
        return proxy or "系统代理"
    return ""


def friendly_error(exc: BaseException, url: str = "") -> str:
    """把 requests 的各种异常翻译成用户能照做的中文提示。"""
    name = type(exc).__name__
    text = str(exc)
    where = f"（{url}）" if url else ""

    proxy = is_noise_proxy(exc)
    if proxy:
        return (
            f"代理连不上{where}：{proxy}。\n"
            "   办法一：在「设置」页把「代理」留空（默认就是直连，推荐）\n"
            "   办法二：把代理程序打开，或把地址改成真正可用的那个\n"
            "   办法三：勾选「跟随系统代理」表示确实要走系统里配置的代理"
        )

    if "Timeout" in name or isinstance(exc, requests.exceptions.Timeout):
        return f"请求超时{where}：网络太慢或被拦了，重试一次；持续失败请检查网络环境。"
    if isinstance(exc, requests.exceptions.SSLError):
        return f"HTTPS 证书校验失败{where}：多半是网络被中间人代理改了证书，换回直连试试。"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return (
            f"连不上服务器{where}：{text[:160]}\n"
            "   检查网络是否正常；如果开着 VPN / 代理，试试关掉（本程序默认直连）。"
        )

    # 代理或防火墙返回了非 JSON（HTML 错误页 / 空响应）
    if isinstance(exc, ValueError) or "JSONDecode" in name:
        return (
            f"服务器返回的不是 JSON{where}：{text[:120]}\n"
            "   常见原因：请求被代理或防火墙拦截了。本程序默认直连，如仍失败请检查网络环境。"
        )
    return f"{name}: {text[:200]}"
