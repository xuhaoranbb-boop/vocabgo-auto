# -*- coding: utf-8 -*-
"""端到端测试：打本地 mock 服务，验证「签名 → 解混淆 → 判题 → 保存」整条链路。

关注两个结论：
  1. 开启 probe 时，服务端记录的每一题最终作答都是正确的（高分的前提）
  2. 关闭 probe（只靠题库 + 规则）会掉分，且题库能让第二次跑满分 —— 说明缓存真的在起作用
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_server import serve  # noqa: E402
from vocabgo.bank import Bank  # noqa: E402
from vocabgo.client import VocabgoClient  # noqa: E402
from vocabgo.solver import Solver  # noqa: E402


class StubReader:
    """假读图器：无论什么图都返回 mock 里那道图片题的正确答案。"""

    def __init__(self, text: str = "abandon") -> None:
        self.text = text
        self.calls = 0

    def read(self, _ref) -> str:
        self.calls += 1
        return self.text


class SolverE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self.server, self.base, self.state = serve(0)
        self.addCleanup(self.server.shutdown)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.bank_path = Path(self.tmpdir.name) / "bank.json"

    # ── 工具 ──
    def make_client(self) -> VocabgoClient:
        return VocabgoClient(
            usertoken="mock-token",
            abc="mock-abc",
            auth_v="mock-auth-v",
            class_base=self.base,
            study_base=self.base,
            host_header="127.0.0.1",
        )

    def inspect(self) -> dict:
        return requests.get(self.base + "/__inspect", timeout=5).json()["data"]

    def reset_server(self) -> None:
        requests.post(self.base + "/__reset", timeout=5)

    def run_task(self, probe: bool, bank_path: Path | None = None):
        bank = Bank(bank_path or self.bank_path)
        solver = Solver(
            self.make_client(),
            bank,
            kind="class",
            probe=probe,
            pace=(0.0, 0.0),
            llm=None,
            logger=lambda _msg: None,
        )
        stats = solver.run(task_id=1001, release_id=2001)
        return stats, bank

    # ── 用例 ──
    def test_full_run_with_probe_saves_all_correct(self) -> None:
        stats, bank = self.run_task(probe=True)
        info = self.inspect()

        self.assertEqual(stats.total, 13)
        self.assertEqual(len(info["saved"]), 13, "每一题都应该被提交")
        self.assertEqual(info["saved_wrong"], [], "开启试答案后不该留下错误作答")
        self.assertEqual(info["sign_fail"], 0, "所有请求的 sign 都必须通过服务端校验")
        self.assertGreater(info["sign_ok"], 10)
        self.assertEqual(stats.wrong_saved, 0)

        # 策略分布：规则匹配是主力，试答案补齐生词，缓存命中重复题
        self.assertGreaterEqual(stats.match, 5, "mode=0 学到词义后，选择题应能直接匹配")
        self.assertGreaterEqual(stats.probe, 2, "生词应走试答案链路")
        self.assertGreaterEqual(stats.cache, 1, "重复题应命中题库")
        self.assertEqual(stats.llm, 0)

        # 混淆方案确实被轮换使用，说明解码覆盖了多条分支
        self.assertGreaterEqual(len(info["jv_used"]), 2)
        self.assertTrue(any(jv.startswith("2_") or jv.startswith("3_") for jv in info["jv_used"]))

        # 题库落盘
        self.assertGreaterEqual(len(bank), 5)
        self.assertTrue(self.bank_path.exists())

    def test_without_probe_accuracy_drops(self) -> None:
        """对照组：不试答案、又没有 LLM 兜底时，生词只能盲选 → 服务端会记下错答。"""
        stats, _bank = self.run_task(probe=False)
        info = self.inspect()
        self.assertTrue(info["saved_wrong"], "关闭试答案后应当出现错答（证明 probe 不是摆设）")
        self.assertGreaterEqual(stats.wrong_saved, 1)

    def test_bank_makes_second_run_clean(self) -> None:
        """跑过一轮后题库已积累，第二轮即使关掉试答案也应全对。"""
        self.run_task(probe=True)
        self.reset_server()

        stats, _bank = self.run_task(probe=False)
        info = self.inspect()

        self.assertEqual(info["saved_wrong"], [], "题库缓存应覆盖全部题目")
        self.assertEqual(stats.probe, 0)
        self.assertGreaterEqual(stats.cache, 6, "绝大多数题应从题库直接命中")
        self.assertEqual(stats.errors, 0)

    def test_image_stem_ocr_turns_probe_into_match(self) -> None:
        """题干是图片时的差分测试：有读图层能走规则匹配，没有就只能靠试答案。

        两次跑用独立题库文件，避免第二轮直接命中缓存而掩盖差异。
        """
        baseline, _ = self.run_task(probe=True, bank_path=Path(self.tmpdir.name) / "baseline.json")
        self.reset_server()

        reader = StubReader("abandon")
        solver = Solver(
            self.make_client(),
            Bank(Path(self.tmpdir.name) / "with_ocr.json"),
            kind="class",
            probe=True,
            reader=reader,
            pace=(0.0, 0.0),
            llm=None,
            logger=lambda _msg: None,
        )
        stats = solver.run(task_id=1001, release_id=2001)

        self.assertGreaterEqual(reader.calls, 1, "读图接口必须被调用")
        self.assertEqual(stats.ocr, 1, "统计里应记一次读图")
        self.assertEqual(stats.match, baseline.match + 1, "读图后该题应直接命中规则匹配")
        self.assertEqual(stats.probe, baseline.probe - 1, "该题不再需要试答案")
        self.assertEqual(self.inspect()["saved_wrong"], [])

    def test_token_alive_and_task_listing(self) -> None:
        client = self.make_client()
        self.assertTrue(client.token_alive())
        records = client.page_task().get("data", {}).get("records") or []
        self.assertEqual(records[0]["task_name"], "模拟班级任务")


if __name__ == "__main__":
    unittest.main(verbosity=2)
