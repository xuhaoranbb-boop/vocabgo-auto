# -*- coding: utf-8 -*-
"""在线词典 + 本地词库：给答题引擎补上「查得到」的能力。

为什么要它
----------
词达人考的大部分题本质是「这个词是什么意思」。让大模型**凭记忆**答，正确率靠运气；
查词典则是查得到的确定性。

数据源（按顺序，谁先给结果算谁的）：

1. **本地缓存**（``glossary.json``）—— 查过的词直接命中，离线可用，不重复请求；
2. **本地词库文件**（如果放了 ``ecdict.csv`` / ``ecdict.json``）—— 开源英汉词库 ECDICT，
   77 万词条，完全离线；
3. **有道词典在线接口** —— 免费、无需 key、返回中文释义。这是常态数据源。

查到的释义有两个用途：

- **直接匹配选项**：题干里有目标词、选项是中文释义时，按中文重叠度挑出正确项（来源标记 ``词库``）；
- **喂给大模型**：把权威释义塞进 prompt，让它在这几个候选里挑，而不是凭空回忆。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

from . import net

YOUDOU_API = "https://dict.youdao.com/jsonapi"
CACHE_NAME = "glossary.json"
ECDICT_NAME = "ecdict.json"

# 词性/修饰前缀，比对时先剥掉
_POS_PREFIX = re.compile(r"^\s*(?:[a-z]{1,5}\.\s*)+", re.IGNORECASE)
_SPLIT = re.compile(r"[；;|,，、/]|\.\s")


def _clean(text: str) -> str:
    """把释义条目清成可比对的核心中文。"""
    text = _POS_PREFIX.sub("", str(text or "").strip())
    text = re.sub(r"[<>【】\[\]（）()「」“”\"']", "", text)
    return text.strip()


def meaning_units(text: str) -> list[str]:
    """把一条释义切成若干语义单元，便于做重叠比对。"""
    raw = str(text or "").strip()
    # 先剥掉整串开头的词性标记（n. / v. / adj. …）。
    # 否则 "n. " 里的 ". " 会被当作分隔符，切出一个孤零零的 "n" 当语义单元，
    # 把匹配分数算歪（实测：0.5 变成 0.33）。
    raw = _POS_PREFIX.sub("", raw)
    out: list[str] = []
    for piece in _SPLIT.split(raw):
        cleaned = _clean(piece)
        if not cleaned:
            continue
        # 单个英文字母是残留的词性标记，不是释义
        if len(cleaned) < 2 and re.fullmatch(r"[a-zA-Z]", cleaned):
            continue
        out.append(cleaned)
    return out


class Glossary:
    """词库：本地缓存 + 本地词库文件 + 在线词典。"""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        logger: Any = None,
        enabled: bool = True,
        timeout: float = 8.0,
    ) -> None:
        self.path = Path(path) if path else None
        self.log = logger or (lambda _m: None)
        self.enabled = enabled
        self.timeout = timeout
        self._cache: dict[str, list[str]] = {}
        self._offline: dict[str, list[str]] | None = None  # 本地词库文件（懒加载）
        self._dirty = False
        self.load()

    # ── 存取 ──
    def load(self) -> None:
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._cache = {str(k): list(v) for k, v in data.items() if isinstance(v, list)}
            except Exception:
                self._cache = {}

    def flush(self) -> None:
        if not (self.path and self._dirty):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._cache, ensure_ascii=False, indent=1), encoding="utf-8")
            self._dirty = False
        except Exception as exc:
            self.log(f"    ⚠️ 词库缓存写入失败：{exc}")

    def __len__(self) -> int:
        return len(self._cache)

    # ── 查询 ──
    def lookup(self, word: str) -> list[str]:
        """查一个词的中文释义；查不到返回空列表。"""
        key = str(word or "").strip().lower()
        if not key or not self.enabled:
            return []
        if key in self._cache:
            return self._cache[key]

        meanings: list[str] = []
        meanings.extend(self._from_offline(key))
        if not meanings:
            meanings.extend(self._from_youdao(key))
        if meanings:
            self._cache[key] = meanings
            self._dirty = True
        return meanings

    def _from_offline(self, word: str) -> list[str]:
        """本地词库文件（ECIDT 之类的英汉词库），完全离线。"""
        if self._offline is None:
            self._offline = {}
            for candidate in self._offline_candidates():
                try:
                    data = json.loads(candidate.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(data, dict):
                    self._offline = {str(k).lower(): v for k, v in data.items() if isinstance(v, list)}
                    self.log(f"    📚 已加载本地词库 {candidate.name}（{len(self._offline)} 词条）")
                    break
        return list(self._offline.get(word) or [])

    def _offline_candidates(self) -> Iterable[Path]:
        seen: list[Path] = []
        if self.path:
            seen.append(self.path.parent / ECDICT_NAME)
        seen.append(Path.cwd() / ECDICT_NAME)
        for item in seen:
            if item not in seen[: seen.index(item) + 1][:-1]:  # 去重
                yield item

    def _from_youdao(self, word: str) -> list[str]:
        """有道词典（免费、无需 key）。"""
        try:
            resp = net.request(
                "GET",
                YOUDOU_API,
                params={"q": word},
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/132.0.0.0 Safari/537.36",
                    "Referer": "https://dict.youdao.com/",
                },
                timeout=self.timeout,
            )
            body = resp.json()
        except Exception as exc:
            self.log(f"    ⚠️ 词典查询失败（{word}）：{net.friendly_error(exc)[:60]}")
            return []

        out: list[str] = []
        for entry in (body.get("ec") or {}).get("word") or []:
            for trs in entry.get("trs") or []:
                for tr in trs.get("tr") or []:
                    for item in (tr.get("l") or {}).get("i") or []:
                        if isinstance(item, str) and item.strip():
                            out.append(item.strip())
        # 网络释义兜底（专业词/新词查不到时）
        if not out:
            for web in (body.get("web_trans") or {}).get("web-translation") or []:
                for trans in web.get("trans") or []:
                    value = trans.get("value")
                    if isinstance(value, str) and value.strip():
                        out.append(value.strip())
        return out[:8]


def _unit_key(unit: str) -> str:
    """去掉中文虚词后缀，便于跨词性比对。

    实测踩过的坑：选项写「v. 使弯曲；扭曲」，词典给的是「adj. 弯曲的；扭曲的」，
    就差一个「的」字，匹配度从 0.83 掉到 0.50，直接低于阈值被拒。
    """
    for suffix in ("的", "地", "得"):
        if len(unit) > 2 and unit.endswith(suffix):
            return unit[:-1]
    return unit


def _unit_match(unit: str, other: str) -> float:
    """两个语义单元的匹配度：相等 1.0；包含关系按长度比例给分；否则 0。"""
    if unit == other:
        return 1.0
    a, b = _unit_key(unit), _unit_key(other)
    if a and a == b:
        return 1.0
    if len(a) >= 2 and len(b) >= 2 and (a in b or b in a):
        # 按长度比例给分，避免「药物」命中「药物滥用」这种过度匹配
        short, long_ = (a, b) if len(a) <= len(b) else (b, a)
        return len(short) / len(long_)
    return 0.0


def score_overlap(meaning: str, option: str) -> float:
    """选项释义被词典释义覆盖的比例（0~1）。

    方向很关键：**按选项切分、去词典里找**。
    反过来（按词典切分）会出事 —— 词典一条释义动辄上百个字、十几个语义单元，
    正确答案也只能命中两三个，除下来趋近 0，就永远匹配不上了。
    """
    dict_units = meaning_units(meaning)
    opt_units = meaning_units(option)
    if not dict_units or not opt_units:
        return 0.0
    total = 0.0
    for unit in opt_units:
        best = 0.0
        for other in dict_units:
            best = max(best, _unit_match(unit, other))
            if best >= 1.0:
                break
        total += best
    return total / len(opt_units)


def match_option(word_meanings: list[str], options: list[dict[str, Any]]) -> int | None:
    """在选项里挑出与词典释义最匹配的那一个，返回下标；不确定就返回 None。

    阈值定得比较保守：宁可说「不知道」交给大模型/试答案，也别自信地选错 ——
    尤其在只有一次机会的班级任务里。
    """
    if not word_meanings or not options:
        return None
    joint = "；".join(word_meanings)
    best_index, best_score = None, 0.0
    second = 0.0
    for index, option in enumerate(options):
        content = str(option.get("content") or "")
        score = score_overlap(joint, content)
        if score > best_score:
            best_index, second, best_score = index, best_score, score
        elif score > second:
            second = score
    if best_index is None or best_score < 0.60:
        return None
    # 优势不够明显（两个选项都像）时不硬选
    if second >= 0.60 and best_score - second < 0.15:
        return None
    return best_index


def meaning_hint(word_meanings: list[str], limit: int = 3) -> str:
    """把释义压成一行，塞进大模型的 prompt 里当权威参考。"""
    if not word_meanings:
        return ""
    cleaned = [re.sub(r"\s+", " ", m).strip() for m in word_meanings if m.strip()]
    return "；".join(cleaned[:limit])[:300]
