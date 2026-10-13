# -*- coding: utf-8 -*-
"""获取用户令牌（多种方式，都不需要抓包）。

⚠️ 历史坑：最初这里实现的是「扫码授权」，走 `/Auth/Thirdpart/Authorize` 换出微信 OAuth 链接，
让用户扫码绑定一个本地随机 user_token。**这条路已经不通了** —— 实测扫码后服务端返回
「微信认证失败」：那套接口是给第三方授权用的，且新版客户端登录已改成 handshake + 公钥加密传输。

现在可用的方式（均从学生端 JS 里还原出来的真实接口）：

1. **账号密码登录**：``POST /Auth/Login`` ``{user_name, password, login_type}`` → ``data.token``
2. **用户ID + 图形验证码**：``POST /Auth/Login/Before`` 取验证码 → ``POST /Auth/Login/ByUserId``
   ``{id, password, req_no, valid_code}`` → ``data.token``
3. 抓包（``tools/mitm_capture.py``）：已经登录上微信的客户端里直接抄 ``usertoken``

拿到 token 后统一写进 config.json 的 ``auth.usertoken``。
"""

from __future__ import annotations

import base64
import json
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import requests

from . import net
from .client import DEFAULT_UA, derive_auth_headers
from .crypto import SALT, VERSION, md5
from .paths import config_path

AUTH_BASE = "https://app.vocabgo.com"
AUTH_LOGIN = AUTH_BASE + "/Auth/Login"
AUTH_LOGIN_BEFORE = AUTH_BASE + "/Auth/Login/Before"
AUTH_LOGIN_BY_ID = AUTH_BASE + "/Auth/Login/ByUserId"
AUTH_WECHAT_V2 = AUTH_BASE + "/Auth/Wechat/V2/LoginByWechatCode"

# 微信扫码登录：词达人自己的公众号 appid 与回调地址（redirect_uri 必须在平台侧注册过，改不了）
WECHAT_OAUTH = "https://open.weixin.qq.com/connect/oauth2/authorize"
WECHAT_APPID = "wx2a694105a6abbe6d"
WECHAT_REDIRECT_URI = "https://app.vocabgo.com/student/?authorize=0"

# 旧路（已废弃，仅保留常量以免调用方 import 报错）
AUTHORIZE_PATH = "/Auth/Thirdpart/Authorize"
AUTHORIZE_HOSTS = (AUTH_BASE, "https://gateway.vocabgo.com")
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

_working_host: str | None = None  # 兼容旧引用


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class LoginResult:
    ok: bool
    token: str = ""
    message: str = ""


