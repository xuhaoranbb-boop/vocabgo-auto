# -*- coding: utf-8 -*-
"""读图层单测：图片识别、题目字段替换、验证码清洗、缓存与后端选择。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vocabgo.vision import (  # noqa: E402
    ImageReader,
    ImageRef,
    detect_image,
    hydrate_topic,
    load_reader_from_config,
    resolve_tesseract,
    topic_image_fields,
)

PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="


class StubReader:
    """假读图器：记录被调用次数，固定返回一段文本。"""

    def __init__(self, text: str = "abandon") -> None:
        self.text = text
        self.calls = 0

    def read(self, _ref) -> str:
        self.calls += 1
        return self.text


class DetectImageTest(unittest.TestCase):
    def test_detects_data_uri(self) -> None:
        ref = detect_image(f"data:image/png;base64,{PNG_B64}")
        self.assertIsNotNone(ref)
        self.assertEqual(ref.kind, "base64")
        self.assertEqual(ref.payload, PNG_B64)

    def test_detects_url(self) -> None:
        ref = detect_image("https://resource-cdn.vocabgo.com/stem/12345.png")
        self.assertEqual(ref.kind, "url")
        ref2 = detect_image("http://x.y/z.jpeg?v=2")
        self.assertIsNotNone(ref2)
        self.assertEqual(ref2.kind, "url")

    def test_detects_bare_base64(self) -> None:
        self.assertEqual(detect_image(PNG_B64).kind, "base64")
        self.assertEqual(detect_image("/9j/4AAQSkZJRgABAQ==").kind, "base64")

    def test_plain_text_is_not_image(self) -> None:
        for value in (
            "abandon",
            "noun 道路；航线；路线",
            "Researchers are trying to get at the same information through an indirect {route}.",
            "",
            "   ",
            None,
            123,
        ):
            self.assertIsNone(detect_image(value), f"{value!r} 不该被当成图片")

    def test_http_without_image_ext_is_not_image(self) -> None:
        self.assertIsNone(detect_image("https://app.vocabgo.com/student/api/Info"))


class TopicFieldsTest(unittest.TestCase):
    def make_topic(self, stem: str, options: list[str]) -> dict:
        return {
            "topic_mode": 31,
            "stem": {"content": stem},
            "options": [{"content": text, "answer_tag": i} for i, text in enumerate(options)],
        }

    def test_scan_finds_stem_and_option_images(self) -> None:
        topic = self.make_topic(f"data:image/png;base64,{PNG_B64}", ["A", f"data:image/png;base64,{PNG_B64}", "C"])
        fields = topic_image_fields(topic)
        self.assertEqual([path for path, _ in fields], ["stem.content", "options[1].content"])

    def test_hydrate_replaces_in_place(self) -> None:
        topic = self.make_topic(f"data:image/png;base64,{PNG_B64}", ["abandon", "benefit"])
        reader = StubReader("eliminate")
        replaced = hydrate_topic(topic, reader, logger=lambda _m: None)
        self.assertEqual(replaced, 1)
        self.assertEqual(topic["stem"]["content"], "eliminate")
        self.assertEqual(topic["options"][0]["content"], "abandon")  # 非图片字段不受影响

    def test_hydrate_noop_on_text_topic(self) -> None:
        topic = self.make_topic("abandon", ["v. 放弃", "n. 好处"])
        reader = StubReader()
        self.assertEqual(hydrate_topic(topic, reader, logger=lambda _m: None), 0)
        self.assertEqual(reader.calls, 0)


class ImageReaderTest(unittest.TestCase):
    def test_custom_backend_and_cache(self) -> None:
        calls = []

        def fake(ref: ImageRef) -> str:
            calls.append(ref.kind)
            return "  OCR\n结果 "

        reader = ImageReader(backend="none", custom=fake, logger=lambda _m: None)
        first = reader.read(f"data:image/png;base64,{PNG_B64}")
        second = reader.read(f"data:image/png;base64,{PNG_B64}")
        self.assertEqual(first, "OCR 结果")
        self.assertEqual(second, first)
        self.assertEqual(len(calls), 1, "同一张图只该识别一次（命中缓存）")
        self.assertEqual(reader.stats, {"hit": 1, "miss": 1, "fail": 0})

    def test_disabled_backend_returns_none(self) -> None:
        reader = ImageReader(backend="none", logger=lambda _m: None)
        self.assertFalse(reader.enabled)
        self.assertIsNone(reader.read(f"data:image/png;base64,{PNG_B64}"))

    def test_captcha_is_sanitized(self) -> None:
        reader = ImageReader(backend="none", custom=lambda _ref: " a b3-C9! ", logger=lambda _m: None)
        self.assertEqual(reader.read_captcha(PNG_B64), "ab3C9")

    def test_config_loader_falls_back_to_llm_settings(self) -> None:
        reader = load_reader_from_config(
            {
                "vision": {"backend": "llm"},
                "llm": {"url": "https://api.example.com", "key": "k", "model": "m"},
            },
            logger=lambda _m: None,
        )
        self.assertEqual(reader.backend, "llm")
        self.assertEqual(reader.llm_url, "https://api.example.com")
        self.assertEqual(reader.llm_key, "k")

    def test_config_loader_defaults(self) -> None:
        reader = load_reader_from_config({}, logger=lambda _m: None)
        self.assertEqual(reader.backend, "auto")
        self.assertEqual(reader.tesseract_cmd, "", "留空表示自动查找 tesseract")

    def test_tesseract_resolution_is_lenient(self) -> None:
        """显式路径找不到时允许退回自动探测，且任何情况下都不该抛异常。"""
        self.assertIsInstance(resolve_tesseract(""), (str, type(None)))
        self.assertIsInstance(resolve_tesseract("definitely-not-a-real-binary-xyz"), (str, type(None)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
