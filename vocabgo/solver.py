# -*- coding: utf-8 -*-
"""答题引擎：把一道题变成一次「判对 + 保存」。

策略链（先干净、后兜底，越靠前越像人）：

1. **cache**  本地题库命中（历史答过，零风险）
2. **match**  规则匹配：mode=0 学到的词义 / 搭配题 remark 里直接给的 relation
3. **llm**    大模型兜底（需自行配置 OpenAI 兼容接口）
4. **probe**  试答案：提交错误答案逼服务端回吐 answer_corrects，再用正确答案重判
              —— 注意这招会故意答错，而词达人的词分按作答正确率累计，所以放在模型之后

策略链顺序（见 `_solve_one`）：题库 → 规则匹配 → 大模型 → 试答案 → 盲选。
把「试答案」放在后面是**故意**的：实测错两次对一次的词只能拿 3.7 分，
错一次对一次拿 5.0 分，全程不错能到 7.5+，所以能一次答对就别故意答错。
5. **guess**  最后盲选（只保证流程不卡死）

其中 3 是关键：VerifyAnswer 连续两次收到错误答案后，响应体会带上本题的
answer_corrects，于是可以「用正确答案再判一次 → answer_result=1 → 提交」，
最终落库的是正确作答，因此仍然能拿高分。
"""

from __future__ import annotations

import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .bank import Bank
from .client import VocabgoClient, join_phrase
from .llm import LlmHelper
from .vision import ImageReader, hydrate_topic, topic_image_fields
from .matcher import (
    absorb_word_defs,
    display_answer,
    display_stem,
    is_collocation,
    match_choice,
    match_collocation,
    topic_key,
    topic_key_loose,
    topic_mode,
    topic_options,
)

WRONG_SENTINEL = ""  # 试答案用的「明显错误」作答

# 日志里标注「这题靠什么答出来的」——界面上全部用中文
SOURCE_LABELS = {
    "cache": "题库",
    "match": "规则",
    "probe": "试答案",
    "llm": "大模型",
    "guess": "猜测",
    "ocr": "读图",
    "fixed": "服务端纠错",
}
SOURCE_CN = {key: f"[{value}]" for key, value in SOURCE_LABELS.items()}


def source_label(source: str) -> str:
    return SOURCE_CN.get(source, f"[{source or '未知'}]")


@dataclass
class Stats:
    """一次任务的答题统计，用来判断准确率和兜底链路是否健康。"""

    total: int = 0
    done: int = 0
    cache: int = 0
    match: int = 0
    probe: int = 0
    llm: int = 0
    guess: int = 0
    ocr: int = 0
    fixed: int = 0
    wrong_saved: int = 0
    errors: int = 0
    cancelled: bool = False
    sources: dict[str, int] = field(default_factory=dict)

    def hit(self, source: str) -> None:
        self.sources[source] = self.sources.get(source, 0) + 1

    def report(self) -> str:
        parts = [
            f"进度 {self.done}/{self.total}",
            f"题库 {self.cache}",
            f"规则 {self.match}",
            f"试答案 {self.probe}",
            f"LLM {self.llm}",
            f"猜测 {self.guess}",
            f"服务端纠错 {self.fixed}",
        ]
        if self.ocr:
            parts.insert(-1, f"读图 {self.ocr}")
        if self.errors:
            parts.append(f"异常 {self.errors}")
        return " | ".join(parts)