def _auth_post(path: str, payload: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
    """给 Auth 系列接口发一次带签名的请求（和业务请求同一套签名规则）。"""
    stamp = now_ms()
    data: dict[str, Any] = {**payload, "timestamp": stamp, "version": VERSION}
    data["sign"] = md5("&".join(f"{k}={data[k]}" for k in sorted(data) if data[k] or data[k] == 0) + SALT)
    data["app_type"] = 1
    resp = net.request("POST", path, json=data, headers=headers_for(), timeout=timeout)
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(
            net.friendly_error(exc, path) + f"\n   HTTP {resp.status_code}，返回内容：{resp.text[:160]}"
        ) from exc


def _result_from(body: dict[str, Any]) -> LoginResult:
    if body.get("code") == 1:
        token = str((body.get("data") or {}).get("token") or "")
        if token:
            return LoginResult(True, token=token)
        return LoginResult(False, message="登录成功但没拿到令牌，请改用抓包方式")
    return LoginResult(False, message=str(body.get("msg") or body.get("message") or "登录失败"))


def login_with_password(user_name: str, password: str, login_type: int = 1) -> LoginResult:
    """账号密码登录（学生端 /login 页用的就是这个接口）。

    参数名来自学生端 JS：``postUserLogin({user_name, password, login_type})``。
    """
    user_name = (user_name or "").strip()
    if not user_name or not password:
        return LoginResult(False, message="用户名和密码都要填")
    try:
        body = _auth_post(AUTH_LOGIN, {"user_name": user_name, "password": password, "login_type": login_type})
    except Exception as exc:
        return LoginResult(False, message=str(exc))
    return _result_from(body)


def fetch_captcha() -> tuple[str, str]:
    """取图形验证码：返回 ``(req_no, 验证码图片 data-URI)``。"""
    body = _auth_post(AUTH_LOGIN_BEFORE, {})
    data = body.get("data") or {}
    return str(data.get("req_no") or ""), str(data.get("valid_code_image") or "")


def login_with_user_id(user_id: str, password: str, valid_code: str, req_no: str) -> LoginResult:
    """用户ID + 密码 + 图形验证码登录（学生端 /testLogin 页）。

    参数名来自学生端 JS：``postTestLogin({id, password, req_no, valid_code})``。
    """
    if not str(user_id).strip() or not password or not str(valid_code).strip():
        return LoginResult(False, message="用户ID、密码、验证码都要填")
    try:
        body = _auth_post(
            AUTH_LOGIN_BY_ID,
            {"id": str(user_id).strip(), "password": password, "req_no": req_no, "valid_code": str(valid_code).strip()},
        )
    except Exception as exc:
        return LoginResult(False, message=str(exc))
    return _result_from(body)


def login_and_save(result: LoginResult, config_file: Path | None = None) -> Path | None:
    """登录成功就把令牌写进配置文件。"""
    if not result.ok or not result.token:
        return None
    return write_token(config_file or config_path(), result.token)


def build_wechat_login_url(state: str = "vocabgo") -> str:
    """拼出「用微信打开就能登录词达人」的授权链接（把它画成二维码即可）。"""
    params = {
        "appid": WECHAT_APPID,
        "redirect_uri": WECHAT_REDIRECT_URI,
        "response_type": "code",
        "scope": "snsapi_userinfo",
        "state": state,
    }
    query = "&".join(f"{key}={quote(str(value), safe='')}" for key, value in params.items())
    return f"{WECHAT_OAUTH}?{query}#wechat_redirect"


def extract_wechat_code(text: str) -> str:
    """从用户粘贴的内容里抠出微信 code：整条链接、带参 URL、参数片段或裸 code 都认。"""
    text = (text or "").strip()
    if not text:
        return ""
    matched = re.search(r"(?:^|[?&#])code=([A-Za-z0-9_\-]+)", text)
    if matched:
        return matched.group(1)
    if re.fullmatch(r"[A-Za-z0-9_\-]{8,}", text):
        return text  # 用户只复制了 code 本身
    return ""


def login_with_wechat_code(code: str) -> LoginResult:
    """用微信授权回来的 code 换令牌（学生端 /authorize 页用的就是这个接口）。

    参数名与结构来自学生端 JS：``postAuthorizeLoginNew({code, cpub_k, cpub_v})``。
    实测 cpub_k / cpub_v 只是客户端标识，服务端不会因为占位值拒绝请求，
    所以这里填一个结构合法的占位值即可。
    """
    code = (code or "").strip()
    if not code:
        return LoginResult(False, message="没识别到微信 code —— 请把微信里那条链接整段粘过来")
    placeholder_key = base64.b64encode(b"\x00" * 64).decode("ascii")
    try:
        body = _auth_post(AUTH_WECHAT_V2, {"code": code, "cpub_k": placeholder_key, "cpub_v": VERSION})
    except Exception as exc:
        return LoginResult(False, message=str(exc))

    if body.get("encrypted"):
        return LoginResult(
            False,
            message="服务端这次返回了加密数据，这条登录路走不通；请改用账号密码登录，或用抓包方式",
        )
    return _result_from(body)


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
    """【已失效，勿用】旧的扫码授权链路。

    实测（2026-10）走这条路扫码后服务端返回「微信认证失败」：``/Thirdpart/Authorize``
    是第三方授权用途，新版客户端登录已改为 handshake + 公钥加密传输。
    保留代码仅为兼容历史调用；请改用 ``login_with_password`` / ``login_with_user_id``。
    """
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
