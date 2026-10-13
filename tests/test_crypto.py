# -*- coding: utf-8 -*-
"""协议层单测：签名、参数规范化、jv 混淆往返、unwrap。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vocabgo.crypto import (  # noqa: E402
    JV_RULES,
    SALT,
    canonical_params,
    decode_jv,
    encode_jv,
    md5,
    sign,
    unwrap,
)


class SignTest(unittest.TestCase):
    def test_sign_matches_known_vector(self) -> None:
        """固定向量：md5("a=1&b=2" + SALT)，值由外部独立计算得到。"""
        self.assertEqual(sign({"a": 1, "b": 2}), "cc37c3ec0cf50560bbbc91c2d1a9db64")

    def test_canonical_params_sorted_and_compact(self) -> None:
        self.assertEqual(canonical_params({"b": 2, "a": 1}), "a=1&b=2")
        self.assertEqual(canonical_params({"word_map": {"c:1": ["a", "b"]}}), 'word_map={"c:1":["a","b"]}')
        self.assertEqual(canonical_params({"a": 1, "empty": "", "none": None}), "a=1")
        self.assertEqual(canonical_params({"zero": 0, "a": 1}), "a=1&zero=0")  # 0 必须参与签名

    def test_sign_is_order_independent(self) -> None:
        self.assertEqual(sign({"a": 1, "b": 2}), sign({"b": 2, "a": 1}))

    def test_md5_helper(self) -> None:
        self.assertEqual(md5("abc"), "900150983cd24fb0d6963f7d28e17f72")


class JvCodecTest(unittest.TestCase):
    SAMPLE = {
        "topic_code": "T8",
        "topic_mode": 32,
        "stem": {"content": "___ ___ instances", "remark": "许多例子"},
        "options": [{"content": f"option-{i}", "answer_tag": i} for i in range(4)],
        "answer_result": 0,
        "answer_corrects": [],
        "padding": "x" * 600,  # 保证 base64 长度超过混淆表里的最大下标
    }

    def test_round_trip_all_schemes(self) -> None:
        for jv in JV_RULES:
            with self.subTest(jv=jv):
                encoded = encode_jv(self.SAMPLE, jv)
                self.assertNotEqual(encoded[:20], encode_jv(self.SAMPLE, "0")[:20])
                self.assertEqual(decode_jv(encoded, jv), self.SAMPLE)

    def test_prefix_scheme(self) -> None:
        encoded = encode_jv(self.SAMPLE, "1")
        self.assertTrue(encoded.startswith("A" * 32))
        self.assertEqual(decode_jv(encoded, "1"), self.SAMPLE)

    def test_unknown_scheme_degrades_gracefully(self) -> None:
        encoded = encode_jv(self.SAMPLE, "0")
        self.assertEqual(decode_jv(encoded, "9_9999"), self.SAMPLE)

    def test_unwrap_plain_and_obfuscated(self) -> None:
        plain = {"code": 1, "jv": "0", "data": {"topic_code": "T1"}}
        self.assertEqual(unwrap(dict(plain))["data"], {"topic_code": "T1"})

        jv = "2_1254"
        payload = dict(self.SAMPLE)
        resp = {"code": 1, "jv": jv, "data": encode_jv(payload, jv)}
        self.assertEqual(unwrap(resp)["data"], payload)

    def test_unwrap_keeps_raw_data_on_failure(self) -> None:
        resp = {"code": 1, "jv": "2_1254", "data": "not-base64!!!"}
        out = unwrap(resp)
        self.assertIn("_decode_error", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
