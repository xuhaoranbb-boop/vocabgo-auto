# -*- coding: utf-8 -*-
"""对着「正在跑的那份真配置」验一次：界面自动保存不会再把 6 小时时间戳抹掉。

模拟的正是踩过的坑：GUI 内存里那份是启动时载入的旧副本（token_saved_at 为空），
用户随便改个参数触发自动保存，整个 dict 被写回磁盘。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vocabgo.settings import load_settings, save_settings  # noqa: E402

LIVE = ROOT / "release" / "vocabgo-auto-v2.1.4-win64" / "config.json"

print("== 0) 现场：真配置里记的时间戳 ==")
before = json.loads(LIVE.read_text(encoding="utf-8"))
print("   token_saved_at =", repr(before["runtime"].get("token_saved_at")))
print("   usertoken      =", before["auth"]["usertoken"][:10] + "…")

print("\n== 1) 先复制一份到临时目录做破坏性实验（不动真文件）==")
with tempfile.TemporaryDirectory() as folder:
    work = Path(folder) / "config.json"
    shutil.copy2(LIVE, work)

    stale = load_settings(work)
    stale["runtime"]["token_saved_at"] = ""       # ← 启动时的旧副本长这样
    stale["runtime"]["loop"] = 5                  # ← 用户改了个参数
    save_settings(stale, work)

    after = json.loads(work.read_text(encoding="utf-8"))
    print("   写回后 token_saved_at =", repr(after["runtime"].get("token_saved_at")))
    print("   改动是否生效 loop =", after["runtime"]["loop"])

    ok_stamp = after["runtime"]["token_saved_at"] == before["runtime"]["token_saved_at"]
    ok_loop = after["runtime"]["loop"] == 5
    print(f"\n   时间戳有没有被抹掉：{'❌ 被抹掉了' if not ok_stamp else '✅ 保住了'}")
    print(f"   其它改动有没有保存：{'✅ 已保存' if ok_loop else '❌ 丢了'}")

print("\n结论：", "✅ 修复生效" if ok_stamp and ok_loop else "❌ 仍有问题")
