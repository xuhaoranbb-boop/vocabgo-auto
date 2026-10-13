# -*- coding: utf-8 -*-
"""扫码授权取 UserToken 的命令行入口（逻辑都在 vocabgo/qrlogin.py）。

用法::

    python tools/qr_login.py                # 生成二维码 PNG（需 pip install qrcode[pil]）
    python tools/qr_login.py --print-url    # 没有 qrcode 库时，直接打印链接手动打开
    python tools/qr_login.py --timeout 600
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vocabgo.paths import config_path  # noqa: E402
from vocabgo.qrlogin import load_existing_token, open_file, save_qr_png, start_login, token_alive  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="词达人扫码授权取 token")
    parser.add_argument("--config", default=None, help="配置文件路径（默认：程序目录下的 config.json）")
    parser.add_argument("--print-url", action="store_true", help="只打印授权链接，不生成二维码")
    parser.add_argument("--qr-out", default="", help="二维码 PNG 输出路径")
    parser.add_argument("--timeout", type=int, default=300, help="等待扫码的秒数")
    parser.add_argument("--force", action="store_true", help="即使已有有效 token 也重新授权")
    args = parser.parse_args()

    config_file = config_path(args.config)
    existing = load_existing_token(config_file)
    if existing and not args.force and token_alive(existing):
        print("✅ 已配置的 token 仍然有效，无需重新授权（要换账号加 --force）")
        return 0

    qr_png = Path(args.qr_out) if args.qr_out else config_file.parent / "授权二维码.png"
    last_url = ""

    def on_url(url: str, data: bytes | None) -> None:
        nonlocal last_url
        last_url = url
        if args.print_url:
            return
        if data and save_qr_png(url, qr_png):
            print(f"🖼️ 二维码已保存：{qr_png}")
            open_file(qr_png)
            print("👉 用微信扫一扫弹出的二维码（或把链接发到微信里点开）完成授权")
        else:
            args.print_url = True  # 没有 qrcode 库，退回打印链接

    ok, token = start_login(
        config_file=config_file,
        timeout=args.timeout,
        on_url=on_url,
        on_status=print,
    )
    if ok:
        print(f"🔑 user_token = {token}")
        print("ℹ️ 使用期间别在其它设备登录词达人，否则 token 会被刷新")
        return 0
    if last_url:
        print("👉 也可以复制下面这条链接，发到微信任意聊天窗口，用微信点开完成授权：")
        print(last_url)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
