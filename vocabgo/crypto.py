# -*- coding: utf-8 -*-
"""词达人（vocabgo）协议层：请求签名 sign + 响应 jv 混淆解码。

协议要点（整理自公开开源实现，见 docs/开源项目调研.md）：

* Salt        : ajfajfamsnfaflfasakljdlalkflak
* 签名算法    : md5( sorted(k=v 用 & 连接) + SALT )，参与签名的是含 timestamp/version 的完整 body
* 响应结构    : {"code":1, "jv":"2_1254", "data":"<base64 字符串>", ...}
                - jv 缺省/"0"     : data 为明文对象，直接可用
                - jv == "1"       : 去掉 base64 串前 32 个字符
                - jv == "2_xxxx"  : 按规则表顺序删除若干字符段
                - jv == "3_xxxx"  : 先删字符段，再把 base64 切成 avg 段并按 loc 重排
* data 解出来是 JSON 字符串（部分题型是列表/对象）

本模块同时提供 encode_jv()：jv 的逆运算，用于本地 mock server 与单元测试，
保证解码逻辑被真实数据链路验证过，而不是只在真实接口上“碰运气”。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

SALT = "ajfajfamsnfaflfasakljdlalkflak"
VERSION = "2.7.0.260507_01"

# 服务端下发的混淆方案表：s = 起始下标，n = 删除字符数（顺序执行、作用在逐步变短的串上）
JV_RULES: dict[str, list[dict[str, int]]] = {
    "2_1254": [
        {"s": 0, "n": 3}, {"s": 1, "n": 2}, {"s": 31, "n": 1}, {"s": 41, "n": 2},
        {"s": 51, "n": 1}, {"s": 87, "n": 1}, {"s": 97, "n": 1},
    ],
    "2_10234": [
        {"s": 0, "n": 3}, {"s": 1, "n": 4}, {"s": 39, "n": 1}, {"s": 57, "n": 2},
        {"s": 188, "n": 1}, {"s": 259, "n": 1}, {"s": 316, "n": 2},
    ],
    "2_9214": [
        {"s": 0, "n": 3}, {"s": 1, "n": 4}, {"s": 41, "n": 2}, {"s": 57, "n": 1},
        {"s": 139, "n": 2}, {"s": 272, "n": 1}, {"s": 361, "n": 2},
    ],
    "2_9314": [
        {"s": 0, "n": 3}, {"s": 1, "n": 4}, {"s": 31, "n": 2}, {"s": 60, "n": 1},
        {"s": 152, "n": 2}, {"s": 256, "n": 1},
    ],
    "3_1021": {
        "uc": [{"s": 0, "n": 1}, {"s": 1, "n": 2}, {"s": 33, "n": 1}, {"s": 57, "n": 1}, {"s": 111, "n": 1}],
        "avg": 5, "loc": [1, 3, 2, 0, 4],
    },
    "3_2265": {
        "uc": [{"s": 0, "n": 2}, {"s": 1, "n": 3}, {"s": 33, "n": 1}, {"s": 57, "n": 1}, {"s": 121, "n": 1}],
        "avg": 5, "loc": [3, 1, 0, 4, 2],
    },
    "3_2277": {
        "uc": [{"s": 0, "n": 3}, {"s": 1, "n": 3}, {"s": 32, "n": 2}, {"s": 50, "n": 1}, {"s": 110, "n": 1}],
        "avg": 5, "loc": [3, 1, 0, 4, 2],
    },
}


def md5(text: str) -> str:
    """MD5 十六进制小写摘要。"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def canonical_params(params: dict[str, Any]) -> str:
    """按 key 升序拼 k=v，dict/list 走紧凑 JSON。空值跳过（0 保留）。"""
    parts: list[str] = []
    for key in sorted(params):
        value = params[key]
        if isinstance(value, (dict, list)):
            value = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        if value or value == 0:
            parts.append(f"{key}={value}")
    return "&".join(parts)


def sign(params: dict[str, Any]) -> str:
    """计算 sign 字段（服务端用同一套规则校验 body 完整性）。"""
    return md5(canonical_params(params) + SALT)


def _deobfuscate(data: str, rules: list[dict[str, int]]) -> str:
    """顺序删除若干字符段：d = (s 前) + (s+n 之后)。"""
    out = data
    for rule in rules:
        start, count = rule["s"], rule["n"]
        out = (out[:start] if start else "") + out[start + count:]
    return out


def _obfuscate(data: str, rules: list[dict[str, int]], filler: str = "A") -> str:
    """_deobfuscate 的逆运算：倒序把被删掉的字符段补回去（filler 可任意）。"""
    out = data
    for rule in reversed(rules):
        start, count = rule["s"], rule["n"]
        out = out[:start] + filler * count + out[start:]
    return out


def _b64decode(text: str) -> str:
    """宽松 base64 解码：去掉空白、自动补 '=' 填充。"""
    raw = re.sub(r"\s+", "", text.strip())
    raw += "=" * (-len(raw) % 4)
    return base64.b64decode(raw).decode("utf-8")


def decode_jv(data: str, jv: str) -> Any:
    """把 jv 混淆过的 base64 载荷解成 Python 对象。"""
    if jv == "1":
        return json.loads(_b64decode(data[32:]))

    rule = JV_RULES.get(jv)
    if rule is None:
        # 未登记的新混淆方案：先按纯 base64 试着解，失败就把原文丢回去
        try:
            return json.loads(_b64decode(data))
        except Exception:
            return data

    if isinstance(rule, list):
        return json.loads(_b64decode(_deobfuscate(data, rule)))

    plain = _deobfuscate(data, rule["uc"])
    avg, loc = rule["avg"], rule["loc"]
    chunk = len(plain) // avg
    if chunk <= 0:
        return json.loads(_b64decode(plain))
    pieces = [plain[i * chunk:(i + 1) * chunk] for i in range(avg)]
    out = "".join(pieces[loc.index(i)] for i in range(avg))
    tail = plain[avg * chunk:]
    if tail:
        out += tail
    return json.loads(_b64decode(out))


def encode_jv(obj: Any, jv: str, filler: str = "A") -> str:
    """decode_jv 的逆运算，产出 jv 混淆后的 base64 串（本地 mock / 自测用）。"""
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")

    if jv == "1":
        return filler * 32 + b64

    rule = JV_RULES.get(jv)
    if rule is None:
        return b64

    if isinstance(rule, list):
        return _obfuscate(b64, rule, filler)

    avg, loc = rule["avg"], rule["loc"]
    chunk = len(b64) // avg
    if chunk <= 0:
        return _obfuscate(b64, rule["uc"], filler)
    pieces = [b64[i * chunk:(i + 1) * chunk] for i in range(avg)]
    tail = b64[avg * chunk:]
    # 逆重排：位置 j 上放第 loc[j] 块，解码端的 out[i] = pieces[loc.index(i)] 才能还原
    shuffled = "".join(pieces[loc[j]] for j in range(avg)) + tail
    return _obfuscate(shuffled, rule["uc"], filler)


def unwrap(resp: dict[str, Any]) -> dict[str, Any]:
    """解开响应里的 data 字段（就地替换）。非字符串 / 无 jv 时原样返回。"""
    if not isinstance(resp, dict):
        return resp
    jv = str(resp.get("jv", ""))
    data = resp.get("data")
    if not jv or jv == "0" or not isinstance(data, str):
        return resp
    try:
        resp["data"] = decode_jv(data, jv)
    except Exception as exc:  # 解不开时保留原文，便于排查
        resp["_decode_error"] = f"{type(exc).__name__}: {exc}"
    return resp
