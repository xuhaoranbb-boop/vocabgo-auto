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
import copy
import datetime
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
AUTH_KEY_VERSION = "v1"          # 客户端上报的 key 版本
AUTH_HKDF_INFO = b"vcg-auth-aes"  # HKDF 的 info，来自网页端源码

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


def _generate_keypair() -> tuple[Any, str]:
    """本地生成 P-256 密钥对，返回 (私钥, base64(SPKI 公钥))。

    与网页端一致：``crypto.subtle.generateKey({name:"ECDH", namedCurve:"P-256"})``
    然后 ``exportKey("spki", publicKey)`` 再 base64。
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_b64 = base64.b64encode(
        private_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).decode("ascii")
    return private_key, public_b64


def decrypt_auth_response(envelope: dict[str, Any], private_key: Any) -> dict[str, Any]:
    """解密「服务端用我们公钥加密」的响应。

    与网页端一致：

    1. ECDH(我们的私钥, 服务端公钥 spub_k) → 共享密钥
    2. HKDF-SHA256(共享密钥, salt, info="vcg-auth-aes") → 256 位 AES 密钥
    3. AES-GCM(iv, cipher_text, aad) 解密 → 明文 JSON
    """
    import json as _json

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else envelope
    handshake = data.get("handshake") or {}
    payload = data.get("payload") or {}
    if isinstance(handshake, str):
        handshake = _json.loads(handshake)
    if isinstance(payload, str):
        payload = _json.loads(payload)

    server_public_b64 = handshake.get("spub_k") or handshake.get("serverPublicKey") or ""
    salt_b64 = handshake.get("salt") or ""
    if not server_public_b64 or not salt_b64:
        raise ValueError("响应里缺少 spub_k / salt，无法解密")

    server_key = serialization.load_der_public_key(base64.b64decode(server_public_b64))
    shared_secret = private_key.exchange(ec.ECDH(), server_key)
    aes_key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=base64.b64decode(salt_b64), info=AUTH_HKDF_INFO
    ).derive(shared_secret)

    iv = base64.b64decode(payload.get("iv") or "")
    cipher_text = base64.b64decode(payload.get("cipher_text") or payload.get("cipherText") or "")
    aad = payload.get("aad")
    plain = AESGCM(aes_key).decrypt(iv, cipher_text, aad.encode("utf-8") if aad else None)
    return _json.loads(plain.decode("utf-8"))


def login_with_wechat_code(code: str) -> LoginResult:
    """用微信授权回来的 code 换令牌（学生端 /authorize 页用的就是这个接口）。

    网页端源码里的完整链路：

    - 本地生成 ECDH P-256 密钥对，公钥 base64(SPKI) 作为 ``cpub_k`` 一起提交
    - 服务端用这个公钥加密响应（所以公钥非法时它直接报「加密失败！」）
    - 客户端用私钥解出明文，明文里才是 ``token``
    """
    code = (code or "").strip()
    if not code:
        return LoginResult(False, message="没识别到微信 code —— 请把微信里那条链接整段粘过来")

    try:
        private_key, cpub_k = _generate_keypair()
    except Exception as exc:
        return LoginResult(False, message=f"本机缺少加密组件（需要 cryptography 库）：{exc}")

    try:
        body = _auth_post(
            AUTH_WECHAT_V2, {"code": code, "cpub_k": cpub_k, "cpub_v": AUTH_KEY_VERSION}
        )
    except Exception as exc:
        return LoginResult(False, message=str(exc))

    data = body.get("data")
    if body.get("code") == 1 and isinstance(data, dict) and data.get("encrypted"):
        try:
            inner = decrypt_auth_response(body, private_key)
        except Exception as exc:
            return LoginResult(False, message=f"服务端返回了加密数据，但解密失败：{exc}")
        token = str(inner.get("token") or "")
        if token:
            return LoginResult(True, token=token)
        return LoginResult(False, message=f"解密成功但响应里没有令牌：{str(inner)[:120]}")

    if body.get("code") == 1 and isinstance(data, dict) and data.get("token"):
        return LoginResult(True, token=str(data["token"]))  # 万一服务端返回明文

    result = _result_from(body)
    if "微信服务异常" in result.message:
        result.message = "微信服务异常 —— 一般是这条链接里的 code 已经过期或用过了（只能用一次、约 5 分钟），请重新扫码"
    elif "加密失败" in result.message:
        result.message = "服务端没能用我们的公钥加密响应，请把这条错误反馈给作者"
    return result


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
    """把 token 写进配置文件（保留其余配置），并记下获取时间。

    记时间是因为：词达人的令牌**有效期约 6 小时**（实测 10:16 登录 → 16:22 失效），
    到点后所有请求都会回 11003「需安全验证」/「请重新登录」。有了时间戳就能提前提醒。
    """
    from .settings import DEFAULT_SETTINGS

    config: dict[str, Any] = {}
    if config_file.exists():
        try:
            config = json.loads(config_file.read_text(encoding="utf-8"))
        except Exception:
            config = {}
    for section, defaults in DEFAULT_SETTINGS.items():
        # 注意：必须 deepcopy！直接传 defaults 会把「全局默认配置」这个对象塞进 config，
        # 后面一改就污染了 DEFAULT_SETTINGS，之后所有 load_settings 都会带上旧值。
        config.setdefault(section, copy.deepcopy(defaults))
    config.setdefault("auth", {})["usertoken"] = token
    config.setdefault("runtime", {})["token_saved_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_file


TOKEN_LIFETIME_HOURS = 6.0  # 实测：词达人令牌约 6 小时过期


def token_age_hours(config: dict[str, Any] | None) -> float | None:
    """令牌拿到多久了（小时）。没有记录时间就返回 None。"""
    stamp = str(((config or {}).get("runtime") or {}).get("token_saved_at") or "")
    if not stamp:
        return None
    try:
        saved = datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return (datetime.datetime.now() - saved).total_seconds() / 3600.0


def token_expiry_hint(config: dict[str, Any] | None) -> str:
    """按令牌年龄给一句人话提醒；还年轻就返回空串。"""
    age = token_age_hours(config)
    if age is None:
        return ""
    if age >= TOKEN_LIFETIME_HOURS:
        return (
            f"⏰ 这个令牌已经拿到 {age:.1f} 小时了（有效期约 {TOKEN_LIFETIME_HOURS:.0f} 小时），"
            "基本肯定已过期 —— 请点「登录获取令牌」重新登录。"
        )
    if age >= TOKEN_LIFETIME_HOURS - 0.5:
        return (
            f"⏰ 提醒：这个令牌已经用了 {age:.1f} 小时，有效期约 {TOKEN_LIFETIME_HOURS:.0f} 小时，"
            "建议刷完这轮就重新登录，免得刷到一半断掉。"
        )
    return ""


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
