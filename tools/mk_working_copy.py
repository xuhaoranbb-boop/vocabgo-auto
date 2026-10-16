# -*- coding: utf-8 -*-
"""给新发布包准备「可直接用的工作副本」——把上一个包里那份活配置搬过来。

为什么需要它：`build_release.py` 每次都会把目标目录整个删掉重建，
所以刚登录好的令牌、攒下来的题库、词典缓存都会被清空。
（zip 里本来就**不该**有这些文件，这里动的是本地那个文件夹。）

    python tools/mk_working_copy.py v2.1.4                 # 从最近一个有 config.json 的旧包搬
    python tools/mk_working_copy.py v2.1.4 --from <目录>    # 指定来源目录
    python tools/mk_working_copy.py v2.1.4 --list          # 只看有哪些旧包可搬

搬完会打印令牌时间戳，方便判断还能用多久（词达人令牌约 6 小时）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / "release"
MOVED = ("config.json", "bank.json", "glossary.json")


def candidates() -> list[Path]:
    """release/ 下所有「里面有 config.json」的包，按修改时间从新到旧。"""
    found = [p for p in RELEASE.iterdir() if p.is_dir() and (p / "config.json").exists()]
    return sorted(found, key=lambda p: (p / "config.json").stat().st_mtime, reverse=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="把旧包的活配置搬进新包")
    parser.add_argument("target", help="目标包名或路径，如 v2.1.4 或 release/vocabgo-auto-v2.1.4-win64")
    parser.add_argument("--from", dest="source", default="", help="来源目录（默认用最近一个有配置的旧包）")
    parser.add_argument("--list", action="store_true", help="只列出可搬的旧包")
    parser.add_argument(
        "--stamp",
        default="",
        help="补一个令牌获取时间（ISO，如 2026-10-16T09:34:16）。只在旧包里没记时间时用，"
        "且必须是**真的登录时刻** —— 填早了/填晚了都会让 6 小时提醒失准。",
    )
    args = parser.parse_args()

    listing = candidates()
    if args.list:
        for path in listing:
            stamp = json.loads((path / "config.json").read_text(encoding="utf-8"))
            when = (stamp.get("runtime") or {}).get("token_saved_at") or "(没记时间)"
            print(f"  {path.name}　令牌时间：{when}")
        return 0

    target = Path(args.target)
    if not target.is_absolute():
        if not target.exists():
            target = RELEASE / (target if target.name.startswith("vocabgo-auto-") else f"vocabgo-auto-{target}-win64")
    if not target.exists():
        print(f"❌ 目标目录不存在：{target}")
        return 1

    source = Path(args.source) if args.source else (listing[0] if listing else None)
    if not source or not source.exists():
        print("❌ 没找到可以搬的旧包（release/ 下没有带 config.json 的目录）")
        return 1

    print(f"来源：{source}")
    print(f"目标：{target}")
    for name in MOVED:
        src = source / name
        if src.exists():
            shutil.copy2(src, target / name)
            print(f"  ✅ {name}　{src.stat().st_size / 1024:.0f} KB")
        else:
            print(f"  · {name}　（来源里没有，跳过）")

    config_file = target / "config.json"
    if config_file.exists():
        config = json.loads(config_file.read_text(encoding="utf-8"))
        runtime = config.setdefault("runtime", {})
        if args.stamp and not runtime.get("token_saved_at"):
            runtime["token_saved_at"] = args.stamp
            config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n已补上令牌获取时间：{args.stamp}")
        token = str((config.get("auth") or {}).get("usertoken") or "")
        print(f"\n令牌：{'已配置 ' + token[:10] + '…' if token else '未配置'}")
        print(f"令牌获取时间：{runtime.get('token_saved_at') or '(空 —— 6 小时提醒不会响)'}")
        if token and not runtime.get("token_saved_at"):
            print("提示：这次是「登录后没来得及写时间戳」的旧配置，界面里重新登录一次即可补上。")
    print("\n提醒：zip 是干净的（不含以上文件），分享出去不会泄露；这个文件夹别整个发给别人。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