class Solver:
    def __init__(
        self,
        client: VocabgoClient,
        bank: Bank,
        *,
        kind: str = "class",
        llm: LlmHelper | None = None,
        reader: ImageReader | None = None,
        probe: bool = True,
        probe_attempts: int = 2,
        pace: tuple[float, float] = (1.0, 2.6),
        dry_run: bool = False,
        cancel: "threading.Event | None" = None,
        logger: Callable[[str], None] = print,
    ) -> None:
        self.client = client
        self.bank = bank
        self.kind = kind  # "class" = 班级任务, "study" = 自学任务
        self.llm = llm
        self.reader = reader
        self.probe_enabled = probe
        self.probe_attempts = probe_attempts
        self.pace = pace
        self.dry_run = dry_run
        self.cancel = cancel  # GUI 的「停止」按钮：置位后本题收尾即退出
        self.log = logger
        self.word_defs: dict[str, list[str]] = {}
        self.stats = Stats()

    # ── 对外入口 ──
    def run(
        self,
        task_id: Any = None,
        release_id: Any = None,
        *,
        course_id: str | None = None,
        list_id: Any = None,
        task_type: int = 3,
        grade: int = 2,
    ) -> Stats:
        resp = self._start(task_id, release_id, course_id, list_id, task_type, grade)
        topic = VocabgoClient.pick_topic(resp)
        if not topic:
            raise RuntimeError(f"StartAnswer 未返回题目: {resp.get('code')} {resp.get('msg') or ''}")

        data = resp.get("data") or {}
        self.stats.total = int(data.get("topic_total") or 0)
        self.stats.done = int(data.get("topic_done_num") or 0)
        self.log(f"🚀 开始作答：{self.stats.done}/{self.stats.total}，题库 {len(self.bank)} 条")

        guard = 0
        while topic:
            if self.cancel is not None and self.cancel.is_set():
                self.stats.cancelled = True
                self.log(f"⏹️ 手动停止　{self.stats.report()}")
                self.bank.flush()
                return self.stats

            topic = self._hydrate(topic)  # 题干/选项若是图片，先读图再进策略链
            code = str(topic.get("topic_code") or "")
            mode = topic_mode(topic)
            self.stats.done = int(topic.get("topic_done_num") or self.stats.done)
            self.stats.total = int(topic.get("topic_total") or self.stats.total)

            try:
                if mode == 0:
                    absorb_word_defs(topic, self.word_defs)
                    self.log(f"  [{self.stats.done}/{self.stats.total}] 📖 {display_stem(topic)}")
                    save = self._submit(code, mode)
                else:
                    save = self._solve_one(topic)
            except Exception as exc:
                text = str(exc)
                if "20001" in text or "需要选词" in text:
                    # 这不是故障：本轮选中的词都答完了，服务端要求重新选词。
                    # 属于正常的会话结束，别报成「异常」吓人。
                    self.log(f"✅ 本轮题目已答完（{self.stats.done}/{self.stats.total}）　{self.stats.report()}")
                    self.bank.flush()
                    return self.stats
                self.stats.errors += 1
                self.log(f"  ⚠️ 本题异常，跳过：{type(exc).__name__}: {exc}")
                self.bank.flush()
                return self.stats

            next_topic = VocabgoClient.pick_topic(save)
            if not next_topic or str(next_topic.get("topic_code")) == code:
                finish = save.get("data") or {}
                if isinstance(finish, dict) and finish.get("topic_done_num"):
                    self.stats.done = int(finish["topic_done_num"])
                    self.stats.total = int(finish.get("topic_total") or self.stats.total)
                self.log(f"🎉 结束：{self.stats.done}/{self.stats.total}　{self.stats.report()}")
                self.bank.flush()
                return self.stats

            topic = next_topic
            guard += 1
            if guard > 5000:
                self.log("⚠️ 循环次数过多，主动退出以防死循环")
                self.bank.flush()
                return self.stats
            if mode != 0:
                self._sleep()

        self.bank.flush()
        return self.stats

    # ── 单题处理 ──
    def _hydrate(self, topic: dict[str, Any]) -> dict[str, Any]:
        """读图：题干/选项以图片下发时，OCR 成文本，后续匹配逻辑不用改。"""
        if self.reader is None or not topic_image_fields(topic):
            return topic
        try:
            replaced = hydrate_topic(topic, self.reader, logger=self.log)
            self.stats.ocr += replaced
        except Exception as exc:
            self.log(f"  ⚠️ 读图失败，按原样继续：{type(exc).__name__}: {exc}")
        return topic

    def _solve_one(self, topic: dict[str, Any]) -> dict[str, Any]:
        code = str(topic.get("topic_code") or "")
        mode = topic_mode(topic)
        stem = display_stem(topic)

        if is_collocation(topic):
            return self._solve_multi(topic)

        key = topic_key(topic)

        # 1) 题库
        cached = self.bank.get(key)
        if cached is None:
            cached = self._loose_phrase_answer(topic, mode)
        if cached is not None:
            code, ok, _ = self._answer(topic, code, cached)
            if ok:
                self.stats.cache += 1
                self.log(f"  ✅ {stem} → {display_answer(topic, cached)} {source_label('cache')}")
                return self._submit(code, mode)

        # 2) 规则匹配
        matched = match_choice(topic, self.word_defs)
        if matched is not None:
            code, ok, _ = self._answer(topic, code, matched)
            if ok:
                self.stats.match += 1
                self.bank.put(key, matched, display_stem(topic))
                self.log(f"  ✅ {stem} → {display_answer(topic, matched)} {source_label('match')}")
                return self._submit(code, mode)

        # 3) 大模型
        # 注意顺序：**先问模型，再考虑试答案**。
        # 因为词达人的词分是按作答正确率累计的（实测：错两次对一次 ≈ 3.7 分，
        # 错一次对一次 ≈ 5.0 分，全程不错 ≈ 7.5 分），而试答案会故意答错，
        # 每用一次就把这个词的记录拉低一截。所以只在模型也没辙时才动用它。
        if self.llm and self.llm.enabled:
            guess = self.llm.ask_phrase(topic, self.word_defs) if mode == 32 else self.llm.ask_choice(topic, self.word_defs)
            if guess is not None:
                code, ok, _ = self._answer(topic, code, guess)
                if ok:
                    self.stats.llm += 1
                    self.bank.put(key, guess, display_stem(topic))
                    self.log(f"  🤖 {stem} → {display_answer(topic, guess)} {source_label('llm')}")
                    return self._submit(code, mode)

        # 4) 试答案（兜底：故意答错换服务端的正确答案，会拉低该词正确率，谨慎使用）
        if self.probe_enabled:
            code, probed = self._probe(topic, code)
            if probed is not None:
                code, ok, data = self._answer(topic, code, probed)
                if ok:
                    self.stats.probe += 1
                    self.bank.put(key, probed, display_stem(topic))
                    self.log(f"  🔎 {stem} → {display_answer(topic, probed)} {source_label('probe')}")
                    return self._submit(code, mode)
                # 极端情况：服务端自己回吐的答案判不过，直接保存现状
                self.stats.wrong_saved += 1
                self.log(f"  ⚠️ {stem} → 试答案纠正失败，按现状提交")
                return self._submit(str(data.get("topic_code") or code), mode)

        # 5) 盲选
        fallback: Any = self._default_answer(topic)
        code, ok, _ = self._answer(topic, code, fallback)
        self.stats.guess += 1
        if not ok:
            self.stats.wrong_saved += 1
        self.log(f"  ❓ {stem} → {display_answer(topic, fallback)} {source_label('guess')}")
        return self._submit(code, mode)

    def _loose_phrase_answer(self, topic: dict[str, Any], mode: int) -> str | None:
        """选项变了导致精确键 miss 时，找回同一个题干的旧答案。

        只认「内容型」答案（组词题给的是词本身），并且要求每个词都能在当前选项池里找到 ——
        选择题的答案是选项下标，选项一变下标就没意义了，所以绝不复用，宁可重新推。
        复用内容型答案能省下一次「故意答错」，直接提高正确率记录。
        """
        if mode != 32:
            return None
        pool = {str(opt.get("content") or "").strip().lower() for opt in topic_options(topic)}
        pool.discard("")
        if not pool:
            return None
        for _full_key, answer in self.bank.find_loose(topic_key_loose(topic)):
            if not isinstance(answer, str) or not answer.strip():
                continue
            # 组词题的答案两种存法都见过：`a mathematical formula`（空格）和 `in,many,instances`（逗号）
            tokens = [tok for tok in re.split(r"[\s,，]+", answer.strip()) if tok]
            if tokens and all(tok.lower() in pool for tok in tokens):
                return answer
        return None

    def _solve_multi(self, topic: dict[str, Any]) -> dict[str, Any]:
        """搭配题：多选，服务端用 over_status 标记整题判完。"""
        code = str(topic.get("topic_code") or "")
        key = topic_key(topic)
        stem = display_stem(topic)
        answer_num = int(topic.get("answer_num") or 2)
        options = topic_options(topic)

        tags: list[Any] | None = None
        source = ""
        cached = self.bank.get(key)
        if isinstance(cached, list) and cached:
            tags, source = list(cached), "cache"
        if not tags:
            rule_tags = match_collocation(topic)
            if rule_tags:
                tags, source = rule_tags, "match"

        good: list[Any] = []
        tried: list[Any] = []

        if tags:
            for tag in tags[:answer_num]:
                if tag in tried:
                    continue
                data = self._verify(code, tag)
                tried.append(tag)
                code = str(data.get("topic_code") or code)
                if data.get("answer_result") == 1:
                    good.append(tag)
                good.extend(t for t in (data.get("answer_corrects") or []) if t not in good)
                if data.get("over_status") == 1:
                    break

        if not good and self.probe_enabled:
            # 枚举所有选项，逐个判对；服务端标记 over_status=1 即整题结束
            source = source or "probe"
            for index, opt in enumerate(options):
                tag = opt.get("answer_tag", index)
                if tag in tried:
                    continue
                data = self._verify(code, tag)
                tried.append(tag)
                code = str(data.get("topic_code") or code)
                if data.get("answer_result") == 1:
                    good.append(tag)
                good.extend(t for t in (data.get("answer_corrects") or []) if t not in good)
                if data.get("over_status") == 1:
                    break

        if good:
            self.bank.put(key, good, display_stem(topic))
            if source == "cache":
                self.stats.cache += 1
            elif source == "match":
                self.stats.match += 1
            else:
                self.stats.probe += 1
        else:
            self.stats.wrong_saved += 1

        shown = ",".join(
            display_answer(topic, tag) for tag in (good or tried[:answer_num])
        )
        icon = "🔗" if good else "⚠️"
        self.log(f"  {icon} {stem} → {shown} {source_label(source or 'guess')}")
        return self._submit(code, topic_mode(topic))

    # ── 底层动作 ──
    def _start(self, task_id, release_id, course_id, list_id, task_type, grade) -> dict[str, Any]:
        if self.dry_run:
            raise RuntimeError("dry-run 模式不发起真实请求")
        if self.kind == "study":
            return self.client.study_start_answer(task_id, course_id, list_id, task_type=task_type, grade=grade)
        return self.client.start_answer(task_id, release_id)

    def _verify(self, code: str, answer: Any) -> dict[str, Any]:
        if self.dry_run:
            return {"topic_code": code, "answer_result": 1}
        if self.kind == "study":
            return (self.client.study_verify(code, answer).get("data") or {})
        return (self.client.verify(code, answer).get("data") or {})

    def _submit(self, code: str, mode: int) -> dict[str, Any]:
        spent = random.randint(500, 1500) if mode == 0 else random.randint(2000, 4500)
        if self.dry_run:
            return {"data": {"topic_code": code}}
        if self.kind == "study":
            return self.client.study_submit(code, spent)
        return self.client.submit(code, spent)

    def _answer(self, topic: dict[str, Any], code: str, answer: Any) -> tuple[str, bool, dict[str, Any]]:
        """判一次；错了就看服务端有没有回吐正确答案，再判一次。"""
        answer = self._normalize(topic, answer)
        data = self._verify(code, answer)
        code = str(data.get("topic_code") or code)
        if data.get("answer_result") == 1:
            return code, True, data

        corrects = data.get("answer_corrects") or []
        if corrects:
            fixed = self._normalize(topic, corrects[0])
            data2 = self._verify(code, fixed)
            code2 = str(data2.get("topic_code") or code)
            if data2.get("answer_result") == 1:
                self.stats.fixed += 1
                return code2, True, data2
        return code, False, data

    def _probe(self, topic: dict[str, Any], code: str) -> tuple[str, Any | None]:
        """连续提交错误答案，把本题的正确答案从服务端套出来。"""
        for _ in range(max(1, self.probe_attempts)):
            data = self._verify(code, WRONG_SENTINEL)
            code = str(data.get("topic_code") or code)
            corrects = data.get("answer_corrects") or []
            if corrects:
                return code, corrects[0]
            if data.get("over_status") == 1:
                break
        return code, None

    def _normalize(self, topic: dict[str, Any], answer: Any) -> Any:
        """组词题的答案要变成 "in,many,instances" 这种逗号串。"""
        if topic_mode(topic) == 32 and isinstance(answer, str) and not answer.isdigit():
            return join_phrase(answer)
        return answer

    def _default_answer(self, topic: dict[str, Any]) -> Any:
        if topic_mode(topic) == 32:
            options = topic_options(topic)
            return ",".join(str(o.get("content") or "") for o in options)
        options = topic_options(topic)
        return options[0].get("answer_tag", 0) if options else 0

    def _sleep(self) -> None:
        lo, hi = self.pace
        if hi <= 0:
            return
        time.sleep(random.uniform(lo, hi))


def pick_task(records: list[dict[str, Any]], index: int = 0) -> dict[str, Any]:
    if not records:
        raise SystemExit("没有可用的任务")
    if index >= len(records):
        raise SystemExit(f"任务下标越界：{index}（共 {len(records)} 个）")
    return records[index]


def select_words(words: list[dict[str, Any]], max_score: int = 10) -> dict[str, list[str]]:
    """按「未满分的词」构造 word_map，交给 SubmitChoseWord 重置进度。"""
    word_map: dict[str, list[str]] = {}
    for item in words:
        try:
            score = float(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        if score >= max_score:
            continue
        key = f"{item.get('course_id')}:{item.get('list_id')}"
        word_map.setdefault(key, []).append(str(item.get("word") or ""))
    return word_map
