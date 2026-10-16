# -*- coding: utf-8 -*-
"""抓取指定 PID 的全部可见顶层窗口（主窗 + 模态对话框），合并成一张 PNG。"""
import ctypes
import ctypes.wintypes as wt
import sys
import time
from pathlib import Path

from PIL import ImageGrab

u = ctypes.windll.user32
u.SetProcessDPIAware()
CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)


def windows_of(pid: int):
    found = []

    def cb(h, _l):
        p = wt.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and u.IsWindowVisible(h):
            r = wt.RECT()
            u.GetWindowRect(h, ctypes.byref(r))
            n = u.GetWindowTextLengthW(h)
            buf = ctypes.create_unicode_buffer(n + 2)
            u.GetWindowTextW(h, buf, n + 2)
            found.append((int(h), buf.value, r.left, r.top, r.right, r.bottom))
        return True

    u.EnumWindows(CB(cb), 0)
    return found


def main() -> int:
    pid = int(sys.argv[1])
    out = Path(sys.argv[2])
    wins = windows_of(pid)
    for h, t, l, tp, r, b in wins:
        print(f"hwnd={h} title={t!r} rect=({l},{tp})-({r},{b}) size={r-l}x{b-tp}")
    if not wins:
        print("no visible window for pid", pid)
        return 1
    L = min(w[2] for w in wins) - 8
    T = min(w[3] for w in wins) - 8
    R = max(w[4] for w in wins) + 8
    B = max(w[5] for w in wins) + 8
    u.SetForegroundWindow(wins[-1][0])
    time.sleep(0.7)
    img = ImageGrab.grab(bbox=(L, T, R, B), all_screens=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out))
    print("saved", out, img.size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
