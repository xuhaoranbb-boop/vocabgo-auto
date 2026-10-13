# -*- coding: utf-8 -*-
"""词达人自动答题工具 —— 启动入口（双击就用这个文件；打包成 exe 也是它）。

用法::

    python vocabgo_app.py                # 打开图形界面
    python vocabgo_app.py --selftest     # 环境自检（打包后验证 exe 用）
    python vocabgo_app.py --cli run ...  # 命令行模式（等价于 python -m vocabgo）
"""

from __future__ import annotations

import io
import sys

# 打包成「无控制台」的窗口程序后，sys.stdout / sys.stderr 是 None，
# 任何一句 print 都会抛 AttributeError 把程序带崩 —— 这里先兜住。
if sys.stdout is None:  # pragma: no cover - 只在 windowed 模式触发
    sys.stdout = io.StringIO()
if sys.stderr is None:  # pragma: no cover
    sys.stderr = io.StringIO()


def selftest() -> int:
    """环境自检：不联网，只验证「跑起来必需的东西」齐不齐。"""
    import platform

    from vocabgo.crypto import JV_RULES, decode_jv, encode_jv, sign
    from vocabgo.paths import app_dir, config_path, is_frozen
    from vocabgo.settings import load_settings, resolve_llm
    from vocabgo.vision import resolve_tesseract

    checks: list[tuple[str, bool, str, bool]] = []  # (名称, 通过, 说明, 是否关键项)

    def check(name: str, ok: bool, detail: str = "", critical: bool = True) -> None:
        checks.append((name, bool(ok), detail, critical))

    # 1) 依赖能不能 import（打包最容易死在这里）
    try:
        import requests  # noqa: F401

        check("requests 可用", True, f"v{requests.__version__}")
    except Exception as exc:
        check("requests 可用", False, str(exc))
    try:
        import tkinter

        check("tkinter 可用", True, f"Tk {tkinter.TkVersion}")
    except Exception as exc:
        check("tkinter 可用", False, str(exc))
    try:
        import certifi  # noqa: F401

        check("certifi 可用", True, "TLS 根证书已打包")
    except Exception as exc:
        check("certifi 可用", False, str(exc))

    # 2) 协议层逻辑（打包后代码没被裁坏）
    try:
        ok = sign({"a": 1, "b": 2}) == "cc37c3ec0cf50560bbbc91c2d1a9db64"
        check("签名算法正确", ok, "已知向量比对通过" if ok else "结果不符")
        payload = {"topic_code": "T1", "pad": "x" * 600}
        jv = next(iter(JV_RULES))
        check("jv 混淆解码正确", decode_jv(encode_jv(payload, jv), jv) == payload, f"方案 {jv}")
    except Exception as exc:
        check("协议层逻辑", False, str(exc))

    # 3) 路径与配置（打包后最容易「写不进去」）
    directory = app_dir()
    try:
        probe = directory / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        check("程序目录可写", True, str(directory))
    except Exception as exc:
        check("程序目录可写", False, f"{directory} → {exc}")

    config = load_settings()
    resolved = resolve_llm(config)
    check("配置文件可读", True, str(config_path()))
    check("用户令牌已配置", bool((config.get("auth") or {}).get("usertoken")), "待配置：去界面里扫码授权", critical=False)
    check(
        "大模型已配置",
        bool(resolved["key"]),
        f"{resolved['provider']} / {resolved['model']} / 密钥 {'有' if resolved['key'] else '无'}（可选）",
        critical=False,
    )
    exe = resolve_tesseract(str((config.get("vision") or {}).get("tesseract_cmd") or ""))
    check("tesseract 可用", bool(exe), exe or "未找到（可以改用 llm 读图，可选）", critical=False)

    # 二维码组件：扫码授权要在界面里直接弹二维码，缺了它就只能手动复制链接
    try:
        from vocabgo.qrlogin import render_qr_png

        png = render_qr_png("https://open.weixin.qq.com/connect/oauth2/authorize?appid=test")
        check("二维码组件可用", bool(png and png.startswith(b"\x89PNG")), "扫码授权要用（可选）", critical=False)
    except Exception as exc:
        check("二维码组件可用", False, f"{type(exc).__name__}: {exc}（可选）", critical=False)

    lines = [
        "=== 词达人自动答题工具 · 自检 ===",
        f"打包运行  : {'是（PyInstaller）' if is_frozen() else '否（源码）'}",
        f"程序路径  : {sys.executable}",
        f"程序目录  : {directory}",
        f"Python    : {platform.python_version()} ({platform.system()} {platform.release()})",
        "",
    ]
    for name, ok, detail, critical in checks:
        if ok:
            mark = "通过"
        else:
            mark = "失败" if critical else "待配置"
        lines.append(f"[{mark}] {name}" + (f"　— {detail}" if detail else ""))

    failed = [item[0] for item in checks if item[3] and not item[1]]
    lines.append("")
    lines.append("结论：" + ("关键项全部通过 ✅" if not failed else f"关键项失败 ❌ {', '.join(failed)}"))

    report = "\n".join(lines)
    target = directory / "selftest.txt"
    try:
        target.write_text(report, encoding="utf-8")
        report += f"\n\n（报告已写入 {target}）"
    except Exception:
        pass
    try:
        print(report)
    except Exception:
        pass
    return 0 if not failed else 1


def main() -> int:
    args = sys.argv[1:]

    if "--selftest" in args:
        return selftest()

    if "--cli" in args:
        from vocabgo.cli import main as cli_main

        return cli_main([item for item in args if item != "--cli"])

    from vocabgo.gui import main as gui_main

    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
