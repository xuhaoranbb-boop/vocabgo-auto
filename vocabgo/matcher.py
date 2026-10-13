# -*- coding: utf-8 -*-
"""答案匹配层：不联网、不试错，靠题干结构 + mode=0 顺带学到的词义直接选答案。

词达人题型（topic_mode）：
  0  = 单词记忆（给单词 + 全部释义，无判题，直接提交）
  11 = 句中选义（stem 里带 {word}，考一词多义）
  31 = 词义选择（单/多选）；若 stem.remark 是 list，则是「搭配/组合」多选
  32 = 短语排序（用选项词排成短语，答案是逗号串）
"""

from __future__ import annotations

import json
import re
from typing import Any

WORD_BRACE = re.compile(r"\{(\w+)\}")
NON_WORD = re.compile(r"\s+")


def norm(text: str) -> str:
    """归一化：去掉所有空白并转小写，用于释义比对。"""
    return NON_WORD.sub("", str(text or "")).lower().strip()


def topic_stem(topic: dict[str, Any]) -> str:
    return str((topic.get("stem") or {}).get("content") or "")


def topic_remark(topic: dict[str, Any]) -> Any:
    return (topic.get("stem") or {}).get("remark") or ""


def topic_options(topic: dict[str, Any]) -> list[dict[str, Any]]:
    return list(topic.get("options") or [])


def topic_mode(topic: dict[str, Any]) -> int:
    try:
        return int(topic.get("topic_mode", -1))
    except (TypeError, ValueError):
        return -1


def is_collocation(topic: dict[str, Any]) -> bool:
    """搭配题：mode=31 且 remark 是带 relation 字段的列表（服务端直接给了答案词）。"""
    if topic_mode(topic) != 31:
        return False
    remark = topic_remark(topic)
    return isinstance(remark, list) and bool(remark) and isinstance(remark[0], dict)


def topic_key(topic: dict[str, Any]) -> str:
    """题目指纹，用于题库索引。题干 + 备注 + 选项一起哈希，避免撞题。"""
    remark = topic_remark(topic)
    if isinstance(remark, list):
        remark = json.dumps(remark, ensure_ascii=False, sort_keys=True)
    opts = "|".join(str(opt.get("content") or "")[:20] for opt in topic_options(topic))
    stem = re.sub(r"\s+", " ", topic_stem(topic)).strip().lower()
    kind = "coll" if is_collocation(topic) else "norm"
    return f"{topic_mode(topic)}::{kind}::{stem}::{remark}::{opts.lower()}"


def absorb_word_defs(topic: dict[str, Any], word_defs: dict[str, list[str]]) -> list[str]:
    """mode=0 的题目本身就是「单词 + 释义」，顺手记下，后面选择题直接查表。"""
    if topic_mode(topic) != 0:
        return []
    word = norm(topic_stem(topic))
    defs = [str(opt.get("content") or "") for opt in topic_options(topic) if opt.get("content")]
    if word and defs:
        word_defs[word] = defs
    return defs


def match_collocation(topic: dict[str, Any]) -> list[Any] | None:
    """搭配题：remark 里每个 relation 就是正确选项内容，直接取对应 answer_tag。"""
    remark = topic_remark(topic)
    if not isinstance(remark, list):
        return None
    relations = {
        str(item.get("relation") or "").strip().lower()
        for item in remark
        if isinstance(item, dict) and item.get("relation")
    }
    if not relations:
        return None
    tags: list[Any] = []
    for index, opt in enumerate(topic_options(topic)):
        content = str(opt.get("content") or "").strip().lower()
        if content in relations:
            tags.append(opt.get("answer_tag", index))
    return tags or None


def _defs_of(word: str, word_defs: dict[str, list[str]]) -> list[str] | None:
    if word in word_defs:
        return word_defs[word]
    for known, defs in word_defs.items():
        if word.startswith(known) or known.startswith(word):
            return defs
    return None


def match_word_to_def(word: str, topic: dict[str, Any], word_defs: dict[str, list[str]]) -> Any | None:
    """用 mode=0 学到的释义去选中选项。三级兜底：完全相等 → 包含 → 关键词命中。"""
    defs = _defs_of(norm(word), word_defs)
    if not defs:
        return None
    options = topic_options(topic)
    defs_norm = [norm(d) for d in defs]

    for opt in options:
        content = norm(opt.get("content"))
        for target in defs_norm:
            if content and (content == target or target in content or content in target):
                return opt.get("answer_tag")

    for opt in options:
        content = str(opt.get("content") or "")
        for definition in defs:
            core = re.sub(r"^[a-z]+\.\s*", "", definition)
            for keyword in re.split(r"[；;，,、]", core):
                keyword = keyword.strip()
                if len(keyword) >= 2 and keyword in content:
                    return opt.get("answer_tag")
    return None


def match_choice(topic: dict[str, Any], word_defs: dict[str, list[str]]) -> Any | None:
    """选择题（31 / 11 / 其他）的规则匹配入口，命中返回 answer_tag。"""
    if is_collocation(topic):
        return match_collocation(topic)

    stem = topic_stem(topic)
    mode = topic_mode(topic)

    if mode == 32:
        return None  # 组词题交给 LLM / 试答案链路

    # mode=11 的题干形如 "He {abandoned} the plan." —— 取花括号里的词
    brace = WORD_BRACE.search(stem)
    word = brace.group(1) if brace else stem
    word = norm(word)
    if not word or word.startswith("_"):
        return None
    return match_word_to_def(word, topic, word_defs)


def display_answer(topic: dict[str, Any], answer: Any) -> str:
    """把答案渲染成人看的短字符串。"""
    if isinstance(answer, (list, tuple)):
        return ",".join(str(a) for a in answer)[:40]
    if isinstance(answer, str):
        return answer[:40]
    options = topic_options(topic)
    if isinstance(answer, int) and 0 <= answer < len(options):
        return str(options[answer].get("content") or "?")[:40]
    return str(answer)[:40]


def display_stem(topic: dict[str, Any]) -> str:
    stem = topic_stem(topic).replace("\n", " ")
    remark = topic_remark(topic)
    if isinstance(remark, str) and remark:
        return f"{stem[:32]} ({remark[:12]})"
    return stem[:44]


def answer_index_of(topic: dict[str, Any], answer: Any) -> int | None:
    """把 answer_tag 换算成选项下标（两者在多数题型里一致，这里做一次显式校验）。"""
    options = topic_options(topic)
    if isinstance(answer, int) and 0 <= answer < len(options):
        return answer
    for index, opt in enumerate(options):
        if str(opt.get("answer_tag")) == str(answer):
            return index
    return None


def blank_count(topic: dict[str, Any]) -> int:
    return topic_stem(topic).count("_")
