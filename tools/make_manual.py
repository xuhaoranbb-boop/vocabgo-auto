# -*- coding: utf-8 -*-
"""把 docs/说明书.html 转成单文件版：截图内嵌成 base64，离线也能看。

    python tools/make_manual.py                      # → dist/说明书.html
    python tools/make_manual.py 输出路径.html
"""

from __future__ import annotations

import argparse
import base64
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "说明书.html"
IMAGE_TAG = re.compile(r'src="([^"]+\.(?:png|jpe?g|gif|webp))"', re.IGNORECASE)


def build(source: Path = SOURCE, out: Path | None = None) -> Path:
    if not source.exists():
        raise SystemExit(f"❌ 找不到说明书源文件：{source}")

    html = source.read_text(encoding="utf-8")
    inlined = 0

    def embed(match: re.Match[str]) -> str:
        nonlocal inlined
        relative = match.group(1)
        image = (source.parent / relative).resolve()
        if not image.exists():
            print(f"⚠️ 截图缺失，保持原样：{relative}")
            return match.group(0)
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }.get(image.suffix.lower(), "image/png")
        data = base64.b64encode(image.read_bytes()).decode("ascii")
        inlined += 1
        print(f"  内嵌 {relative}（{len(data) // 1024} KB → base64）")
        return f'src="data:{mime};base64,{data}"'

    html = IMAGE_TAG.sub(embed, html)

    target = out or (ROOT / "dist" / "说明书.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    print(f"✅ 单文件说明书已生成：{target}（内嵌 {inlined} 张图，{target.stat().st_size // 1024} KB）")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="生成单文件版说明书")
    parser.add_argument("out", nargs="?", default="", help="输出路径（默认 dist/说明书.html）")
    args = parser.parse_args()
    build(out=Path(args.out) if args.out else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
