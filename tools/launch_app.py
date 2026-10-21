# -*- coding: utf-8 -*-
"""在 Windows 上把 GUI 程序真正「甩开」启动：不继承父进程的 stdout/stderr。

为什么要单独写：直接用 PowerShell 的 Start-Process 或 subprocess 时，
子进程会继承父 shell 的管道句柄，父 shell（比如自动化工具）会一直等到 GUI 退出，
表现就是「命令卡住不动」。这里把三个标准流都指向 DEVNULL 并指定
DETACHED_PROCESS / CREATE_NEW_PROCESS_GROUP，启动完立刻返回 PID。

    python tools/launch_app.py [exe 路径]
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT = Path(__file__).resolve().parent.parent / "release" / "vocabgo-auto-v2.1.4-win64" / "Vocabgo.exe"


def main() -> int:
    exe = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    if not exe.exists():
        print(f"❌ 找不到：{exe}")
        return 1

    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_BREAKAWAY_FROM_JOB"):
        flags |= int(getattr(subprocess, name, 0))

    try:
        proc = subprocess.Popen(
            [str(exe)],
            cwd=str(exe.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )
    except OSError:
        # CREATE_BREAKAWAY_FROM_JOB 在某些作业对象里不被允许，去掉再来一次
        flags &= ~int(getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0))
        proc = subprocess.Popen(
            [str(exe)],
            cwd=str(exe.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )

    print(f"已启动 PID={proc.pid}")
    print(f"  程序：{exe}")
    print(f"  目录：{exe.parent}")

    for i in range(12):
        time.sleep(1)
        code = proc.poll()
        if code is not None:
            print(f"  ❌ 第 {i + 1} 秒就退出了，退出码 {code}")
            return 2
        if i == 2:
            print("  ✅ 已存活 3 秒，进程稳定")
    return 0


if __name__ == "__main__":
    sys.exit(main())
