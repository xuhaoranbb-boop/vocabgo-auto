# -*- coding: utf-8 -*-
"""本地题库：把答过的题缓存下来，下次直接命中，避免重复试答案。

存储格式（JSON）::

    {
      "31::norm::abandon::|舍弃,放弃|遗弃": {"ans": 1, "stem": "abandon", "ts": 1760000000},
      "32::norm::___ instances::许多例子|...": {"ans": "in,many,instances", ...}
    }

多进程安全：写盘用「文件锁 + 锁内二次读盘合并 + os.replace 原子替换」，
同时跑多个任务进程也不会互相覆盖。
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_BANK_NAME = "bank.json"


@contextmanager
def _file_lock(lock_path: Path) -> Iterator[None]:
    """跨平台文件锁（Windows: msvcrt，POSIX: fcntl）。"""
    with open(lock_path, "a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


class Bank:
    """题库缓存。读-改-写三步在锁内完成，保证并发安全。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path or DEFAULT_BANK_NAME)
        self._cache: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self.load()

    # ── 读写 ──
    def load(self) -> None:
        if not self.path.exists():
            self._cache = {}
            return
        try:
            self._cache = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self._cache, dict):
                self._cache = {}
        except Exception:
            # 题库损坏不应该拖垮整个任务，直接降级为空题库
            self._cache = {}

    def get(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if isinstance(entry, dict):
            return entry.get("ans")
        return None

    def cover_count(self, words: list[str]) -> int:
        """这批词里，有多少个在题库中见过（粗覆盖率）。

        用来在「只有一次机会」的班级任务开跑前提醒用户：题库太薄就别硬上，
        先去自学任务把同一批词喂进题库，再一次拿高分。
        """
        if not words:
            return 0
        haystack = " ".join(f"{entry.get('stem', '')} {key}" for key, entry in self._cache.items()).lower()
        return sum(1 for word in words if word and str(word).lower() in haystack)

    def find_loose(self, prefix: str, limit: int = 200) -> list[tuple[str, Any]]:
        """按「不含选项的题目指纹」找旧记录（选项每次可能不同，精确键会 miss）。

        返回 [(完整键, 答案), ...]。调用方**必须自己校验答案能不能用**：
        内容型答案（词本身）可以复用，下标型答案（选项编号）绝对不要复用 ——
        选项变了下标就是错的，答错反而扣分。
        """
        prefix = prefix + "::"
        hits: list[tuple[str, Any]] = []
        for key, entry in self._cache.items():
            if not key.startswith(prefix):
                continue
            if isinstance(entry, dict) and entry.get("ans") is not None:
                hits.append((key, entry["ans"]))
                if len(hits) >= limit:
                    break
        return hits

    def put(self, key: str, answer: Any, stem: str = "", flush: bool = True) -> None:
        self._cache[key] = {
            "ans": answer,
            "stem": stem[:80],
            "ts": int(time.time()),
        }
        self._dirty = True
        if flush:
            self.flush()

    def flush(self) -> None:
        if not self._dirty:
            return
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.{os.getpid()}.tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(lock_path):
            try:
                disk: dict[str, Any] = {}
                if self.path.exists():
                    try:
                        disk = json.loads(self.path.read_text(encoding="utf-8"))
                    except Exception:
                        disk = {}
                disk.update(self._cache)
                tmp_path.write_text(
                    json.dumps(disk, ensure_ascii=False, indent=1), encoding="utf-8"
                )
                os.replace(tmp_path, self.path)
                self._cache = disk
                self._dirty = False
            finally:
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass

    # ── 统计 ──
    def __len__(self) -> int:
        return len(self._cache)

    def __contains__(self, key: object) -> bool:
        return key in self._cache
