# -*- coding: utf-8 -*-
"""配置 / 路径 / LLM 层的单元测试（不联网，全部用假响应验证）。"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vocabgo.llm import PROVIDER_PRESETS, LlmHelper, guess_models, preset_choices  # noqa: E402
from vocabgo.paths import app_dir, config_path, is_frozen  # noqa: E402
from vocabgo.settings import (  # noqa: E402
    DEFAULT_SETTINGS,
    RECOMMENDED_NOTES,
    apply_recommended,
    load_settings,
    missing_auth_fields,
    recommended_lines,
    resolve_llm,
    save_settings,
)


class FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self) -> dict:
        return self._payload


class SettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()) / "config.json"
        self.addCleanup(lambda: self.tmp.exists() and self.tmp.unlink())

    def test_defaults_when_file_missing(self) -> None:
        config = load_settings(self.tmp)
        self.assertEqual(config["llm"]["provider"], "deepseek")
        self.assertTrue(config["runtime"]["probe"])
        self.assertEqual(config["study"]["course_id"], "CET4_v2")

    def test_partial_file_deep_merges(self) -> None:
        self.tmp.write_text(json.dumps({"auth": {"usertoken": "tok"}, "runtime": {"probe": False}}), encoding="utf-8")
        config = load_settings(self.tmp)
        self.assertEqual(config["auth"]["usertoken"], "tok")
        self.assertFalse(config["runtime"]["probe"])
        # 没写的字段仍然有默认值
        self.assertEqual(config["runtime"]["max_score"], 10)
        self.assertEqual(config["auth"]["abc"], "")

    def test_broken_file_falls_back_to_defaults(self) -> None:
        self.tmp.write_text("{ this is not json", encoding="utf-8")
        config = load_settings(self.tmp)
        self.assertEqual(config["llm"]["provider"], "deepseek")

    def test_env_override_wins(self) -> None:
        self.tmp.write_text(json.dumps({"auth": {"usertoken": "from_file"}}), encoding="utf-8")
        with patch.dict("os.environ", {"VOCABGO_TOKEN": "from_env"}, clear=False):
            config = load_settings(self.tmp)
        self.assertEqual(config["auth"]["usertoken"], "from_env")

    def test_save_load_round_trip(self) -> None:
        config = load_settings(self.tmp)
        config["auth"]["usertoken"] = "abc123"
        config["llm"]["key"] = "sk-test"
        save_settings(config, self.tmp)
        again = load_settings(self.tmp)
        self.assertEqual(again["auth"]["usertoken"], "abc123")
        self.assertEqual(again["llm"]["key"], "sk-test")
        # 落盘内容不含内部键
        raw = json.loads(self.tmp.read_text(encoding="utf-8"))
        self.assertNotIn("_path", raw)

    def test_runtime_defaults_and_persistence(self) -> None:
        """界面上的轮数/任务类型/读图开关/上次任务都要能存住，下次打开直接带出来。"""
        config = load_settings(self.tmp)
        self.assertEqual(config["runtime"]["loop"], 3)
        self.assertEqual(config["runtime"]["kind"], "class")
        self.assertTrue(config["runtime"]["ocr"])
        self.assertEqual(config["runtime"]["last_task_id"], "")

        config["runtime"]["loop"] = 5
        config["runtime"]["kind"] = "study"
        config["runtime"]["ocr"] = False
        config["runtime"]["last_task_id"] = "95201087"
        save_settings(config, self.tmp)

        again = load_settings(self.tmp)
        self.assertEqual(again["runtime"]["loop"], 5)
        self.assertEqual(again["runtime"]["kind"], "study")
        self.assertFalse(again["runtime"]["ocr"])
        self.assertEqual(again["runtime"]["last_task_id"], "95201087")

    def test_missing_auth_fields(self) -> None:
        config = load_settings(self.tmp)
        self.assertEqual(missing_auth_fields(config), ["usertoken"])
        config["auth"]["usertoken"] = "x"
        self.assertEqual(missing_auth_fields(config), [])


class ResolveLlmTest(unittest.TestCase):
    def test_deepseek_preset_fills_url_and_models(self) -> None:
        resolved = resolve_llm({"llm": {"provider": "deepseek", "key": "sk-x"}})
        self.assertEqual(resolved["url"], "https://api.deepseek.com")
        self.assertEqual(resolved["model"], "deepseek-v4-flash")
        self.assertEqual(resolved["vision_model"], "deepseek-flash")
        self.assertEqual(resolved["thinking"], "off")
        self.assertEqual(resolved["key"], "sk-x")

    def test_explicit_values_beat_preset(self) -> None:
        resolved = resolve_llm(
            {
                "llm": {
                    "provider": "deepseek",
                    "key": "sk-x",
                    "model": "deepseek-v4-pro",
                    "vision_model": "deepseek-flash",
                    "thinking": "auto",
                    "url": "https://proxy.example.com/v1",
                }
            }
        )
        self.assertEqual(resolved["model"], "deepseek-v4-pro")
        self.assertEqual(resolved["thinking"], "auto")
        self.assertEqual(resolved["url"], "https://proxy.example.com/v1")

    def test_key_from_env_when_blank(self) -> None:
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-env"}, clear=False):
            resolved = resolve_llm({"llm": {"provider": "deepseek", "key": ""}})
        self.assertEqual(resolved["key"], "sk-env")

    def test_custom_provider_needs_all_fields(self) -> None:
        resolved = resolve_llm({"llm": {"provider": "custom", "key": "k"}})
        self.assertEqual(resolved["url"], "")
        self.assertEqual(resolved["model"], "")

    def test_presets_have_labels(self) -> None:
        choices = dict(preset_choices())
        for provider in PROVIDER_PRESETS:
            self.assertIn(provider, choices)
            self.assertTrue(choices[provider])

    def test_default_thinking_follows_preset(self) -> None:
        """默认配置里 thinking 留空，这样 DeepSeek 预设的 off 才生效（不然会给短答提取白烧 token）。"""
        with tempfile.TemporaryDirectory() as folder:
            config = load_settings(Path(folder) / "config.json")
        self.assertEqual(config["llm"]["thinking"], "")
        self.assertEqual(resolve_llm(config)["thinking"], "off")


class LlmHelperTest(unittest.TestCase):
    def test_endpoint_building(self) -> None:
        self.assertEqual(
            LlmHelper(url="https://api.deepseek.com", key="k", model="m").endpoint_chat(),
            "https://api.deepseek.com/v1/chat/completions",
        )
        self.assertEqual(
            LlmHelper(url="https://api.deepseek.com/v1", key="k", model="m").endpoint_chat(),
            "https://api.deepseek.com/v1/chat/completions",
        )
        self.assertEqual(
            LlmHelper(url="http://127.0.0.1:11434/", key="k", model="m").endpoint_models(),
            "http://127.0.0.1:11434/v1/models",
        )

    def test_enabled_requires_url_key_model(self) -> None:
        self.assertFalse(LlmHelper(url="", key="k", model="m").enabled)
        self.assertFalse(LlmHelper(url="https://x", key="", model="m").enabled)
        self.assertFalse(LlmHelper(url="https://x", key="k", model="").enabled)
        self.assertTrue(LlmHelper(url="https://x", key="k", model="m").enabled)

    def test_thinking_flag_added_only_when_configured(self) -> None:
        helper = LlmHelper(url="https://x", key="k", model="m", thinking="off")
        payload = helper._payload([{"role": "user", "content": "hi"}], "m", 8)  # noqa: SLF001
        self.assertEqual(payload["thinking"], {"type": "disabled"})

        auto = LlmHelper(url="https://x", key="k", model="m", thinking="auto")
        self.assertNotIn("thinking", auto._payload([], "m", 8))  # noqa: SLF001

    def test_thinking_param_is_dropped_on_400(self) -> None:
        """服务端不认 thinking 参数时要能自动退回，而不是整条兜底链挂掉。"""
        sent: list[dict] = []

        def fake_request(method, url, *, timeout=None, headers=None, **kwargs):
            sent.append(kwargs.get("json") or {})
            if len(sent) == 1:
                return FakeResponse(400, {"error": {"message": "Unknown parameter: thinking"}})
            return FakeResponse(200, {"choices": [{"message": {"content": "OK"}}]})

        helper = LlmHelper(url="https://x", key="k", model="m", thinking="off", logger=lambda _m: None)
        with patch("vocabgo.net.request", side_effect=fake_request):
            body, error = helper._post({"model": "m", "thinking": {"type": "disabled"}})  # noqa: SLF001

        self.assertEqual(error, "")
        self.assertIsNotNone(body)
        self.assertEqual(len(sent), 2)
        self.assertIn("thinking", sent[0])
        self.assertNotIn("thinking", sent[1])

    def test_other_400_is_not_retried(self) -> None:
        sent: list[dict] = []

        def fake_request(method, url, *, timeout=None, headers=None, **kwargs):
            sent.append(kwargs.get("json") or {})
            return FakeResponse(400, {"error": {"message": "Insufficient balance"}})

        helper = LlmHelper(url="https://x", key="k", model="m", logger=lambda _m: None)
        with patch("vocabgo.net.request", side_effect=fake_request):
            body, error = helper._post({"model": "m", "thinking": {"type": "disabled"}})  # noqa: SLF001

        self.assertIsNone(body)
        self.assertIn("Insufficient balance", error)
        self.assertEqual(len(sent), 1, "与 thinking 无关的错误不该重试")

    def test_ask_choice_parses_index(self) -> None:
        helper = LlmHelper(url="https://x", key="k", model="m", logger=lambda _m: None)
        topic = {
            "topic_mode": 31,
            "stem": {"content": "abandon"},
            "options": [{"content": "n. 好处"}, {"content": "v. 放弃"}, {"content": "adj. 关键的"}],
        }
        with patch.object(LlmHelper, "_chat", return_value="2"):
            self.assertEqual(helper.ask_choice(topic, {}), 2)
        with patch.object(LlmHelper, "_chat", return_value="选项 1"):  # 夹带文字也能抠出数字
            self.assertEqual(helper.ask_choice(topic, {}), 1)
        with patch.object(LlmHelper, "_chat", return_value="9"):  # 越界要拒绝
            self.assertIsNone(helper.ask_choice(topic, {}))

    def test_ask_phrase_normalizes(self) -> None:
        helper = LlmHelper(url="https://x", key="k", model="m", logger=lambda _m: None)
        topic = {"topic_mode": 32, "stem": {"content": "___ ___ instances", "remark": "许多例子"}, "options": []}
        with patch.object(LlmHelper, "_chat", return_value="in many instances"):
            self.assertEqual(helper.ask_phrase(topic, {}), "in,many,instances")


class PathsTest(unittest.TestCase):
    def test_source_mode_paths(self) -> None:
        self.assertFalse(is_frozen())
        self.assertEqual(app_dir().name, "vocabgo-auto")
        self.assertTrue(str(config_path()).endswith("config.json"))

    def test_override_path_is_honoured(self) -> None:
        override = Path(tempfile.gettempdir()) / "my_config.json"
        self.assertEqual(config_path(override), override)


class GuessModelsTest(unittest.TestCase):
    """从 /models 返回值里自动认出「答题模型」和「读图模型」。"""

    def test_deepseek_picks_preset_names(self) -> None:
        ids = ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-flash"]
        text, vision = guess_models(ids, "deepseek")
        self.assertEqual(text, "deepseek-v4-flash")
        self.assertEqual(vision, "deepseek-flash")

    def test_vision_by_name_hint(self) -> None:
        ids = ["qwen2.5:7b", "qwen2.5vl:7b", "llama3:8b"]
        text, vision = guess_models(ids, "ollama")
        self.assertEqual(vision, "qwen2.5vl:7b")
        self.assertEqual(text, "qwen2.5:7b")

    def test_single_model_serves_both(self) -> None:
        text, vision = guess_models(["gpt-4o-mini"], "openai")
        self.assertEqual(text, "gpt-4o-mini")
        self.assertEqual(vision, "gpt-4o-mini")

    def test_unknown_provider_falls_back_to_first(self) -> None:
        text, vision = guess_models(["model-a", "model-b"], "custom")
        self.assertEqual(text, "model-a")
        self.assertEqual(vision, text, "没有视觉特征时就跟随答题模型")

    def test_empty_list(self) -> None:
        self.assertEqual(guess_models([], "deepseek"), ("", ""))


class RecommendedConfigTest(unittest.TestCase):
    """推荐配置：既要在程序里生效，也要和说明书写的一致（别各说各话）。"""

    # 说明书的表格里每一条「项目」对应配置里的哪个字段
    NOTE_PATHS = {
        "服务商": ("llm", "provider"),
        "答题模型": ("llm", "model"),
        "读图模型": ("llm", "vision_model"),
        "思考模式": ("llm", "thinking"),
        "作答间隔": ("runtime", "pace"),
        "试答案兜底": ("runtime", "probe"),
        "读图": ("runtime", "ocr"),
        "练习轮数": ("runtime", "loop"),
        "满分阈值": ("runtime", "max_score"),
    }

    def test_notes_cover_every_recommended_field(self) -> None:
        note_names = [name for name, _, _ in RECOMMENDED_NOTES]
        self.assertEqual(sorted(note_names), sorted(self.NOTE_PATHS), "说明书表格与推荐配置漂移了")
        for name, value, why in RECOMMENDED_NOTES:
            self.assertTrue(value and why, f"{name} 缺少说明")

    def test_recommended_values_match_notes(self) -> None:
        config = apply_recommended({})
        for name, _, _ in RECOMMENDED_NOTES:
            section, key = self.NOTE_PATHS[name]
            self.assertIn(key, config[section], f"{name} 在推荐配置里没有对应项")

        self.assertEqual(config["llm"]["provider"], "deepseek")
        self.assertEqual(config["llm"]["thinking"], "off", "短答任务不该默认开思考")
        self.assertEqual(config["runtime"]["pace"], [1.5, 3.5], "推荐节奏要比默认更保守")
        self.assertTrue(config["runtime"]["probe"])
        self.assertTrue(config["runtime"]["ocr"])
        self.assertEqual(config["vision"]["backend"], "auto")

    def test_apply_recommended_keeps_credentials(self) -> None:
        existing = {
            "auth": {"usertoken": "my-token", "abc": "a", "authorization_v": "v"},
            "llm": {"key": "sk-my-key"},
        }
        merged = apply_recommended(existing)
        self.assertEqual(merged["auth"]["usertoken"], "my-token", "推荐配置不能动用户令牌")
        self.assertEqual(merged["llm"]["key"], "sk-my-key", "推荐配置不能覆盖用户密钥")
        self.assertEqual(merged["llm"]["model"], "deepseek-v4-flash")

    def test_recommended_survives_save_load(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            save_settings(apply_recommended({"llm": {"key": "sk-1"}}), path)
            again = load_settings(path)
        self.assertEqual(again["runtime"]["loop"], 3)
        self.assertEqual(again["llm"]["key"], "sk-1")

    def test_recommended_lines_are_readable(self) -> None:
        lines = recommended_lines()
        self.assertEqual(len(lines), len(RECOMMENDED_NOTES))
        self.assertTrue(all(line.startswith("　") for line in lines))


class GuiPlaceholderTest(unittest.TestCase):
    """灰色占位提示不能污染配置 —— 否则「留空即自动推算」会被当成真值发进请求头。"""

    def setUp(self) -> None:
        try:
            import tkinter
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"环境不支持 Tk：{exc}")
        import vocabgo.gui as gui  # noqa: PLC0415

        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        old_env = os.environ.get("VOCABGO_CONFIG")
        os.environ["VOCABGO_CONFIG"] = str(Path(self.folder) / "config.json")
        self.addCleanup(lambda: os.environ.__setitem__("VOCABGO_CONFIG", old_env) if old_env else os.environ.pop("VOCABGO_CONFIG", None))

        try:
            self.root = tkinter.Tk()
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"Tk 初始化失败：{exc}")
        self.addCleanup(self.root.destroy)
        self.root.geometry("+4000+4000")  # 挪到屏幕外，别打扰
        self.app = gui.VocabgoApp(self.root)
        self.root.update()

    def test_hint_shows_when_empty_and_does_not_touch_variable(self) -> None:
        self.root.update()
        self.assertEqual(self.app.var_abc.get(), "", "占位提示绝不能写进变量")
        self.assertEqual(self.app.var_auth_v.get(), "")
        self.assertTrue(self.app.hint_abc.winfo_ismapped(), "空值时应显示灰色提示")
        self.assertTrue(self.app.hint_auth_v.winfo_ismapped())

    def test_hint_hides_once_user_types(self) -> None:
        self.app.var_abc.set("real-captured-abc")
        self.app.hint_abc._refresh()  # type: ignore[attr-defined]  # noqa: SLF001
        self.root.update()
        self.assertFalse(self.app.hint_abc.winfo_ismapped())
        self.assertEqual(self.app.var_abc.get(), "real-captured-abc")

        self.root.focus_set()  # 焦点挪出输入框（聚焦时提示本就该隐藏）
        self.app.var_abc.set("")
        self.app.hint_abc._refresh()  # type: ignore[attr-defined]  # noqa: SLF001
        self.root.update()
        self.assertTrue(self.app.hint_abc.winfo_ismapped(), "清空且失焦后提示应回来")

    def test_collected_config_has_no_placeholder(self) -> None:
        config = self.app.collect_config()
        self.assertEqual(config["auth"]["abc"], "")
        self.assertEqual(config["auth"]["authorization_v"], "")
        self.assertNotIn("留空即自动推算", json.dumps(config, ensure_ascii=False), "提示文字泄漏进配置了")


    def test_qr_popup_shows_image_in_window(self) -> None:
        """点「扫码授权」时二维码要直接弹在界面上，而不是丢个文件让用户去找。"""
        import vocabgo.qrlogin as qrlogin  # noqa: PLC0415

        data = qrlogin.render_qr_png("https://open.weixin.qq.com/connect/oauth2/authorize?appid=x")
        if data is None:
            self.skipTest("本机没装 qrcode 库")

        self.app._show_qr("https://open.weixin.qq.com/x", data)  # noqa: SLF001
        self.root.update()
        self.assertIsNotNone(self.app.qr_window, "应该弹出二维码窗口")
        self.assertTrue(self.app.qr_window.winfo_exists())
        self.app._close_qr_window()
        self.root.update()
        self.assertIsNone(self.app.qr_window)

    def test_qr_popup_degrades_without_qrcode(self) -> None:
        """没有二维码库时：不崩、把链接复制到剪贴板、日志给出提示。"""
        with patch("vocabgo.gui.messagebox.showinfo") as shown:
            self.app._show_qr("https://open.weixin.qq.com/fallback", None)  # noqa: SLF001
        self.root.update()
        self.assertIsNone(self.app.qr_window)
        self.assertTrue(shown.called, "应弹提示告诉用户用链接")


class GuiImportTest(unittest.TestCase):
    def test_gui_module_imports(self) -> None:
        """GUI 至少要在无显示器/打包环境下也能 import（Tk 只在 main 里创建）。"""
        import vocabgo.gui as gui  # noqa: PLC0415

        self.assertTrue(hasattr(gui, "VocabgoApp"))
        self.assertTrue(callable(gui.main))

    def test_app_entry_selftest_runs(self) -> None:
        """打包入口的自检在源码模式下也要能跑通（打包后同样用它验收）。"""
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import vocabgo_app  # noqa: PLC0415

        self.assertEqual(vocabgo_app.selftest(), 0, "自检应全部通过")


if __name__ == "__main__":
    unittest.main(verbosity=2)
