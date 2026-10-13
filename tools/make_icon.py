# -*- coding: utf-8 -*-
"""生成程序图标 assets/icon.ico（用 Pillow 画，不需要外部素材）。

    python tools/make_icon.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "icon.ico"
CJK_FONTS = (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf")


def build(out_path: Path = OUT, size: int = 256) -> Path | None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("❌ 需要 Pillow：pip install pillow")
        return None

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # 圆角底：深蓝渐变（自己画，省得依赖）
    radius = size // 6
    for y in range(size):
        ratio = y / max(1, size - 1)
        color = (
            int(28 + 20 * ratio),
            int(58 + 40 * ratio),
            int(120 + 60 * ratio),
            255,
        )
        draw.line([(0, y), (size, y)], fill=color)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    image.putalpha(mask)

    # 中间一个大字：优先「词」，没有中文字体就退回 V
    glyph, font = "词", None
    for candidate in CJK_FONTS:
        if Path(candidate).exists():
            try:
                font = ImageFont.truetype(candidate, int(size * 0.56))
                break
            except Exception:
                continue
    if font is None:
        glyph = "V"
        try:
            font = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", int(size * 0.6))
        except Exception:
            font = ImageFont.load_default()

    box = draw.textbbox((0, 0), glyph, font=font)
    draw.text(
        ((size - (box[2] - box[0])) / 2 - box[0], (size - (box[3] - box[1])) / 2 - box[1] - size * 0.04),
        glyph,
        font=font,
        fill=(255, 255, 255, 255),
    )

    # 右下角一个小对勾，表示「自动过」
    tick = int(size * 0.30)
    x0, y0 = int(size * 0.60), int(size * 0.62)
    draw.ellipse([x0, y0, x0 + tick, y0 + tick], fill=(46, 204, 113, 255))
    draw.line(
        [
            (x0 + tick * 0.24, y0 + tick * 0.52),
            (x0 + tick * 0.44, y0 + tick * 0.72),
            (x0 + tick * 0.78, y0 + tick * 0.28),
        ],
        fill=(255, 255, 255, 255),
        width=max(3, tick // 9),
        joint="curve",
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return out_path


if __name__ == "__main__":
    result = build()
    print(f"✅ 图标已生成：{result}" if result else "❌ 生成失败")
    raise SystemExit(0 if result else 1)
