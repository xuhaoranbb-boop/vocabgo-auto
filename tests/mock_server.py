# -*- coding: utf-8 -*-
"""本地假 vocabgo 服务：不碰真实站点就能把整条链路跑通。

它复刻了真实服务端的关键行为：
  * body 必须带正确 sign（用 salt 校验），GET 必须带 timestamp/version/app_type
  * 响应 data 用 jv 混淆（1 / 2_xxx / 3_xxx 都会出现，随机轮换）
  * VerifyAnswer 第一次答错不回吐答案，第二次答错才给出 answer_corrects
  * SubmitAnswerAndSave 记录本题最终作答，然后返回下一题

可以直接单独跑，当作可玩的演练环境::

    python tests/mock_server.py --port 8877
    # 另开一个窗口
    $env:VOCABGO_BASE_URL="http://127.0.0.1:8877"; python -m vocabgo run --kind class --index 0 --fast
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vocabgo.crypto import JV_RULES, SALT, canonical_params, encode_jv, md5  # noqa: E402

# ── 题目素材 ──
WORD_DEFS = {
    "abandon": ["v. 放弃；抛弃", "n. 放纵；无拘束"],
    "benefit": ["n. 好处；益处", "v. 有益于"],
    "crucial": ["adj. 决定性的；关键的"],
    "diligent": ["adj. 勤奋的；用功的"],
    "eliminate": ["v. 消除；剔除；淘汰"],
}


def _opt(content: str, tag: int) -> dict[str, Any]:
    return {"content": content, "answer_tag": tag, "option_id": tag + 1}


def _choice_topic(index: int, word: str, distractors: list[str], total: int, pos: int = 0) -> dict[str, Any]:
    correct = WORD_DEFS[word][0]
    contents = list(distractors)[:3]
    contents.insert(min(pos, len(contents)), correct)  # 正确答案位置随机，防止「盲选第一项」蒙对
    options = [_opt(text, i) for i, text in enumerate(contents)]
    return {
        "topic_code": f"T{index}",
        "topic_mode": 31,
        "stem": {"content": word, "remark": ""},
        "options": options,
        "topic_done_num": index,
        "topic_total": total,
        "__answer": contents.index(correct),
    }


def _memory_topic(index: int, word: str, total: int) -> dict[str, Any]:
    return {
        "topic_code": f"T{index}",
        "topic_mode": 0,
        "stem": {"content": word, "remark": ""},
        "options": [_opt(text, i) for i, text in enumerate(WORD_DEFS[word])],
        "topic_done_num": index,
        "topic_total": total,
        "__answer": None,
    }


def _sentence_topic(index: int, word: str, distractors: list[str], total: int, pos: int = 0) -> dict[str, Any]:
    correct = WORD_DEFS[word][0]
    contents = list(distractors)[:2]
    contents.insert(min(pos, len(contents)), correct)
    options = [_opt(text, i) for i, text in enumerate(contents)]
    return {
        "topic_code": f"T{index}",
        "topic_mode": 11,
        "stem": {"content": f"He finally decided to {{{word}}} the whole plan.", "remark": "他最后决定放弃整个计划。"},
        "options": options,
        "topic_done_num": index,
        "topic_total": total,
        "__answer": contents.index(correct),
    }


def _phrase_topic(index: int, total: int) -> dict[str, Any]:
    return {
        "topic_code": f"T{index}",
        "topic_mode": 32,
        "stem": {"content": "___ ___ instances", "remark": "许多例子"},
        "options": [_opt(text, i) for i, text in enumerate(["in", "many", "of", "the"])],
        "topic_done_num": index,
        "topic_total": total,
        "__answer": "in,many,instances",
    }


FAKE_TOPIC_IMAGE = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def _topic_image_data_uri(word: str) -> str:
    """题干图片：装了 Pillow 就真把单词画出来（这样演练环境能真跑 OCR），否则退回 1x1 占位图。"""
    try:
        import base64
        import shutil
        import tempfile

        from vocabgo.vision import render_probe_image

        workdir = Path(tempfile.mkdtemp())
        try:
            image = render_probe_image(word, workdir / "stem.png")
            if image is not None:
                return "data:image/png;base64," + base64.b64encode(image.read_bytes()).decode("ascii")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    except Exception:
        pass
    return FAKE_TOPIC_IMAGE


def _image_choice_topic(index: int, word: str, distractors: list[str], total: int, pos: int = 0) -> dict[str, Any]:
    """题干以图片下发的选择题：模拟「服务端只发图不发字」的防爬升级场景。"""
    topic = _choice_topic(index, word, distractors, total, pos=pos)
    topic["stem"]["content"] = _topic_image_data_uri(word)
    return topic


def _collocation_topic(index: int, total: int) -> dict[str, Any]:    return {
        "topic_code": f"T{index}",
        "topic_mode": 31,
        "stem": {
            "content": "in ___ ___",
            "remark": [
                {"relation": "many", "word": "many"},
                {"relation": "instances", "word": "instances"},
            ],
        },
        "options": [_opt(text, i) for i, text in enumerate(["many", "instances", "of", "the"])],
        "answer_num": 2,
        "topic_done_num": index,
        "topic_total": total,
        "__answer": [0, 1],
    }


def build_topics() -> list[dict[str, Any]]:
    """13 道题，覆盖 mode=0/11/31/32 + 搭配题 + 图片题干 + 重复题（验证题库缓存）。"""
    total = 13
    return [
        _memory_topic(0, "abandon", total),
        _memory_topic(1, "benefit", total),
        _memory_topic(2, "crucial", total),
        _memory_topic(3, "diligent", total),
        _choice_topic(4, "abandon", ["n. 好处；益处", "adj. 勤奋的", "v. 消除"], total, pos=2),
        _sentence_topic(5, "abandon", ["n. 好处；益处", "adj. 勤奋的"], total, pos=1),
        _choice_topic(6, "diligent", ["v. 放弃", "n. 好处", "v. 消除"], total, pos=3),
        _choice_topic(7, "eliminate", ["n. 好处", "adj. 关键的", "adj. 勤奋的"], total, pos=1),
        _phrase_topic(8, total),
        _collocation_topic(9, total),
        _choice_topic(10, "benefit", ["adj. 关键的", "v. 消除", "adj. 勤奋的"], total, pos=0),
        # 与第 7 题题干/选项完全一致 → 题目指纹相同，用来验证题库命中
        _choice_topic(11, "eliminate", ["n. 好处", "adj. 关键的", "adj. 勤奋的"], total, pos=1),
        # 题干是图片：没有读图层就只能靠试答案，有读图层则能走规则匹配
        _image_choice_topic(12, "abandon", ["n. 好处；益处", "adj. 勤奋的", "v. 消除"], total, pos=1),
    ]


class MockState:
    """服务端状态：判题、记录、统计。"""

    def __init__(self) -> None:
        self.topics = build_topics()
        self.index = 0
        self.wrong_count: dict[str, int] = {}
        self.collected: dict[str, list[Any]] = {}
        self.last_correct: dict[str, bool] = {}
        self.saved: dict[str, Any] = {}
        self.saved_wrong: list[str] = []
        self.sign_fail = 0
        self.sign_ok = 0
        self.jv_used: list[str] = []
        self.verify_calls = 0
        self.words_chosen: list[str] = []
        self.signin = 0

    # ── 题目 ──
    def topic_by_code(self, code: str) -> dict[str, Any]:
        for topic in self.topics:
            if topic["topic_code"] == code:
                return topic
        raise KeyError(code)

    def next_topic(self) -> dict[str, Any] | None:
        return self.topics[self.index] if self.index < len(self.topics) else None

    def advance(self) -> None:
        self.index = min(self.index + 1, len(self.topics))

    # ── 判题 ──
    def judge(self, code: str, answer: Any) -> dict[str, Any]:
        topic = self.topic_by_code(code)
        expected = topic["__answer"]
        self.verify_calls += 1

        if isinstance(expected, list):
            ok = self._tag_of(topic, answer) in expected
            if ok:
                bag = self.collected.setdefault(code, [])
                if self._tag_of(topic, answer) not in bag:
                    bag.append(self._tag_of(topic, answer))
            done = sorted(bag) == sorted(expected) if (bag := self.collected.get(code)) else False
            return {
                "answer_result": 1 if ok else 0,
                "over_status": 1 if done else 0,
                "answer_corrects": [] if (ok or self.wrong_count.get(code, 0) < 2) else list(expected),
                "topic_code": code,
            }

        ok = self._is_correct(topic, answer)

        if not ok:
            self.wrong_count[code] = self.wrong_count.get(code, 0) + 1
            # 真实服务端行为：第一次错不给答案，第二次错才回吐 answer_corrects
            corrects = [expected] if self.wrong_count[code] >= 2 else []
            return {"answer_result": 0, "over_status": 0, "answer_corrects": corrects, "topic_code": code}

        self.last_correct[code] = True
        return {"answer_result": 1, "over_status": 1, "answer_corrects": [], "topic_code": code}

    @staticmethod
    def _tag_of(topic: dict[str, Any], answer: Any) -> int | None:
        if isinstance(answer, int):
            return answer
        text = str(answer).strip()
        if text.isdigit():
            return int(text)
        for opt in topic["options"]:
            if str(opt["content"]).strip().lower() == text.lower():
                return int(opt["answer_tag"])
        return None

    def _is_correct(self, topic: dict[str, Any], answer: Any) -> bool:
        expected = topic["__answer"]
        if expected is None:
            return True
        if isinstance(expected, str):
            normalized = ",".join(t for t in str(answer).replace(",", " ").split() if t)
            return normalized.lower() == expected.lower()
        return self._tag_of(topic, answer) == expected

    def save(self, code: str) -> None:
        topic = self.topic_by_code(code)
        expected = topic["__answer"]
        if isinstance(expected, list):
            bag = sorted(self.collected.get(code, []))
            ok = bag == sorted(expected)
        elif expected is None:
            ok = True
        else:
            ok = self.last_correct.get(code, False)
        self.saved[code] = {"ok": ok, "mode": topic["topic_mode"]}
        if not ok:
            self.saved_wrong.append(code)
        self.advance()


class MockHandler(BaseHTTPRequestHandler):
    state: MockState

    def log_message(self, *args: Any) -> None:  # 静音默认访问日志
        pass

    # ── 工具 ──
    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return {}

    def _jv(self, payload: dict[str, Any]) -> str:
        """挑一个长度够用的混淆方案，够用就随机换，覆盖各条解码分支。"""
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        length = len(text) * 4 // 3 + 4
        candidates = ["1", "2_1254", "2_9314", "3_1021", "3_2265", "3_2277"]
        if length > 380:
            candidates += ["2_9214", "2_10234"]
        usable = [jv for jv in candidates if jv == "1" or self._max_index(jv) < length - 8]
        jv = (usable or ["1"])[self.state.verify_calls % len(usable or ["1"])]
        self.state.jv_used.append(jv)
        return jv

    @staticmethod
    def _max_index(jv: str) -> int:
        rule = JV_RULES[jv]
        rules = rule if isinstance(rule, list) else rule["uc"]
        return max(r["s"] + r["n"] for r in rules)

    def _send(self, payload: Any, *, obfuscate: bool = True, code: int = 200) -> None:
        if obfuscate:
            jv = self._jv(payload)
            body = {"code": 1, "jv": jv, "data": encode_jv(payload, jv)}
        else:
            body = {"code": 1, "jv": "0", "data": payload}
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _check_sign(self, body: dict[str, Any]) -> bool:
        signature = body.get("sign")
        if not signature:
            return False
        payload = {k: v for k, v in body.items() if k not in ("sign", "app_type")}
        expected = md5(canonical_params(payload) + SALT)
        if expected == signature:
            self.state.sign_ok += 1
            return True
        self.state.sign_fail += 1
        return False

    # ── 路由 ──
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        state = self.state

        if path.endswith("/ClassTask/StartAnswer"):
            topic = state.next_topic()
            if topic is None:
                self._send({"topic_done_num": len(state.topics), "topic_total": len(state.topics)})
                return
            self._send(dict(topic, topic_done_num=state.index, topic_total=len(state.topics)))
            return

        if path.endswith("/ClassTask/PageTask"):
            self._send({"records": [{"task_id": 1001, "release_id": 2001, "task_name": "模拟班级任务", "progress": 0, "score": 0}]})
            return

        if path.endswith("/ClassTask/Info"):
            self._send({"task_name": "模拟班级任务", "word_num": len(WORD_DEFS)})
            return

        if path.endswith("/ClassTask/ChoseWordList"):
            self._send({"word_list": [{"word": w, "score": 0, "course_id": "CET4_v2", "list_id": 9} for w in WORD_DEFS]})
            return

        if path == "/__inspect":
            self._send(
                {
                    "saved": state.saved,
                    "saved_wrong": state.saved_wrong,
                    "sign_ok": state.sign_ok,
                    "sign_fail": state.sign_fail,
                    "jv_used": sorted(set(state.jv_used)),
                    "verify_calls": state.verify_calls,
                    "wrong_count": state.wrong_count,
                    "words_chosen": state.words_chosen,
                    "signin": state.signin,
                },
                obfuscate=False,
            )
            return

        self._send({"msg": "not found"}, obfuscate=False, code=404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._read_json()
        state = self.state

        if path == "/__reset":
            state.__init__()
            self._send({"ok": True}, obfuscate=False)
            return

        if not self._check_sign(body):
            self._send({"msg": "bad sign"}, obfuscate=False, code=403)
            return

        if path.endswith("/ClassTask/PageTask"):
            self._send({"records": [{"task_id": 1001, "release_id": 2001, "task_name": "模拟班级任务", "progress": 0, "score": 0}]})
            return

        if path.endswith("/ClassTask/VerifyAnswer"):
            self._send(state.judge(str(body.get("topic_code")), body.get("answer")))
            return

        if path.endswith("/ClassTask/SubmitAnswerAndSave"):
            state.save(str(body.get("topic_code")))
            topic = state.next_topic()
            if topic is None:
                self._send({"topic_done_num": len(state.topics), "topic_total": len(state.topics), "integral": 10})
                return
            self._send(dict(topic, topic_done_num=state.index, topic_total=len(state.topics)))
            return

        if path.endswith("/ClassTask/SubmitChoseWord"):
            for words in (body.get("word_map") or {}).values():
                state.words_chosen.extend(words)
            self._send({"ok": True})
            return

        if path.endswith("/TaskStudentSignin/Do"):
            state.signin += 1
            self._send({"sign_in_total": 7, "integral": 5})
            return

        self._send({"msg": "not found"}, obfuscate=False, code=404)


def serve(port: int = 0) -> tuple[ThreadingHTTPServer, str, MockState]:
    """起一个后台 mock 服务，返回 (server, base_url, state)。"""
    state = MockState()
    handler = type("BoundMockHandler", (MockHandler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", state


def main() -> int:
    parser = argparse.ArgumentParser(description="本地 mock vocabgo 服务")
    parser.add_argument("--port", type=int, default=8877)
    args = parser.parse_args()
    server, url, _ = serve(args.port)
    print(f"🧪 mock vocabgo 已启动：{url}")
    print(f'   PowerShell: $env:VOCABGO_BASE_URL="{url}"; python -m vocabgo run --kind class --index 0 --fast')
    print("   Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
