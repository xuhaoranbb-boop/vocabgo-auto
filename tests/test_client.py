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
