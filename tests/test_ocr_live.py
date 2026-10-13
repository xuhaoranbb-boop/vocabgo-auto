# -*- coding: utf-8 -*-
"""真机 OCR 测试：真生成一张图 → 真跑 OCR → 真接进匹配链。

和 test_vision.py 的区别：那边用假读图器验证「接线」，这边用本机 tesseract 验证
「图片文本能不能真被认出来并选中正确选项」。缺 tesseract / Pillow 时自动跳过。
"""

from __future__ import annotations

import base64
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vocabgo.matcher import match_choice  # noqa: E402
from vocabgo.vision import (  # noqa: E402
    ImageReader,
    hydrate_topic,
    probe_backend,
    render_probe_image,
    resolve_tesseract,
)


def _silent(_msg: str) -> None:
    pass


class LiveOcrTest(unittest.TestCase):
    def setUp(self) -> None:
        if resolve_tesseract() is None:
            self.skipTest("本机没有 tesseract，跳过真机 OCR 测试")
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("没有 Pillow，无法生成测试图")
        self.workdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)

    def make_reader(self) -> ImageReader:
        return ImageReader(backend="tesseract", logger=_silent)

    def to_data_uri(self, text: str) -> str:
        image = render_probe_image(text, self.workdir / f"{abs(hash(text))}.png")
        self.assertIsNotNone(image, "生成测试图失败")
        return "data:image/png;base64," + base64.b64encode(image.read_bytes()).decode("ascii")

    def test_reads_english_word(self) -> None:
        text = self.make_reader().read(self.to_data_uri("abandon"))
        self.assertIsNotNone(text, "OCR 没返回结果")
        self.assertIn("abandon", text.lower())

    def test_reads_sentence_with_braces(self) -> None:
        """mode=11 的题干长这样，OCR 出来要还能解析出 {} 里的词。"""
        sentence = "He decided to {abandon} the plan."
        text = self.make_reader().read(self.to_data_uri(sentence))
        self.assertIsNotNone(text)
        self.assertIn("abandon", text.lower())

    def test_ocr_feed_matcher_picks_right_option(self) -> None:
        """整条链路：图片题干 → OCR → 词义表匹配 → 选中正确选项。"""
        topic = {
            "topic_mode": 31,
            "stem": {"content": self.to_data_uri("abandon"), "remark": ""},
            "options": [
                {"content": "n. 好处；益处", "answer_tag": 0},
                {"content": "v. 放弃；抛弃", "answer_tag": 1},
                {"content": "adj. 关键的", "answer_tag": 2},
            ],
        }
        reader = self.make_reader()
        self.assertEqual(hydrate_topic(topic, reader, logger=_silent), 1)
        self.assertIn("abandon", topic["stem"]["content"].lower())

        word_defs = {"abandon": ["v. 放弃；抛弃", "n. 放纵；无拘束"]}
        self.assertEqual(match_choice(topic, word_defs), 1, "OCR 后应能直接匹配到正确释义")

    def test_probe_backend_reports_success(self) -> None:
        report = probe_backend(self.make_reader(), "benefit")
        self.assertTrue(report["tesseract"], "应能定位到 tesseract 可执行文件")
        self.assertTrue(report["ok"], f"体检应通过，实际: {report}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
