# -*- coding: utf-8 -*-
"""一键打包成 Windows 软件（PyInstaller）。

产物：

    dist/Vocabgo/Vocabgo.exe        图形界面版（无控制台窗口，双击就用）
    dist/VocabgoCLI/VocabgoCLI.exe  命令行版（保留控制台，脚本/排查用）

用法::

    python build_exe.py                 # 两个都打（onedir，启动快）
    python build_exe.py --onefile       # 单文件版（启动慢一点，方便分发）
    python build_exe.py --gui-only      # 只打图形界面版
    python build_exe.py --skip-dist     # 只清理，不打包
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
ASSETS = ROOT / "assets"
ICON = ASSETS / "icon.ico"


def ensure_icon() -> Path | None:
    if ICON.exists():
        return ICON
    try:
        from tools.make_icon import build  # type: ignore
    except Exception:
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            from make_icon import build  # type: ignore
        except Exception as exc:
            print(f"⚠️ 跳过图标：{exc}")
            return None
    try:
        return build(ICON)
    except Exception as exc:
        print(f"⚠️ 生成图标失败：{exc}")
        return None


def kill_running(name: str) -> None:
    """打包前先关掉同名的旧进程：Windows 上进程会锁住 _internal 里的 DLL，
    不关就会出现 PermissionError: [WinError 5] 这种莫名其妙的失败。"""
    if not sys.platform.startswith("win"):
        return
    try:
        found = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return
    if f"{name}.exe" not in (found.stdout or ""):
        return
    print(f"⚠️ 检测到 {name}.exe 正在运行（会锁住旧文件），正在关闭…")
    subprocess.run(["taskkill", "/IM", f"{name}.exe", "/F"], capture_output=True, timeout=20)


def run_pyinstaller(name: str, entry: str, *, windowed: bool, onefile: bool, icon: Path | None) -> bool:
    kill_running(name)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        name,
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD),
        "--specpath",
        str(BUILD),
        "--collect-data",
        "certifi",
        "--collect-submodules",
        "qrcode",  # 二维码展示要用，必须打进包里
        "--collect-submodules",
        "cryptography",  # 微信扫码登录要用的加解密
        "--hidden-import",
        "cryptography.hazmat.backends.openssl",
        "--windowed" if windowed else "--console",
        "--onefile" if onefile else "--onedir",
    ]
    if icon:
        command += ["--icon", str(icon)]
    if ASSETS.exists():
        command += ["--add-data", f"{ASSETS}{';' if sys.platform.startswith('win') else ':'}{ASSETS.name}"]
    command.append(str(ROOT / entry))

    print(f"\n🔨 打包 {name}（{'窗口' if windowed else '控制台'} / {'单文件' if onefile else '目录'}）…")
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        print(f"❌ {name} 打包失败（退出码 {result.returncode}）")
        print(f"   常见原因：{name}.exe 还在运行 → 先关掉界面再重跑；或杀软正在扫描 dist 目录")
        return False
    print(f"✅ {name} 打包完成")
    return True


def post_install(name: str) -> None:
    """把说明和示例配置放进产物目录，用户拿到就能用。"""
    target = DIST / name
    if not target.exists():
        return
    for filename in ("README.md", "config.example.json"):
        source = ROOT / filename
        if source.exists():
            shutil.copy2(source, target / filename)
    (target / "使用说明.txt").write_text(
        "词达人自动答题工具\n"
        "==================\n\n"
        "1. 双击 exe 打开界面\n"
        "2. 点「扫码授权」，用微信扫码（扫不了就把界面里的链接发到微信点开）\n"
        "3. 回到「刷题」页，点「刷新任务」，选中任务，点「开始刷题」\n\n"
        "命令行版：VocabgoCLI.exe --help\n"
        "环境自检：Vocabgo.exe --selftest（结果同时写到 selftest.txt）\n\n"
        "配置文件 config.json 与题库 bank.json 都放在 exe 同目录，换机器一并拷走即可。\n"
        "注意：刷题期间不要在手机或其它设备登录词达人，否则 token 会被刷新失效。\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="打包词达人自动答题工具")
    parser.add_argument("--onefile", action="store_true", help="打成单文件（默认 onedir 目录模式）")
    parser.add_argument("--gui-only", action="store_true", help="只打图形界面版")
    parser.add_argument("--skip-dist", action="store_true", help="只清理旧产物，不打包")
    args = parser.parse_args()

    if args.skip_dist:
        shutil.rmtree(DIST, ignore_errors=True)
        shutil.rmtree(BUILD, ignore_errors=True)
        print("🧹 已清理 dist/ 与 build/")
        return 0

    icon = ensure_icon()
    if icon:
        print(f"🎨 使用图标：{icon}")

    ok = run_pyinstaller("Vocabgo", "vocabgo_app.py", windowed=True, onefile=args.onefile, icon=icon)
    post_install("Vocabgo")

    if not args.gui_only:
        ok = run_pyinstaller("VocabgoCLI", "vocabgo_cli.py", windowed=False, onefile=args.onefile, icon=icon) and ok
        post_install("VocabgoCLI")

    print("\n=== 打包结果 ===")
    for folder in sorted(DIST.glob("*")):
        if folder.is_dir():
            size = sum(item.stat().st_size for item in folder.rglob("*") if item.is_file())
            print(f"  {folder}　（约 {size / 1024 / 1024:.1f} MB）")
        else:
            print(f"  {folder}　（约 {folder.stat().st_size / 1024 / 1024:.1f} MB）")

    print("\n验收：")
    print("  dist\\Vocabgo\\Vocabgo.exe --selftest")
    print("  dist\\VocabgoCLI\\VocabgoCLI.exe config")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
