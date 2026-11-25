# -*- coding: utf-8 -*-
"""焦点框视觉验证: 在 notepad 上画框并截图, 检查颜色与发光。"""
import os
import sys
import time
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KZQ_TOKEN", "boxdemo")

import server  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "box_shot.png")

p = subprocess.Popen(["notepad.exe"])
time.sleep(1.2)

import ctypes
u = ctypes.windll.user32
hwnd = 0
u.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(
    lambda h, l: (globals().__setitem__("hwnd", h) or True) if u.GetForegroundWindow() == h else True), 0)
r = ctypes.wintypes.RECT()
u.GetWindowRect(u.GetForegroundWindow(), ctypes.byref(r))
rect = (r.left + 40, r.top + 80, r.left + 340, r.top + 130)
print("target rect", rect)

server.FOCUS_BOX.show(rect)
time.sleep(0.5)
# 再移一次, 验证动画不炸
server.FOCUS_BOX.show((rect[0] + 30, rect[1] + 40, rect[2] + 30, rect[3] + 40))
time.sleep(0.6)

import pyautogui
pyautogui.screenshot(region=(rect[0] - 60, rect[1] - 60, rect[2] - rect[0] + 120,
                            rect[3] - rect[1] + 120)).save(OUT)
print("saved", OUT)

from PIL import Image
im = Image.open(OUT).convert("RGB")
w, h = im.size
print("shot size", w, h)
# 采样边框像素: 找最接近 #38A5FF 的像素并统计
from collections import Counter
cnt = Counter()
best = None
for y in range(h):
    for x in range(w):
        c = im.getpixel((x, y))
        d = abs(c[0] - 0x38) + abs(c[1] - 0xA5) + abs(c[2] - 0xFF)
        if d < 60:
            cnt[c] += 1
            if best is None or d < best[0]:
                best = (d, c, (x, y))
print("近 #38A5FF 像素数", sum(cnt.values()))
print("最接近的像素", best)
print("top5", cnt.most_common(5))
server.FOCUS_BOX.hide()
p.terminate()
