# -*- coding: utf-8 -*-
"""词达人 HTTP 客户端：鉴权、签名、解混淆、班级任务 / 自学任务两套接口。"""

from __future__ import annotations

import re
import time
from typing import Any

import requests

from . import net
from .crypto import VERSION, md5, sign, unwrap

CLASS_BASE = "https://app.vocabgo.com/studentv1/api"
STUDY_BASE = "https://app.vocabgo.com/student/api"

# 词达人学生端在 PC 微信里的真实 UA，直接用可以减少风控关注
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) "
    "WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf254173b) XWEB/19027 Flue"
)

# 图片/字号参数：客户端本来就会带，服务端用于返回适配的题干图片
RENDER_PARAMS = {
    "opt_img_w": 2300,
    "opt_font_size": 128,
    "opt_font_c": "#000000",
    "it_img_w": 2702,
    "it_font_size": 144,
}

# ── 鉴权头的构成（这就是「abc 字段」的答案）──
#
# 平台在请求头里放了两道低成本反爬校验，名字故意起得没意义：
#
#   Abc             = md5(实际发送的 User-Agent)
#                     → 服务端比对「UA 和 Abc 是否配套」，UA 一换值就得跟着换
#   Authorization-V = md5("0") 或 md5("1")
#                     → 客户端版本分支标记，实测是固定值
#
# 依据：上游原项目 api/request_header.py 里写着
#   "Abc": encrypt_md5(user_age)                      # user_age 其实是 UA 字符串
#   "Authorization-V": "cfcd208495d565ef66e7dff9f98764da"   # 正好是 md5("0")
#   （提交类接口另用 "c4ca4238a0b923820dcc509a6f75849b" = md5("1")）
#
# 所以这两个值可以直接推算，不抓包也能跑；抓包拿到的真值优先级更高（万一平台升级了校验）。
DERIVED_AUTH_V = "cfcd208495d565ef66e7dff9f98764da"          # md5("0")
DERIVED_AUTH_V_SUBMIT = "c4ca4238a0b923820dcc509a6f75849b"   # md5("1")
AUTH_HINT_KEYWORDS = ("登录", "授权", "token", "签名", "sign", "abc", "鉴权", "非法", "禁止", "校验")


def derive_auth_headers(user_agent: str) -> tuple[str, str]:
    """按 UA 推算 (abc, authorization-v) —— 不抓包时的兜底。"""
    return md5(user_agent), DERIVED_AUTH_V


