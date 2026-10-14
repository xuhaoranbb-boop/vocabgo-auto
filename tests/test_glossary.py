# -*- coding: utf-8 -*-
"""词库测试：在线词典解析、本地缓存、以及「释义↔选项」匹配的保守性。

重点在于**不能乱猜**：匹配不确定时必须返回 None，交给大模型或试答案。
尤其在只有一次机会的班级任务里，自信地选错比说不知道更糟。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vocabgo import glossary as gl  # noqa: E402


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def youdao_payload(*meanings: str) -> dict:
    """造一个和有道接口同构的返回。"""
    return {"ec": {"word": [{"trs": [{"tr": [{"l": {"i": list(meanings)}}]}]}]}}


class MeaningUnitsTest(unittest.TestCase):
    def test_strips_pos_and_splits(self) -> None:
        units = gl.meaning_units("v. 使弯曲；扭伤（脚踝等）；缠绕")
        self.assertIn("使弯曲", units)
        self.assertIn("扭伤脚踝等", units)
        self.assertIn("缠绕", units)

    def test_overlap_scores(self) -> None:
        # 完全一致
        self.assertAlmostEqual(gl.score_overlap("n. 强调；重点", "强调"), 1.0, delta=0.01)
        # 部分包含
        partial = gl.score_overlap("n. 强调；重点", "重点；强调")
        self.assertGreater(partial, 0.9)
        # 完全无关
        self.assertLess(gl.score_overlap("n. 强调", "n. 子弹"), 0.2)


class LookupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.cache = self.folder / "glossary.json"

    def test_youdao_lookup_and_cache(self) -> None:
        gloss = gl.Glossary(self.cache, logger=lambda _m: None)
        payload = youdao_payload("v. 使弯曲；（使）扭曲变形", "n. 扭动，转动")
        with patch("vocabgo.net.request", return_value=FakeResponse(payload)) as request:
            first = gloss.lookup("twist")
        self.assertEqual(len(first), 2)
        self.assertIn("使弯曲", first[0])
        # 第二次应该走缓存，不再发请求
        with patch("vocabgo.net.request") as request2:
            second = gloss.lookup("twist")
        self.assertEqual(first, second)
        request2.assert_not_called()
        self.assertEqual(request.call_count, 1)

    def test_cache_persists_to_disk(self) -> None:
        gloss = gl.Glossary(self.cache, logger=lambda _m: None)
        with patch("vocabgo.net.request", return_value=FakeResponse(youdao_payload("n. 公式"))):
            gloss.lookup("formula")
        gloss.flush()
        again = gl.Glossary(self.cache, logger=lambda _m: None)
        with patch("vocabgo.net.request") as request:
            self.assertEqual(again.lookup("formula"), ["n. 公式"])
        request.assert_not_called()

    def test_network_failure_is_soft(self) -> None:
        gloss = gl.Glossary(self.cache, logger=lambda _m: None)
        with patch("vocabgo.net.request", side_effect=OSError("boom")):
            self.assertEqual(gloss.lookup("whatever"), [])
        # 失败不写缓存，下次还会重试
        self.assertEqual(len(gloss), 0)

    def test_offline_dictionary_file_wins(self) -> None:
        """放了本地词库文件（ecdict.json）就优先离线查，不发网络请求。"""
        (self.folder / gl.ECDICT_NAME).write_text(
            json.dumps({"twist": ["v. 扭；扭曲", "n. 转折"]}, ensure_ascii=False), encoding="utf-8"
        )
        gloss = gl.Glossary(self.cache, logger=lambda _m: None)
        with patch("vocabgo.net.request") as request:
            meanings = gloss.lookup("twist")
        self.assertEqual(meanings, ["v. 扭；扭曲", "n. 转折"])
        request.assert_not_called()

    def test_disabled_glossary_never_queries(self) -> None:
        gloss = gl.Glossary(self.cache, logger=lambda _m: None, enabled=False)
        with patch("vocabgo.net.request") as request:
            self.assertEqual(gloss.lookup("twist"), [])
        request.assert_not_called()


class MatchOptionTest(unittest.TestCase):
    def _options(self, *contents: str) -> list[dict]:
        return [{"content": c} for c in contents]

    def test_picks_the_matching_option(self) -> None:
        meanings = ["v. 使弯曲；（使）扭曲变形；扭伤"]
        options = self._options("n. 强调；重点", "n. 子弹", "v. 使弯曲；扭曲", "n. 估算，估价")
        self.assertEqual(gl.match_option(meanings, options), 2)

    def test_refuses_when_two_options_look_alike(self) -> None:
        """两个选项都很像时不硬选 —— 宁可交给大模型。"""
        meanings = ["n. 强调；重点"]
        options = self._options("强调；重点", "重点；强调")
        self.assertIsNone(gl.match_option(meanings, options))

    def test_refuses_when_nothing_matches(self) -> None:
        meanings = ["n. 公式"]
        options = self._options("n. 子弹", "n. 估算", "n. 装饰")
        self.assertIsNone(gl.match_option(meanings, options))

    def test_empty_inputs(self) -> None:
        self.assertIsNone(gl.match_option([], self._options("a")))
        self.assertIsNone(gl.match_option(["n. 公式"], []))


class MeaningHintTest(unittest.TestCase):
    def test_hint_is_compact(self) -> None:
        hint = gl.meaning_hint(["n. 公式；方程式", "n. 配方"], limit=2)
        self.assertIn("公式", hint)
        self.assertLessEqual(len(hint), 300)

    def test_empty_hint(self) -> None:
        self.assertEqual(gl.meaning_hint([]), "")


class GlossarySolverIntegrationTest(unittest.TestCase):
    """词库真的接进答题链了吗 —— 用一个假客户端跑一遍选择题。"""

    class _FakeClient:
        def __init__(self) -> None:
            self.verified: list = []
            self.submitted = 0

        def verify(self, code, answer):  # 服务端判题
            self.verified.append(answer)
            return {"code": 1, "data": {"answer_result": 1, "topic_code": "next", "answer_corrects": []}}

        def submit(self, code, spent):  # 提交本题
            self.submitted += 1
            return {"code": 1, "data": {"topic_done_num": 1, "topic_total": 1}}

        # 自学任务用不到，但接口留着避免 AttributeError
        def study_verify(self, code, answer):
            return self.verify(code, answer)

        def study_submit(self, code, spent):
            return self.submit(code, spent)

    def test_glossary_answers_choice_question(self) -> None:
        from vocabgo.bank import Bank  # noqa: PLC0415
        from vocabgo.solver import Solver  # noqa: PLC0415

        folder = Path(tempfile.mkdtemp())
        bank = Bank(folder / "bank.json")
        gloss = gl.Glossary(folder / "glossary.json", logger=lambda _m: None)
        # 直接塞缓存，避免真发网络请求
        gloss._cache["twist"] = ["v. 使弯曲；（使）扭曲变形；扭伤"]  # noqa: SLF001

        client = self._FakeClient()
        solver = Solver(client, bank, kind="class", glossary=gloss, probe=False, logger=lambda _m: None)
        topic = {
            "topic_mode": 11,
            "stem": {"content": "He {twisted} the wire into a ring.", "remark": "他把铁丝弯成环。"},
            "topic_code": "c1",
            "options": [
                {"content": "n. 强调；重点", "answer_tag": 0},
                {"content": "n. 子弹", "answer_tag": 1},
                {"content": "v. 使弯曲；扭曲", "answer_tag": 2},
                {"content": "n. 估算，估价", "answer_tag": 3},
            ],
        }
        solver._solve_one(topic)  # noqa: SLF001

        self.assertEqual(client.verified, [2], "应该把词库选中的第 2 项交给服务端")
        self.assertEqual(solver.stats.glossary, 1, "统计里应该记一次词库命中")
        self.assertEqual(client.submitted, 1)

    def test_no_glossary_means_no_hit(self) -> None:
        from vocabgo.bank import Bank  # noqa: PLC0415
        from vocabgo.solver import Solver  # noqa: PLC0415

        folder = Path(tempfile.mkdtemp())
        client = self._FakeClient()
        solver = Solver(client, Bank(folder / "bank.json"), kind="class", probe=False, logger=lambda _m: None)
        topic = {
            "topic_mode": 11,
            "stem": {"content": "He {twisted} the wire.", "remark": ""},
            "topic_code": "c1",
            "options": [{"content": "a", "answer_tag": 0}, {"content": "b", "answer_tag": 1}],
        }
        solver._solve_one(topic)  # noqa: SLF001
        self.assertEqual(solver.stats.glossary, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
