# -*- coding: utf-8 -*-
"""列出当前可见的顶层窗口（标题 + 位置），用来判断屏幕上现在有什么。"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

u = ctypes.windll.user32
u.SetProcessDPIAware()
CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

rows: list[tuple[str, int, tuple[int, int, int, int]]] = []


def cb(h, _l):
    if not u.IsWindowVisible(h) or u.IsIconic(h):
        return True
    n = u.GetWindowTextLengthW(h)
    if n <= 0:
        return True
    buf = ctypes.create_unicode_buffer(n + 2)
    u.GetWindowTextW(h, buf, n + 2)
    r = wt.RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    pid = wt.DWORD()
    u.GetWindowThreadProcessId(h, ctypes.byref(pid))
    rows.append((buf.value, int(pid.value), (r.left, r.top, r.right, r.bottom)))
    return True


u.EnumWindows(CB(cb), 0)
print(f"可见窗口 {len(rows)} 个：")
for title, pid, rect in rows:
    print(f"  [{pid:>6}] {rect[0]:>5},{rect[1]:>5} {rect[2]-rect[0]:>5}x{rect[3]-rect[1]:<5} {title}")