class VocabgoError(RuntimeError):
    """接口返回异常（code != 1 或结构不符合预期）。"""

    def __init__(self, message: str, resp: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.resp = resp or {}


def now_ms() -> int:
    return int(time.time() * 1000)


class VocabgoClient:
    """一个学生身份 = 一个 client。线程不安全，请单线程使用。"""

    def __init__(
        self,
        usertoken: str,
        abc: str = "",
        auth_v: str = "",
        *,
        version: str = VERSION,
        user_agent: str = "",
        timeout: float = 20.0,
        debug: bool = False,
        class_base: str = CLASS_BASE,
        study_base: str = STUDY_BASE,
        host_header: str = "app.vocabgo.com",
    ) -> None:
        if not usertoken:
            raise VocabgoError("缺少 UserToken，请先抓包获取（见 README）")
        self.version = version
        self.timeout = timeout
        self.debug = debug
        self.class_base = class_base
        self.study_base = study_base
        # abc / authorization-v 留空就按 UA 推算（抓包填了就优先用真值）
        self.user_agent = user_agent or DEFAULT_UA
        self.abc_derived = not (abc or "").strip()
        self.auth_v_derived = not (auth_v or "").strip()
        derived_abc, derived_auth_v = derive_auth_headers(self.user_agent)
        self.session = net.make_session()  # 默认直连（忽略系统/环境代理），代理策略见 net.py
        self.session.headers.update(
            {
                "host": host_header,
                "usertoken": usertoken,
                "abc": (abc or "").strip() or derived_abc,
                "authorization-v": (auth_v or "").strip() or derived_auth_v,
                "x-requested-with": "XMLHttpRequest",
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "origin": "https://app.vocabgo.com",
                "referer": "https://app.vocabgo.com/student/",
                "user-agent": self.user_agent,
            }
        )

    def auth_source(self) -> str:
        """给界面/体检用：说明这两个鉴权字段是真值还是推算值。"""
        parts = []
        if self.abc_derived:
            parts.append("abc 按 UA 推算")
        if self.auth_v_derived:
            parts.append("authorization-v 用默认值")
        return "，".join(parts) if parts else "abc / authorization-v 均来自抓包"

    def _auth_hint(self) -> str:
        if not (self.abc_derived or self.auth_v_derived):
            return ""
        fields = "、".join(
            name
            for name, derived in (("abc", self.abc_derived), ("authorization-v", self.auth_v_derived))
            if derived
        )
        return (
            f"\n   提示：{fields} 目前是按 UA 自动推算的（md5(UA) / md5(\"0\")）。"
            "如果一直报错，请用 tools/mitm_capture.py 抓包，把真实值填进界面上的对应字段。"
        )

    # ── 底层请求 ──
    def _request(self, method: str, url: str, *, params=None, body=None) -> dict[str, Any]:
        if method == "GET":
            params = {**(params or {}), "timestamp": now_ms(), "version": self.version, "app_type": 1}
        else:
            payload = {**(body or {}), "timestamp": now_ms(), "version": self.version}
            payload["sign"] = sign(payload)
            payload["app_type"] = 1

        try:
            if method == "GET":
                response = self.session.get(url, params=params, timeout=self.timeout)
            else:
                response = self.session.post(url, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            # 网络层问题（代理没开、断网、超时）翻译成人话，别丢一串英文给用户
            raise VocabgoError(net.friendly_error(exc, url)) from exc

        if self.debug:
            print(f"-> {method} {url} {params if method == 'GET' else body}")
        try:
            data = response.json()
        except ValueError as exc:
            raise VocabgoError(
                net.friendly_error(exc, url) + f"\n   HTTP {response.status_code}，返回内容：{response.text[:160]}"
            ) from exc
        if self.debug:
            print(f"<- {response.status_code} {data}")
        data = unwrap(data)
        if data.get("code") not in (1, None):
            message = f"接口报错 code={data.get('code')} msg={data.get('msg') or data.get('message')}"
            text = str(data.get("msg") or data.get("message") or "").lower()
            if response.status_code >= 400 or any(word in text for word in AUTH_HINT_KEYWORDS):
                message += self._auth_hint()
            raise VocabgoError(message, data)
        return data

    def _get(self, path: str, params: dict[str, Any], base: str | None = None) -> dict[str, Any]:
        return self._request("GET", (base or self.class_base) + path, params=params)

    def _post(self, path: str, body: dict[str, Any], base: str | None = None) -> dict[str, Any]:
        return self._request("POST", (base or self.class_base) + path, body=body)

    # ── 班级任务 ──
    def page_task(self, page: int = 1, size: int = 50, search_type: str = "0") -> dict[str, Any]:
        """search_type: 0=全部 1=进行中 2=已完成"""
        return self._post(
            "/Student/ClassTask/PageTask",
            {"search_type": search_type, "page_count": page, "page_size": size},
        )

    def task_info(self, task_id: int, release_id: int) -> dict[str, Any]:
        return self._get("/Student/ClassTask/Info", {"task_id": task_id, "release_id": release_id})

    def chose_word_list(self, task_id: int) -> dict[str, Any]:
        return self._get("/Student/ClassTask/ChoseWordList", {"task_id": task_id, "task_type": 1})

    def submit_chose_word(self, task_id: int, word_map: dict[str, list[str]]) -> dict[str, Any]:
        return self._post(
            "/Student/ClassTask/SubmitChoseWord",
            {
                "task_id": task_id,
                "word_map": word_map,
                "chose_err_item": 1,
                "reset_chose_words": 1,  # 重置已选词：放弃旧进度，全部重练一遍刷满分
            },
        )

    def start_answer(self, task_id: int, release_id: int) -> dict[str, Any]:
        return self._get(
            "/Student/ClassTask/StartAnswer",
            {"task_id": task_id, "task_type": 1, "release_id": release_id, **RENDER_PARAMS},
        )

    def verify(self, topic_code: str, answer: Any) -> dict[str, Any]:
        return self._post("/Student/ClassTask/VerifyAnswer", {"topic_code": topic_code, "answer": answer})

    def submit(self, topic_code: str, time_spent: int) -> dict[str, Any]:
        return self._post(
            "/Student/ClassTask/SubmitAnswerAndSave",
            {"topic_code": topic_code, "time_spent": time_spent, **RENDER_PARAMS},
        )

    def signin(self) -> dict[str, Any]:
        return self._post("/Student/TaskStudentSignin/Do", {})

    # ── 自学任务 ──
    def study_task_list(self, course_id: str = "CET4_v2") -> dict[str, Any]:
        return self._get("/Student/StudyTask/List", {"course_id": course_id}, base=self.study_base)

    def study_start_task(self, course_id: str, list_id: Any, task_type: int = 3, grade: int = 2) -> dict[str, Any]:
        return self._post(
            "/Student/StudyTask/StartTask",
            {"course_id": course_id, "list_id": list_id, "task_type": task_type, "grade": grade},
            base=self.study_base,
        )

    def study_task_info(self, task_id: Any, course_id: str, list_id: Any, task_type: int = 3, grade: int = 2) -> dict[str, Any]:
        return self._get(
            "/Student/StudyTask/Info",
            {"task_id": task_id, "course_id": course_id, "list_id": list_id, "task_type": task_type, "grade": grade},
            base=self.study_base,
        )

    def study_chose_word_list(self, task_id: Any, course_id: str, list_id: Any, task_type: int = 3, grade: int = 2) -> dict[str, Any]:
        return self._get(
            "/Student/StudyTask/ChoseWordList",
            {"task_id": task_id, "course_id": course_id, "list_id": list_id, "task_type": task_type, "grade": grade},
            base=self.study_base,
        )

    def study_submit_chose_word(self, task_id: Any, course_id: str, list_id: Any, word_map: dict[str, list[str]], task_type: int = 3, grade: int = 2) -> dict[str, Any]:
        return self._post(
            "/Student/StudyTask/SubmitChoseWord",
            {
                "task_id": task_id,
                "task_type": task_type,
                "grade": grade,
                "course_id": course_id,
                "list_id": list_id,
                "word_map": word_map,
                "chose_err_item": 1,
                "reset_chose_words": 1,
            },
            base=self.study_base,
        )

    def study_start_answer(self, task_id: Any, course_id: str, list_id: Any, task_type: int = 3, grade: int = 2) -> dict[str, Any]:
        return self._get(
            "/Student/StudyTask/StartAnswer",
            {"task_id": task_id, "task_type": task_type, "grade": grade, "course_id": course_id,
             "list_id": list_id, **RENDER_PARAMS},
            base=self.study_base,
        )

    def study_verify(self, topic_code: str, answer: Any) -> dict[str, Any]:
        return self._post("/Student/StudyTask/VerifyAnswer", {"topic_code": topic_code, "answer": answer}, base=STUDY_BASE)

    def study_submit(self, topic_code: str, time_spent: int) -> dict[str, Any]:
        return self._post(
            "/Student/StudyTask/SubmitAnswerAndSave",
            {"topic_code": topic_code, "time_spent": time_spent, **RENDER_PARAMS},
            base=self.study_base,
        )

    # ── 工具 ──
    @staticmethod
    def pick_topic(resp: dict[str, Any]) -> dict[str, Any] | None:
        """从 StartAnswer / SubmitAnswerAndSave 的响应里取出下一题。"""
        data = (resp or {}).get("data") or {}
        if isinstance(data, dict) and data.get("topic_code"):
            return data
        if isinstance(data, dict):
            for key in ("topic_info", "topic", "next_topic"):
                topic = data.get(key)
                if isinstance(topic, dict) and topic.get("topic_code"):
                    return topic
            topic_list = data.get("topic_list") or []
            if topic_list and isinstance(topic_list[0], dict):
                return topic_list[0]
        return None

    def token_alive(self) -> bool:
        """轻量探活：能拿到任务列表就说明 token 有效。"""
        try:
            self.page_task(page=1, size=1)
            return True
        except VocabgoError:
            return False


def extract_words(topic: dict[str, Any]) -> list[str]:
    """mode=32 组词题的选项词（用于拼短语）。"""
    return [str(opt.get("content", "")).strip() for opt in (topic.get("options") or []) if opt.get("content")]


def join_phrase(answer: str) -> str:
    """把 "in many instances" / "in... many..." 归一成 "in,many,instances" 提交格式。"""
    cleaned = re.sub(r"(\.\.\.|…)", " ", str(answer))
    tokens = [tok for tok in re.split(r"[\s,]+", cleaned.strip()) if tok]
    return ",".join(tokens)
