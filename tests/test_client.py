# -*- coding: utf-8 -*-
"""鉴权头测试：abc = md5(UA)、authorization-v = md5("0")，且抓包值优先。

这两个字段是平台的低成本反爬校验（名字故意起得没意义）：
  Abc             = md5(实际发送的 User-Agent)
  Authorization-V = md5("0") / md5("1")
依据：上游原项目 api/request_header.py 的实现。
"""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vocabgo.client import (  # noqa: E402
    DEFAULT_UA,
    DERIVED_AUTH_V,
    DERIVED_AUTH_V_SUBMIT,
    VocabgoClient,
    VocabgoError,
    derive_auth_headers,
)


class DeriveAuthTest(unittest.TestCase):
    def test_abc_is_md5_of_user_agent(self) -> None:
        user_agent = "Mozilla/5.0 (Test) MicroMessenger/8.0.20"
        abc, _auth_v = derive_auth_headers(user_agent)
        self.assertEqual(abc, hashlib.md5(user_agent.encode()).hexdigest())

    def test_constants_are_md5_of_zero_and_one(self) -> None:
        self.assertEqual(DERIVED_AUTH_V, hashlib.md5(b"0").hexdigest())
        self.assertEqual(DERIVED_AUTH_V_SUBMIT, hashlib.md5(b"1").hexdigest())

    def test_abc_follows_user_agent(self) -> None:
        """UA 一换 abc 就得跟着换 —— 这正是平台拿它做「配套校验」的意义。"""
        self.assertNotEqual(derive_auth_headers("UA-A")[0], derive_auth_headers("UA-B")[0])
        self.assertEqual(derive_auth_headers(DEFAULT_UA)[0], hashlib.md5(DEFAULT_UA.encode()).hexdigest())


class EnsureStudyTaskTest(unittest.TestCase):
    """自学任务的 task_id 获取：正路是 Info(task_id=-1)，不是 StartTask。

    坑的来历：StartTask 现在被服务端特殊保护，直接调只回「请重新登录」；
    而 Info 传 task_id=-1 时，服务端会把任务建出来并把真实 id 返回。
    """

    def _client(self) -> VocabgoClient:
        return VocabgoClient("tok")

    def test_uses_existing_id_from_list(self) -> None:
        client = self._client()
        with patch.object(client, "study_task_list", return_value={"data": {"task_list": [
            {"list_id": "CET4_v2_01", "task_id": 167358106},
        ]}}), patch.object(client, "study_task_info") as info:
            self.assertEqual(client.ensure_study_task("CET4_v2", "CET4_v2_01"), 167358106)
        info.assert_not_called()

    def test_creates_via_info_when_list_says_minus_one(self) -> None:
        client = self._client()
        with patch.object(client, "study_task_list", return_value={"data": {"task_list": [
            {"list_id": "CET4_v2_01", "task_id": -1},
        ]}}), patch.object(client, "study_task_info", return_value={"code": 1, "data": {"task_id": 167358123}}) as info:
            self.assertEqual(client.ensure_study_task("CET4_v2", "CET4_v2_01"), 167358123)
        self.assertEqual(info.call_args[0][0], -1, "创建时必须传 task_id=-1")

    def test_falls_back_to_start_task(self) -> None:
        client = self._client()
        with patch.object(client, "study_task_list", return_value={"data": {"task_list": [
            {"list_id": "CET4_v2_01", "task_id": -1},
        ]}}), patch.object(client, "study_task_info", side_effect=VocabgoError("nope")), patch.object(
            client, "study_start_task", return_value={"code": 1, "data": {"task_id": 999}}
        ):
            self.assertEqual(client.ensure_study_task("CET4_v2", "CET4_v2_01"), 999)

    def test_raises_with_actionable_message(self) -> None:
        client = self._client()
        with patch.object(client, "study_task_list", return_value={"data": {"task_list": []}}), patch.object(
            client, "study_task_info", side_effect=VocabgoError("nope")
        ), patch.object(client, "study_start_task", side_effect=VocabgoError("nope")):
            with self.assertRaises(VocabgoError) as ctx:
                client.ensure_study_task("CET4_v2", "CET4_v2_01")
        self.assertIn("先在词达人里打开这条课程", str(ctx.exception))


class ClientHeaderTest(unittest.TestCase):
    def test_derive_when_blank(self) -> None:
        client = VocabgoClient(usertoken="tok")
        self.assertEqual(client.session.headers["abc"], hashlib.md5(DEFAULT_UA.encode()).hexdigest())
        self.assertEqual(client.session.headers["authorization-v"], DERIVED_AUTH_V)
        self.assertTrue(client.abc_derived)
        self.assertTrue(client.auth_v_derived)
        self.assertIn("推算", client.auth_source())

    def test_captured_values_win(self) -> None:
        client = VocabgoClient(usertoken="tok", abc="real-abc", auth_v="real-auth-v")
        self.assertEqual(client.session.headers["abc"], "real-abc")
        self.assertEqual(client.session.headers["authorization-v"], "real-auth-v")
        self.assertFalse(client.abc_derived)
        self.assertFalse(client.auth_v_derived)
        self.assertEqual(client.auth_source(), "abc / authorization-v 均来自抓包")

    def test_abc_tracks_custom_user_agent(self) -> None:
        """自定义 UA 时 abc 必须跟着这个 UA 算，不能还用默认 UA 的哈希。"""
        custom = "Mozilla/5.0 (Custom) WindowsWechat"
        client = VocabgoClient(usertoken="tok", user_agent=custom)
        self.assertEqual(client.session.headers["user-agent"], custom)
        self.assertEqual(client.session.headers["abc"], hashlib.md5(custom.encode()).hexdigest())

    def test_partial_config(self) -> None:
        """只填了 abc、没填 authorization-v：各自独立判定。"""
        client = VocabgoClient(usertoken="tok", abc="only-abc")
        self.assertEqual(client.session.headers["abc"], "only-abc")
        self.assertEqual(client.session.headers["authorization-v"], DERIVED_AUTH_V)
        self.assertFalse(client.abc_derived)
        self.assertTrue(client.auth_v_derived)
        self.assertIn("authorization-v", client._auth_hint())  # noqa: SLF001

    def test_hint_only_when_derived(self) -> None:
        self.assertEqual(VocabgoClient(usertoken="tok", abc="a", auth_v="b")._auth_hint(), "")  # noqa: SLF001
        hint = VocabgoClient(usertoken="tok")._auth_hint()  # noqa: SLF001
        self.assertIn("抓包", hint)
        self.assertIn("abc", hint)

    def test_missing_token_rejected(self) -> None:
        with self.assertRaises(VocabgoError):
            VocabgoClient(usertoken="")


if __name__ == "__main__":
    unittest.main(verbosity=2)
