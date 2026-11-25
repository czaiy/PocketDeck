# -*- coding: utf-8 -*-
"""KZQ PC Remote —— 手机控制电脑一体化控制台（服务端）

功能:
  - 触控板 / 鼠标点击 / 双指滚动
  - 文字输入(剪贴板粘贴, 支持中文) / 快捷键 / 虚拟桌面
  - 语音: 手机输入法语音 -> 文本框 -> 发送到电脑光标处
  - 遥控模式: 手机 D-Pad 方向键做空间焦点导航 (Spatial Navigation), OK 激活, Esc 返回,
    PC 端高亮边框实时跟随焦点元素
  - 系统命令: 关机 / 重启 / 睡眠 / 锁屏 / 关屏幕 / 切换显示器 / 音量 / 媒体键

运行:  python server.py          手机与电脑连同一 Wi-Fi
测试:  set KZQ_DRYRUN=1          只打印动作不真正执行
"""
import atexit
import ctypes
import ctypes.wintypes
import hashlib
import importlib.util
import io
import json
import os
import queue
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pyautogui
import pyperclip
from pynput.keyboard import Controller as KbController, Key as KbKey

import site_profiles as SP
from nav_engine import NavEngine

# 多显示器 / 高 DPI：UIA 的 BoundingRectangle 与 Win32 layered 窗口都按物理像素走，
# 进程必须先声明 per-monitor v2，否则 150%/200% 缩放下焦点框会偏移、跨屏尺寸错。
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

try:
    import qrcode
    import qrcode.image.svg
    HAS_QR = True
except ImportError:
    HAS_QR = False

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
CUSTOM_FILE = BASE / "custom.json"
PORT = int(os.environ.get("KZQ_PORT", "9721"))
DRY = os.environ.get("KZQ_DRYRUN") == "1"
NO_BROWSER = os.environ.get("KZQ_NO_BROWSER") == "1"
TOKEN = os.environ.get("KZQ_TOKEN") or ""
if not TOKEN:
    # token 持久化: 重启/换机后不变, 手机页面永不因服务重启而失效(删除 token.txt 可轮换)
    TOKEN_FILE = BASE / "token.txt"
    try:
        TOKEN = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        TOKEN = ""
    if not TOKEN:
        TOKEN = secrets.token_hex(4)
        try:
            TOKEN_FILE.write_text(TOKEN, encoding="utf-8")
        except Exception:
            pass

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0  # 关掉默认延迟, 触控板才跟手


LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.log")


def log(msg: str) -> None:
    print(f"[KZQ] {msg}", flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 鼠标 / 键盘
def mouse_move(dx: int, dy: int) -> None:
    if DRY:
        log(f"DRYRUN 鼠标移动 dx={dx} dy={dy}")
    else:
        pyautogui.moveRel(int(dx), int(dy), duration=0)


def mouse_click(button: str = "left", clicks: int = 1) -> None:
    if DRY:
        log(f"DRYRUN 点击 {button} x{clicks}")
    else:
        pyautogui.click(button=button, clicks=clicks)


def mouse_scroll(n: int) -> None:
    if DRY:
        log(f"DRYRUN 滚动 {n}")
    else:
        pyautogui.scroll(int(n))


def mouse_down() -> None:
    if DRY:
        log("DRYRUN 按下左键")
    else:
        pyautogui.mouseDown()


def mouse_up() -> None:
    if DRY:
        log("DRYRUN 松开左键")
    else:
        pyautogui.mouseUp()


def type_text(text: str) -> None:
    if DRY:
        log(f"DRYRUN 输入文本({len(text)}字): {text[:40]}")
    else:
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        log(f"输入文本({len(text)}字)")


_KB = KbController()  # pynput 键盘控制器, SendInput UNICODE 注入, 不占剪贴板

# ---- 实时流镜像: 手机框是文本唯一事实源, 服务端维护镜像逐条对账 ----
# 锚定编辑 {at,del,ins}: 先按 (at - 光标) 差值发方向键把电脑光标对齐到编辑点, 再删/注字。
# 每条操作自带绝对坐标, 前面丢一条/乱序只影响那一条, 后续自动回正——不会累积漂移。
# 镜像坐标是"字形簇"坐标(= 用户感知字符, 1 个 emoji 算 1): 手机 JS 的字符串索引是 UTF-16 码元、
# Python len 是码点、电脑端方向键在 Chromium 系应用按字形簇移动 —— 客户端发 at/del 前按
# Intl.Segmenter 换算, 服务端镜像算术一律按簇计数(_g_* 助手), UIA caret 偏移用 _g_u16_index 换算。
# 相对按键把电脑光标移出 [0, g_len] 之外时(负值=岛左侧前缀区, >g_len=右侧后缀区),
# 岛文本不变只挪光标账目(岛外删/注字对岛不可见); 回车/翻页等无法建模的键则整岛作废,
# 退化为原点跟随, 直到下一条锚定操作或聚焦预填重建镜像。
_SRV_BUF = ""    # 手机视角的电脑端文本镜像(原文, 不做字符清洗)
_SRV_CARET = 0   # 镜像光标位置(字形簇索引); None = 失明(电脑端位置不可知, 下一条锚定先校准)
_SRV_FULL = False  # 镜像是否覆盖整个电脑输入框(聚焦预填后 True): 文档级锚跳(Ctrl+Home/End)只在整框镜像下安全
_SRV_BLIND = False  # UIA 盲区应用: 光标读不到, 账目不可信 -> 纯光标定位改用字段两端绝对锚定(不依赖上一状态)
_SRV_LAST_MOVE_TS = 0.0  # 最近一次光标移动的时间戳: 2s 内的读回若与账目不符, 按 provider 读数僵死处理
_SRV_MISMATCH = False    # _srv_move 内部信号: 本次移动因"焦点文本≠镜像"被对账打断
_SRV_MISMATCH_LOG_TS = 0.0
_SRV_LOCK = threading.Lock()   # ThreadingHTTPServer 并发 + 多手机时串行化镜像操作
_CHROMIUM_READ_LAST = 0.0      # Chromium 显式读回限频(仅点按同步)
_CHROMIUM_READ_MIN_GAP = 0.3   # 最小间隔(秒): 读回只读文本开销可接受, 窗口外的点击立即同步

# 无法在镜像坐标建模的相对键: 电脑端文本/光标离开镜像坐标系, 整岛作废(客户端同步重置镜像)
_SRV_BLIND_KEYS = {"enter", "up", "down", "home", "end", "pageup", "pagedown", "delete"}

_SRV_KEYMAP = {
    "enter": KbKey.enter, "esc": KbKey.esc, "tab": KbKey.tab, "backspace": KbKey.backspace,
    "delete": KbKey.delete, "space": KbKey.space, "up": KbKey.up, "down": KbKey.down,
    "left": KbKey.left, "right": KbKey.right, "home": KbKey.home, "end": KbKey.end,
    "pageup": KbKey.page_up, "pagedown": KbKey.page_down,
}


def _srv_tap(key_name: str, n: int = 1) -> None:
    """pynput 按键(纯 python 循环无额外延迟, 连发方向键/回车才跟手); 表外按键走 pyautogui。
    n=0 不按键 —— 此前的 max(1, n) 会把"右移 0 次"变成按一次, 导致 at=0 定位必然偏到 1。"""
    k = _SRV_KEYMAP.get(key_name)
    for _ in range(max(0, n)):
        if k:
            _KB.press(k)
            _KB.release(k)
        else:
            press_key(key_name)


def _srv_doc_anchor(end: bool) -> None:
    """Ctrl+End / Ctrl+Home: 文档级定位(整框镜像下, 岛坐标==字段绝对坐标才可用)"""
    pyautogui.hotkey("ctrl", "end" if end else "home")


def _srv_anchor_head() -> None:
    """字段头锚定(Ctrl+Home 后补一次 Left):
    ProseMirror 系(实测) Ctrl+Home 光标落点=1(位置 0 是段落标记位, 光标最小停留 1),
    Left 归零; 普通编辑器 Ctrl+Home 落点=0, Left 无操作。两类编辑器都精确归零。"""
    _srv_doc_anchor(False)
    _srv_tap("left", 1)


# ---- 字形簇(grapheme)助手: 协议统一坐标 = 用户感知字符 ----
# 覆盖: CRLF / 区域指示符成对(旗帜) / ZWJ 连接 / 变体选择符 / 肤色修饰 / keycap / 组合符号。
# Python json 会把代理对合成单个码点, 无需处理孤立代理对。
_VS_SET = {0xFE0E, 0xFE0F}


def _g_spans(s: str) -> list:
    """把字符串切成字形簇, 返回每簇的 (start, end) 码点下标"""
    spans = []
    i, n = 0, len(s)
    while i < n:
        j = i + 1
        cp = ord(s[i])
        if s[i] == "\r" and j < n and s[j] == "\n":
            j += 1                                  # CRLF: 电脑端一次退格删掉
        elif 0x1F1E6 <= cp <= 0x1F1FF and j < n and 0x1F1E6 <= ord(s[j]) <= 0x1F1FF:
            j += 1                                  # 两个区域指示符 = 一面旗帜
        while j < n:
            k = ord(s[j])
            if k == 0x200D and j + 1 < n:           # ZWJ: 与下一簇连接(家庭 emoji 等)
                j += 2
                continue
            if (k in _VS_SET or 0x1F3FB <= k <= 0x1F3FF or k == 0x20E3
                    or unicodedata.category(s[j]) in ("Mn", "Me", "Mc")):
                j += 1                              # 变体选择符/肤色/keycap/组合符号附着本簇
                continue
            break
        spans.append((i, j))
        i = j
    return spans


def _g_len(s: str) -> int:
    return len(_g_spans(s))


def _g_cp_index(s: str, g: int) -> int:
    """字形簇索引 -> 码点下标(簇起点); 越界收敛到边界"""
    if g <= 0:
        return 0
    spans = _g_spans(s)
    if g >= len(spans):
        return len(s)
    return spans[g][0]


def _g_splice(s: str, at_g: int, del_g: int, ins: str) -> str:
    """按字形簇坐标替换: 从 at_g 起删 del_g 个簇, 插入 ins"""
    a = _g_cp_index(s, at_g)
    b = _g_cp_index(s, at_g + max(0, del_g))
    return s[:a] + ins + s[b:]


def _u16_len(s: str) -> int:
    """字符串的 UTF-16 码元宽度(手机 JS 侧的 .length)"""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in s)


def _g_u16_index(s: str, u16: int) -> int:
    """utf16 偏移 -> 字形簇索引; 落在簇中间时归到簇尾"""
    acc = 0
    spans = _g_spans(s)
    for gi, (a, b) in enumerate(spans):
        if acc >= u16:
            return gi
        w = _u16_len(s[a:b])
        if acc + w > u16:
            return gi + 1
        acc += w
    return len(spans)


def _caret_norm_u16(raw: str, u16):
    """UIA caret(utf16 偏移, 原始文本) -> 规范化文本(\\r\\n -> \\n)中的 utf16 偏移"""
    if not isinstance(u16, int) or u16 < 0:
        return None
    out = 0
    acc = 0
    i, n = 0, len(raw)
    while i < n and acc < u16:
        ch = raw[i]
        if ch == "\r" and i + 1 < n and raw[i + 1] == "\n":
            if acc + 1 >= u16:      # caret 落在 \r 与 \n 之间: 视为 \n 之后
                return out + 1
            out += 1
            acc += 2
            i += 2
        else:
            w = 2 if ord(ch) > 0xFFFF else 1
            out += w
            acc += w
            i += 1
    return out


def _caret_raw_u16(raw: str, u16):
    """规范化文本(\\n)中的 utf16 偏移 -> 原始文本(\\r\\n)中的 utf16 偏移。
    设置 Win32/UIA 光标必须转回原始坐标, 否则每个前置换行都会让落点提前一格。"""
    if not isinstance(u16, int) or u16 < 0:
        return None
    out = 0
    acc = 0
    i, n = 0, len(raw)
    while i < n and acc < u16:
        ch = raw[i]
        if ch == "\r" and i + 1 < n and raw[i + 1] == "\n":
            out += 2
            acc += 1
            i += 2
        else:
            w = 2 if ord(ch) > 0xFFFF else 1
            out += w
            acc += w
            i += 1
    return out


def _g_u16_offset(s: str, g: int) -> int:
    """字形簇索引 -> 规范化文本中的 utf16 偏移(给手机端 setSelectionRange 用)"""
    return _u16_len(s[:_g_cp_index(s, g)])


# ---- EM_SETSEL 坐标空间探测 ----
# WM_GETTEXT 返回 \r\n(换行占 2 个 utf16), 但 EM_SETSEL/EM_GETSEL 的坐标空间因控件而异:
# RichEdit 系内部存 \r, 换行按 1 个字符计; 经典 Edit 按 \r\n 两个字符计。
# 判别方法: EM_SETSEL(0, 超大) 全选(Ctrl+A 等价, 终点必然被控件钳位到自身长度),
# EM_GETSEL 读回终点 = 控件 EM 空间长度, 与 raw/norm 两个候选长度比对。
# 注意不能用"SET(超大起点)看钳位"——Win11 记事本对超大起点不钳位(实测返回 9896087),
# 会把选区/光标打出文本外(光标消失)并误判空间。
_EM_SPACE = {}   # hwnd -> "norm"(换行算1, 与规范化文本一一对应) | "raw"(换行算2, 与 WM_GETTEXT 文本一致)


def _probe_em_space(hwnd, raw_u16_len: int, norm_u16_len: int) -> str:
    """全选探测: 返回该控件的 EM 坐标空间类型; 读数异常时默认 norm(实测主通道)"""
    saved = _w32_send(hwnd, _EM_GETSEL)
    _w32_send(hwnd, _EM_SETSEL, 0, 0x7FFFFFF0)
    sel = _w32_send(hwnd, _EM_GETSEL)
    if saved is not None:
        s = saved & 0xFFFF
        e = (saved >> 16) & 0xFFFF
        if e < s:
            e = s
        _w32_send(hwnd, _EM_SETSEL, s, e)   # 恢复原选区/光标
    if sel is None:
        return "norm"
    s2 = sel & 0xFFFF
    e2 = (sel >> 16) & 0xFFFF
    if e2 < s2:
        e2 = s2
    if e2 == raw_u16_len and raw_u16_len != norm_u16_len:
        return "raw"
    if e2 == norm_u16_len and raw_u16_len != norm_u16_len:
        return "norm"
    return "norm"   # 两种空间同长(无换行)或读数异常: 默认与规范化文本一致


def _em_space_kind(hwnd, raw_u16_len: int, norm_u16_len: int) -> str:
    """探测/返回该控件的 EM 坐标空间类型(每控件仅探测一次, 结果留日志)"""
    kind = _EM_SPACE.get(hwnd)
    if kind:
        return kind
    kind = _probe_em_space(hwnd, raw_u16_len, norm_u16_len)
    _EM_SPACE[hwnd] = kind
    log(f"EM 坐标空间探测: hwnd={hwnd} 全选终点判定 raw={raw_u16_len} norm={norm_u16_len} -> {kind}")
    return kind


def _em_sel_to_norm_u16(raw: str, em_pos, kind: str):
    """EM_GETSEL 读数 -> 规范化文本中的 utf16 偏移"""
    if not isinstance(em_pos, int) or em_pos < 0:
        return None
    if kind == "norm":
        return em_pos
    return _caret_norm_u16(raw, em_pos)


def _norm_u16_to_em(raw: str, norm_u16: int, kind: str) -> int:
    """规范化 utf16 偏移 -> EM_SETSEL 坐标"""
    if kind == "norm":
        return norm_u16
    r = _caret_raw_u16(raw, norm_u16)
    return r if r is not None else norm_u16


# ---- Win32 消息直控 (Edit/RichEdit 系应用, 绕过不稳定的 UIA provider) ----
# Win11 记事本的 UIA provider 反复出现调用超时/读数僵死 (2026-11-25 实证), 而这类控件
# 全部吃标准 EM_* 消息: 直读全文/光标、直设光标都是毫秒级且无符号/单位歧义。
# 仅匹配前台焦点窗口类名含 "edit" 的控件(Edit/RichEditD2DPT/RICHEDIT50W/...);
# Chromium/Electron 等无 Win32 Edit, 自动回落 UIA 通道。
class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("hwndActive", ctypes.c_void_p), ("hwndFocus", ctypes.c_void_p),
                ("hwndCapture", ctypes.c_void_p), ("hwndMenuOwner", ctypes.c_void_p),
                ("hwndMoveSize", ctypes.c_void_p), ("hwndCaret", ctypes.c_void_p),
                ("rcCaret", ctypes.c_int * 4)]

_WM_GETTEXT, _WM_GETTEXTLENGTH = 0x000D, 0x000E
_EM_GETSEL, _EM_SETSEL, _EM_SCROLLCARET = 0x00B0, 0x00B1, 0x00B7
_SMTO_ABORTIFHANG = 0x0002


def _w32_send(hwnd, msg, wp=0, lp=0, timeout=300):
    """带超时的跨进程消息发送; 目标挂死 300ms 即放弃, 返回 None(不阻塞 UIA/请求线程)"""
    res = ctypes.wintypes.DWORD()
    ok = _SendMessageTimeoutW(hwnd, msg, wp, lp, _SMTO_ABORTIFHANG, timeout, ctypes.byref(res))
    return res.value if ok else None


# 参数类型声明: lparam 需要接收 64 位指针(缓冲区地址), 默认 c_int 会 OverflowError
_SendMessageTimeoutW = ctypes.windll.user32.SendMessageTimeoutW
_SendMessageTimeoutW.argtypes = [ctypes.wintypes.HWND, ctypes.c_uint, ctypes.wintypes.WPARAM,
                                 ctypes.wintypes.LPARAM, ctypes.c_uint, ctypes.c_uint,
                                 ctypes.POINTER(ctypes.wintypes.DWORD)]
_SendMessageTimeoutW.restype = ctypes.wintypes.LPARAM


def _win32_focus_edit():
    """前台线程的焦点 HWND, 仅接受类名含 edit 的控件; 不是则返回 None"""
    try:
        gti = _GUITHREADINFO()
        gti.cbSize = ctypes.sizeof(_GUITHREADINFO)
        fg = ctypes.windll.user32.GetForegroundWindow()
        if not fg:
            return None
        tid = ctypes.windll.user32.GetWindowThreadProcessId(fg, None)
        if not ctypes.windll.user32.GetGUIThreadInfo(tid, ctypes.byref(gti)) or not gti.hwndFocus:
            return None
        buf = ctypes.create_unicode_buffer(64)
        ctypes.windll.user32.GetClassNameW(gti.hwndFocus, buf, 64)
        if "edit" not in buf.value.lower():
            return None
        return gti.hwndFocus
    except Exception:
        return None


def _win32_readback():
    """直读前台焦点 Edit/RichEdit 全文 + 光标 (WM_GETTEXT + EM_GETSEL)。
    成功返回 (hwnd, text_raw, caret_norm_u16); caret 已按实测 EM 坐标空间换算成
    规范化文本(\\r\\n -> \\n)中的 utf16 偏移, 调用方直接入账, 无需再转换"""
    hwnd = _win32_focus_edit()
    if not hwnd:
        return None
    sel = _w32_send(hwnd, _EM_GETSEL)
    if sel is None:
        return None
    caret = sel & 0xFFFF                                  # LOWORD = 选区起点(<64K 封顶, 镜像上限 30000 内安全)
    n = _w32_send(hwnd, _WM_GETTEXTLENGTH)
    if n is None:
        return None
    # WM_GETTEXTLENGTH 偶发少报(RichEditD2DPT 实测差 1): 缓冲留余量, 以空终止符为准
    buf = ctypes.create_unicode_buffer(min(n + 64, 61064))
    _w32_send(hwnd, _WM_GETTEXT, len(buf) - 1, ctypes.addressof(buf))
    text = buf.value
    kind = _em_space_kind(hwnd, _u16_len(text), _u16_len(_norm_newlines(text)[0]))
    caret_norm = _em_sel_to_norm_u16(text, caret, kind)
    if caret_norm is None:
        caret_norm = caret      # 换算异常时保守回退原读数(调用方有越界校验)
    return hwnd, text, caret_norm


def _win32_set_caret(hwnd, target_u16):
    """EM_SETSEL 直设光标 + EM_SCROLLCARET 滚动到位; 毫秒级且精确。失败返回 False"""
    if _w32_send(hwnd, _EM_SETSEL, int(target_u16), int(target_u16)) is None:
        return False
    _w32_send(hwnd, _EM_SCROLLCARET)
    return True


def _win32_ensure_caret_visible(hwnd, em_pos):
    """确保 EM 光标行在视口内: Win11 记事本对外部 EM_SCROLLCARET 可能不滚动,
    光标落到视口外 = 用户看到的"光标消失"。用 EM_LINEFROMCHAR + EM_GETFIRSTVISIBLELINE
    判定, 需要时再 EM_LINESCROLL 精确滚动; 失败静默(不影响定位正确性)。"""
    try:
        line = _w32_send(hwnd, _EM_LINEFROMCHAR, int(em_pos))
        first = _w32_send(hwnd, _EM_GETFIRSTVISIBLELINE)
        if line is None or first is None:
            return
        cli = ctypes.wintypes.RECT()
        if not ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(cli)):
            return
        vis = max(1, (cli.bottom - cli.top) // 20)   # 保守估算可见行数(宁多滚不少滚)
        if line < first or line >= first + vis:
            _w32_send(hwnd, _EM_SCROLLCARET)
            first2 = _w32_send(hwnd, _EM_GETFIRSTVISIBLELINE)
            if first2 is None:
                return
            if line < first2 or line >= first2 + vis:
                _w32_send(hwnd, _EM_LINESCROLL, 0, line - first2 - max(vis // 2 - 1, 0))
    except Exception:
        pass


_CHROMIUM_WIN_MARK = "chrome_widgetwin"   # Chrome/Edge/Electron(含 DSH)窗口类名特征


def _fg_is_chromium() -> bool:
    """前台窗口是否 Chromium 系: UIA TextPattern 实测不可靠(光标读数僵死/设置超时/
    Select 扰动 DOM 选区=光标消失), 一律绕开 UIA 光标操作, 走 Ctrl+Home 锚定 + 箭头"""
    info = _fg_info()
    return bool(info) and _CHROMIUM_WIN_MARK in str(info[1]).lower()


def _srv_move(target: int, absolute: bool = False) -> None:
    """把电脑光标移到镜像坐标 target(字形簇单位)。
    Chromium/Electron 系(Chrome/Edge/DSH): 不碰 UIA —— 纯定位用 Ctrl+Home 文档头锚定
    + 箭头(Chromium 箭头按字形簇移动, 与协议坐标一致, 精确且毫秒级); 编辑走账目差值箭头。
    Edit/RichEdit 系优先 Win32 直控(EM_SETSEL); 可读应用(_SRV_BLIND=False)走 UIA TextPattern
    直设光标; 失败才退回箭头。盲区应用: 纯定位(absolute)从字段两端锚定。
    直设前必须通过"焦点控件文本 == 镜像"一致性校验: 不一致 = PC 端有框外改动或焦点已换框,
    此时直设会错位/把光标写进别的框(消失假象) -> 先对账并放弃本次移动。"""
    global _SRV_BUF, _SRV_CARET, _SRV_BLIND, _SRV_LAST_MOVE_TS, _SRV_MISMATCH, _SRV_MISMATCH_LOG_TS
    _SRV_MISMATCH = False
    target = int(target)
    d = target - _SRV_CARET
    if d == 0 and not absolute:
        return
    if _fg_is_chromium():
        if absolute:
            if target > 800:
                log(f"srv_move: chromium 箭头列车过长({target}), 只记账不移动")
                _SRV_CARET = target
                return
            _srv_anchor_head()              # Ctrl+Home 字段头 + Left 归零
            _srv_tap("right", target)       # 箭头按字形簇移动, 精确落位
        else:
            if d > 0:
                _srv_tap("right", d)
            elif d < 0:
                _srv_tap("left", -d)
        _SRV_CARET = target
        _SRV_LAST_MOVE_TS = time.time()
        return
    if not _SRV_FULL and not _SRV_BUF and not _fg_is_chromium():
        # 镜像未建立(服务刚起/纯 API 定位): 先校准一次(记事本=Win32 毫秒级)。
        # 校准成功后走精确直设, 避免按错误账目发长箭头列车(列车慢且会被端点钳住);
        # 校准失败则退回 Ctrl+Home 锚定 + 原始 at 箭头兜底。
        _mirror_calibrate_now()
    if absolute and not _SRV_BUF:
        # 无镜像(服务刚起/未读回/盲区): Ctrl+Home 锚定 + 原始 at 箭头在真实文本上精确落位。
        # 不能按 d=at-_SRV_CARET(0) 发箭头 —— 实际光标不在 0, 列车会被端点钳住(点击失效)。
        if target > 800:
            log(f"srv_move: 无镜像箭头列车过长({target}), 只记账不移动")
            _SRV_CARET = target
            return
        _srv_anchor_head()
        _srv_tap("right", target)
        _SRV_CARET = target
        _SRV_LAST_MOVE_TS = time.time()
        return
    # 纯定位即使账目 d==0 也直设一次: 同位置重复点击可纠正电脑端被外部移动的实际光标。
    # Win32 直控优先(Edit/RichEdit 系): EM_SETSEL 毫秒级精确落位, 无超时/符号/单位歧义
    if _SRV_FULL:
        w32 = _win32_readback()
        if w32 is not None:
            hwnd, text_raw, _c = w32
            text_n, _ = _norm_newlines(text_raw)
            if text_n != _SRV_BUF:
                # _win32_readback 已按实测 EM 空间把光标换算为规范化 utf16, 直接入账
                _SRV_BUF = text_n
                _SRV_CARET = _g_u16_index(text_n, _c) if _c is not None else _g_len(text_n)
                _SRV_BLIND = False
                _SRV_MISMATCH = True   # 调用方(stream_input)据此改为"按真实光标就近应用"
                if time.time() - _SRV_MISMATCH_LOG_TS > 3.0:
                    log("srv_move: 焦点文本与镜像不一致, 已对账")
                    _SRV_MISMATCH_LOG_TS = time.time()
                if absolute:
                    # 纯定位: 对账后镜像=真实现状, 绝对移动不动文本、绝对安全。
                    # 绝不能放弃移动 —— 放弃 = "点击没反应/光标不动"的直接来源。
                    target2 = max(0, min(target, _g_len(_SRV_BUF)))
                    kind = _em_space_kind(hwnd, _u16_len(text_raw), _u16_len(text_n))
                    t2 = _norm_u16_to_em(text_raw, _g_u16_offset(_SRV_BUF, target2), kind)
                    if _win32_set_caret(hwnd, t2):
                        _SRV_CARET = target2
                        _SRV_LAST_MOVE_TS = time.time()
                        _win32_ensure_caret_visible(hwnd, t2)
                return
            kind = _em_space_kind(hwnd, _u16_len(text_raw), _u16_len(text_n))
            target_u16 = _norm_u16_to_em(text_raw, _g_u16_offset(_SRV_BUF, target), kind)
            if _win32_set_caret(hwnd, target_u16):
                # 落点对账: EM_GETSEL 与设置值理论上 1:1, 不等 = 探测空间失真。
                # 作废缓存重探 + 立即按真实现状重建镜像, 绝不让错误换算污染账本。
                sel = _w32_send(hwnd, _EM_GETSEL)
                got_u16 = _em_sel_to_norm_u16(text_raw, sel & 0xFFFF, kind) if sel is not None else None
                got_g = _g_u16_index(text_n, got_u16) if got_u16 is not None else target
                if got_g != target:
                    log(f"srv_move: EM 落点对账({got_g} != 目标{target}), 作废空间缓存重探")
                    _EM_SPACE.pop(hwnd, None)
                    _SRV_MISMATCH = True
                    if not _mirror_calibrate_now():
                        _SRV_CARET = None
                else:
                    _SRV_CARET = target
                    _SRV_LAST_MOVE_TS = time.time()
                    _win32_ensure_caret_visible(hwnd, target_u16)
                return
        if not _SRV_BLIND:
            got = _uia_caret_set(target, _SRV_BUF)
            if _SRV_MISMATCH:
                # UIA 发现焦点原文已变: 先按真实现状重建镜像; 校准失败则取消本次操作,
                # 绝不能继续用旧镜像坐标发箭头或编辑新焦点。
                if not _mirror_calibrate_now():
                    _SRV_CARET = None
                return
            if got is not None:
                _SRV_CARET = got
                _SRV_LAST_MOVE_TS = time.time()
                return
    if _SRV_FULL and ((_SRV_BLIND and absolute) or abs(d) > 80):
        buf_len = _g_len(_SRV_BUF)
        if target <= buf_len - target:       # 目标更靠近字段头: Ctrl+Home 起步右移
            if target > 800:
                log(f"srv_move: 箭头列车过长({target}), 只记账不移动(盲区长文档)")
                _SRV_CARET = target
                return
            _srv_anchor_head()
            _srv_tap("right", target)
        else:                                # 更靠近字段尾: Ctrl+End 起步左移
            train = buf_len - target
            if train > 800:
                log(f"srv_move: 箭头列车过长({train}), 只记账不移动(盲区长文档)")
                _SRV_CARET = target
                return
            _srv_doc_anchor(True)
            _srv_tap("left", train)
        _SRV_CARET = target
        got = _uia_caret_now()               # 可读应用: 读回真实光标对账; 盲区无此通道
        if got is not None:
            _SRV_CARET = got
        return
    if d > 0:
        _srv_tap("right", d)
    elif d < 0:
        _srv_tap("left", -d)
    _SRV_CARET = target
    _SRV_LAST_MOVE_TS = time.time()


def _uia_caret_now() -> "int | None":
    """UIA 读当前焦点光标绝对位置 -> 字形簇索引。需在已持 _SRV_LOCK 的调用方里用
    (内部不再取锁, 也不改镜像文本); 读不到返回 None; UIA 熔断冷却中立即返回 None"""
    if not HAS_UIA or not _uia_ready() or _fg_is_chromium():
        return None
    info = _fg_info()
    if info is None:
        return None
    try:
        r = _uia_call(_uia_focus_text_read_impl, info[0], timeout=2.0)
    except Exception:
        return None
    if not r or r.get("text") is None or not isinstance(r.get("caret"), int):
        return None
    text, _ = _norm_newlines(r["text"])
    u16 = _caret_norm_u16(r["text"], r["caret"])
    if u16 is None:
        return None
    return _g_u16_index(text, u16)


def _uia_caret_set_impl(win_rect, target_u16, expected_n):
    """UIA TextPattern 直设光标(可读应用定位主通道): Clone 文档收拢到头 -> Move -> Select,
    再读回实际偏移微调对齐(最多 3 轮)。免箭头逐字跑, 无方向键被吃/应用重渲染问题。
    返回 {"caret": 最终偏移(utf16), "text": 原文(规范化用)} 或 {"reason"}; UIA 专职线程内"""
    focused = _UIA_AUTO.GetFocusedControl()
    if focused is None:
        return {"reason": "no-focus"}
    try:
        c = _candidate_from(focused, win_rect, learn=True)
    except Exception:
        c = None
    if not c:
        c = _descend_to_edit(focused, win_rect)
        if not c:
            return {"reason": "gate"}
    try:
        tp = c["ctrl"].GetTextPattern()
        doc = tp.DocumentRange
        EP = _UIA_AUTO.TextPatternRangeEndpoint
        try:
            TU = _UIA_AUTO.TextUnit.Character
        except Exception:
            TU = 1
        raw = None
        try:
            raw = doc.GetText(61001)      # 规范化 caret 偏移用(\r\n 折算)
        except Exception:
            pass
        if isinstance(raw, str) and _norm_newlines(raw)[0] != expected_n:
            # 焦点已换框或 PC 端有框外改文: 不碰选区, 交给外层重建真实镜像。
            return {"reason": "text-mismatch", "text": raw}
        target_raw_u16 = _caret_raw_u16(raw, target_u16) if isinstance(raw, str) else target_u16
        rng = doc.Clone()
        rng.MoveEndpointByRange(EP.Start, doc, EP.Start)   # Start 压到文档头
        rng.MoveEndpointByRange(EP.End, rng, EP.Start)     # 关键: End 对齐 Start 收拢成退化区间,
        rng.Move(TU, int(target_raw_u16))                  # 否则 Move 只挪 Start、End 钳在文档尾,
        try:                                               # Select 会选中 target..末尾 一大段 (2026-11-25 实证)
            rng.Select()
        except Exception:
            return {"reason": "no-select"}
        off = None
        for attempt in range(4):             # 最多 3 次调整 + 最后 1 次读回, 返回永远是已落地的位置
            sel = tp.GetSelection()
            if not sel:
                return {"reason": "no-sel"}
            # 残留选中态保险: 选区非退化(Start≠End)先收拢到 Start 再校验,
            # 否则选中态下退格/输入会整段替换选区(数据风险), 方向键语义也变
            try:
                if sel[0].CompareEndpoints(EP.Start, sel[0], EP.End) != 0:
                    sel[0].MoveEndpointByRange(EP.End, sel[0], EP.Start)
                    sel[0].Select()
                    time.sleep(0.05)
            except Exception:
                pass
            probe = sel[0].Clone()
            probe.MoveEndpointByRange(EP.Start, doc, EP.Start)
            # 符号约定统一取绝对值(RichEdit 系返回 -(偏移)): 否则 diff 反向、Move 发散
            off = abs(probe.CompareEndpoints(EP.Start, sel[0], EP.Start))
            diff = int(target_raw_u16) - off
            if diff == 0 or attempt == 3:
                break
            adj = sel[0].Clone()                       # 从当前光标位置微调差值
            adj.MoveEndpointByRange(EP.Start, sel[0], EP.Start)
            adj.MoveEndpointByRange(EP.End, sel[0], EP.Start)
            adj.Move(TU, diff)
            try:
                adj.Select()
            except Exception:
                break
        return {"caret": off, "text": raw}
    except Exception as e:
        return {"reason": f"err:{e}"}


def _uia_caret_set(target_g: int, text_n: str) -> "int | None":
    """UIA 直设光标到镜像字形簇坐标 target_g; 成功返回实际簇索引(账目直接对齐),
    失败返回 None(调用方退回箭头)。文本不一致时标记 _SRV_MISMATCH, 禁止旧坐标继续移动。
    需在已持 _SRV_LOCK 的调用方里用(内部不取锁)"""
    global _SRV_MISMATCH
    if not HAS_UIA or not _uia_ready() or _fg_is_chromium():
        return None
    info = _fg_info()
    if info is None:
        return None
    target_g = max(0, min(int(target_g), _g_len(text_n)))
    want_u16 = _g_u16_offset(text_n, target_g)
    try:
        r = _uia_call(_uia_caret_set_impl, info[0], want_u16, text_n, timeout=2.0)   # 快速失败退箭头, 不拖死全局 UIA 队列
    except Exception:
        return None
    if r and r.get("reason") == "text-mismatch":
        _SRV_MISMATCH = True
        return None
    if not r or r.get("caret") is None or not isinstance(r.get("caret"), int):
        return None
    u16 = r["caret"]
    raw = r.get("text")
    if isinstance(raw, str):
        u16n = _caret_norm_u16(raw, u16)
        if u16n is not None:
            u16 = u16n
    return _g_u16_index(text_n, u16)


def _mirror_calibrate_now() -> bool:
    """UIA 读当前焦点全文+光标重建整框镜像(失明后的首条锚定先走这里对齐基准)。
    需在已持 _SRV_LOCK 的调用方里用(内部不再取锁); 读不到返回 False, 调用方退回就近应用"""
    global _SRV_BUF, _SRV_CARET, _SRV_FULL, _SRV_BLIND
    # Win32 直控优先(Edit/RichEdit 系)
    w32 = _win32_readback()
    if w32 is not None:
        _hwnd, text_raw, raw_caret = w32
        text, _ = _norm_newlines(text_raw)
        if len(text) <= 30000:
            # _win32_readback 已返回规范化 utf16 光标, 直接入账
            u16 = raw_caret
            _SRV_BUF = text
            _SRV_FULL = True
            _SRV_BLIND = False
            _SRV_CARET = _g_u16_index(text, u16) if u16 is not None else _g_len(text)
            log(f"mirror calibrate: win32 ok({len(text)}字, caret={_SRV_CARET})")
            return True
    if not HAS_UIA or not _uia_ready() or _fg_is_chromium():
        _SRV_BLIND = True
        return False
    info = _fg_info()
    if info is None:
        return False
    try:
        r = _uia_call(_uia_focus_text_read_impl, info[0], timeout=2.0)
    except Exception:
        _SRV_BLIND = True
        return False
    if not r or r.get("text") is None:
        _SRV_BLIND = True
        return False
    text, _ = _norm_newlines(r["text"])
    if len(text) > 30000:
        return False
    u16 = _caret_norm_u16(r["text"], r.get("caret"))
    _SRV_BUF = text
    _SRV_FULL = True
    _SRV_BLIND = False
    _SRV_CARET = _g_u16_index(text, u16) if u16 is not None else _g_len(text)
    log(f"mirror calibrate: ok({len(text)}字, caret={_SRV_CARET})")
    return True


def stream_input(data: dict) -> str:
    """实时流输入协议:
      {at, del, ins}  锚定编辑: 光标对齐镜像 at -> 退格xN -> 注入(手机框有镜像文字时)
      {at}            纯光标定位: 点手机文字移动电脑光标
      {del, ins}      相对编辑: 原点跟随电脑当前光标(手机框为空/旧客户端)
      {key, n}        相对按键: 空框滑动方向键 / 空框退格 / 回车 / 导航键
      {reset}         镜像归零(回车发送后聊天框已自清, 手机框同步清空)
    """
    global _SRV_BUF, _SRV_CARET, _SRV_FULL
    key = data.get("key")
    n_key = max(1, min(int(data.get("n") or 1), 300))
    n_del = int(data.get("del") or 0)
    ins = str(data.get("ins") or "")
    at = data.get("at")
    parts = []
    if data.get("reset"):
        parts.append("镜像归零")
    if at is not None:
        parts.append(f"锚点at={at}")
    if n_del:
        parts.append(f"退格x{n_del}")
    if ins:
        parts.append(f"注入({len(ins)}字): {ins[:30]}")
    if key:
        parts.append(f"键:{key}x{n_key}")
    if not parts:
        return "ok"
    desc = " ".join(parts)
    if DRY:
        log(f"DRYRUN 实时输入 {desc}")
        return "ok"
    _ime_ensure_english()   # 手机流输入期间电脑输入法切英文直通, 停止后自动恢复
    with _SRV_LOCK:
        if data.get("reset"):
            _SRV_BUF = ""
            _SRV_CARET = 0
            _SRV_FULL = False
        if at is not None:
            if _SRV_CARET is None and _mirror_calibrate_now():
                pass                     # 校准成功: 电脑端全文+光标已知, 按正常锚定走
            if _SRV_CARET is None:
                if not n_del and not ins:
                    # 失明下的纯定位: 字段头锚定(Ctrl+Home) + 箭头仍可精确落位, 不动文本。
                    # 之前"什么都不做"会让盲区应用里点击完全失效(点了没反应)。
                    at_n = max(0, min(int(at), 800))
                    _srv_anchor_head()
                    _srv_tap("right", at_n)
                    _SRV_CARET = at_n
                    log(f"stream_input: 失明纯定位 锚定字段头+右移{at_n}")
                    return "ok"
                # 失明且校准失败(UIA 盲区): 无基准不发方向键, 按当前电脑光标就近应用并重建镜像
                for _ in range(n_del):
                    _KB.press(KbKey.backspace)
                    _KB.release(KbKey.backspace)
                if ins:
                    _KB.type(ins)
                _SRV_BUF = ins
                _SRV_CARET = _g_len(ins)
                _SRV_FULL = False
            else:
                if _SRV_FULL or _SRV_BUF:
                    at = max(0, min(int(at), _g_len(_SRV_BUF)))
                # 无镜像时保留原始 at: 绝对定位用 Ctrl+Home 锚定在真实文本上精确落位
                # 纯光标定位({at} 无删无注入): 盲区应用走绝对锚定(不依赖账目);
                # 锚定编辑仍走账目差值(逐字重锚定太重, 漂移由下一次定位/读回纠正)
                _srv_move(at + n_del if n_del else at, absolute=not n_del and not ins)
                if _SRV_MISMATCH:
                    if not n_del and not ins:
                        # 纯定位: 对账后镜像=真实现状, 重放绝对定位(点击必须落位)。
                        # win32 路径已在 _srv_move 内部重放; 这里覆盖 UIA/盲区路径。
                        _srv_move(min(int(at), _g_len(_SRV_BUF)), absolute=True)
                        return "ok"
                    if _SRV_CARET is None:
                        # UIA 发现换框/外部改文且校准失败: 真实光标不可知, 宁可取消本次也不写错位置。
                        log("stream_input: 焦点文本不一致且校准失败, 本次编辑取消")
                        return "ok"
                    # PC 端框外改动已对账(镜像=现状): 退格/注入按真实光标就近应用 —— 保输入不丢,
                    # 落点可能与意图有一字级偏差, 由对账链自愈; 若仍用旧 at 拼镜像会永久错位
                    base = max(0, min(_SRV_CARET, _g_len(_SRV_BUF)))
                    p = max(0, base - n_del)
                else:
                    p = at
                for _ in range(n_del):
                    _KB.press(KbKey.backspace)
                    _KB.release(KbKey.backspace)
                if ins:
                    _KB.type(ins)
                _SRV_BUF = _g_splice(_SRV_BUF, p, n_del, ins)
                _SRV_CARET = p + _g_len(ins)
        else:
            # 相对模式(原点跟随电脑光标): 退格 -> 注入 -> 按键, 镜像按可建模部分记账
            for _ in range(n_del):
                _KB.press(KbKey.backspace)
                _KB.release(KbKey.backspace)
            if ins:
                _KB.type(ins)
            if key:
                _srv_tap(key, n_key)
            if _SRV_CARET is not None:
                if key == "left":
                    _SRV_CARET -= n_key          # 负值 = 岛左侧前缀区
                elif key == "right":
                    _SRV_CARET += n_key          # 超过 len = 岛右侧后缀区
                elif key == "backspace":
                    if _SRV_CARET > 0:
                        _SRV_CARET -= 1          # 删岛内一簇
                        _SRV_BUF = _g_splice(_SRV_BUF, _SRV_CARET, 1, "")
                    else:
                        _SRV_CARET -= 1          # 删的是镜像外前缀, 岛文本不变
                elif key in _SRV_BLIND_KEYS:
                    _SRV_BUF = ""                # 失明: 岛作废, 退化为原点跟随
                    _SRV_CARET = None
                    _SRV_FULL = False
            for _ in range(n_del if key != "backspace" else 0):   # 旧客户端相对退格
                if _SRV_CARET is None:
                    break
                if _SRV_CARET > 0:
                    _SRV_CARET -= 1
                    _SRV_BUF = _g_splice(_SRV_BUF, _SRV_CARET, 1, "")
                else:
                    _SRV_CARET -= 1
            if ins:
                if _SRV_CARET is None:
                    _SRV_BUF = ins               # 新岛: 原点=当前电脑光标
                    _SRV_CARET = _g_len(ins)
                    _SRV_FULL = False
                elif 0 <= _SRV_CARET <= _g_len(_SRV_BUF):
                    _SRV_BUF = _g_splice(_SRV_BUF, _SRV_CARET, 0, ins)
                    _SRV_CARET += _g_len(ins)
                else:
                    _SRV_CARET += _g_len(ins)    # 岛外(前缀/后缀区)注入, 岛文本不变
    log(f"实时输入 {desc}")
    return "ok"


# ---- 手机输入期间把电脑输入法切到英文直通, 停止约 2 秒后自动恢复 ----
# 第一性原理: 电脑端中文输入法开着时, 注入的回车/方向键可能被拼音组合框/候选窗吃掉。
# 流输入激活时切英文模式(等效 Shift 切换, 保留输入法), 静默后还原; KZQ_IME_ENGLISH=0 关闭。
_WM_IME_CONTROL = 0x0283
_IMC_GETCONVERSIONMODE, _IMC_SETCONVERSIONMODE = 0x0001, 0x0002
_IMC_GETOPENSTATUS = 0x0005
_IME_CMODE_NATIVE = 0x0001
_IME = {"ime": 0, "mode": 0, "active": False, "ts": 0.0}
_IME_LOCK = threading.Lock()
_IME_WATCH_STARTED = False
IME_ACT_GAP = 1.2      # 流输入静默超过此秒数, 下一条先重新切英文
IME_RESTORE_GAP = 2.0  # 流输入静默此秒数后恢复原输入法状态


def _ime_ctl(ime_hwnd: int, imc: int, wparam: int):
    """向目标线程的默认 IME 窗口发 WM_IME_CONTROL; 目标卡死 200ms 即放弃, 失败静默"""
    try:
        res = ctypes.wintypes.LRESULT()
        ok = ctypes.windll.user32.SendMessageTimeoutW(
            ime_hwnd, _WM_IME_CONTROL, imc, wparam, 0x0002, 200, ctypes.byref(res))
        return res.value if ok else 0
    except Exception:
        return 0


def _ime_activate() -> None:
    """把前台窗口的输入法切英文直通(记录原转换模式供恢复)"""
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return
        ime = ctypes.windll.imm32.ImmGetDefaultIMEWnd(hwnd)
        if not ime:
            return
        open_ = _ime_ctl(ime, _IMC_GETOPENSTATUS, 0)
        mode = _ime_ctl(ime, _IMC_GETCONVERSIONMODE, 0)
        if not open_ or not (mode & _IME_CMODE_NATIVE):
            return                  # 已是英文直通/输入法关闭: 无需干预
        _ime_ctl(ime, _IMC_SETCONVERSIONMODE, mode & ~_IME_CMODE_NATIVE)
        _IME.update(ime=ime, mode=mode, active=True)
        log("IME 切英文直通(手机输入中)")
    except Exception as e:
        log(f"IME 切换失败: {e}")


def _ime_restore() -> None:
    """恢复激活时记录的原输入法转换模式(按记录的窗口, 与当前焦点无关)"""
    if not _IME["active"]:
        return
    _ime_ctl(_IME["ime"], _IMC_SETCONVERSIONMODE, _IME["mode"])
    _IME["active"] = False
    log("IME 已恢复原模式")


def _ime_watch() -> None:
    while True:
        time.sleep(0.4)
        with _IME_LOCK:
            if _IME["active"] and time.time() - _IME["ts"] > IME_RESTORE_GAP:
                _ime_restore()


def _ime_ensure_english() -> None:
    """流输入入口: 空闲边缘切英文, 刷新活跃时间戳; 监视线程懒启动"""
    global _IME_WATCH_STARTED
    if os.environ.get("KZQ_IME_ENGLISH") == "0":
        return
    with _IME_LOCK:
        if not _IME["active"] or time.time() - _IME["ts"] > IME_ACT_GAP:
            _ime_activate()
        _IME["ts"] = time.time()
    if not _IME_WATCH_STARTED:
        _IME_WATCH_STARTED = True
        threading.Thread(target=_ime_watch, daemon=True, name="kzq-ime").start()
        atexit.register(_ime_restore)


KEY_WHITELIST = {
    "enter", "esc", "tab", "backspace", "delete", "space",
    "up", "down", "left", "right", "home", "end", "pageup", "pagedown",
    "win", "alt", "ctrl", "shift",
    "volumeup", "volumedown", "volumemute",
    "playpause", "nexttrack", "prevtrack",
} | {f"f{i}" for i in range(1, 13)}

COMBO_WHITELIST = {
    "win", "ctrl", "alt", "shift", "tab", "enter", "esc", "space",
    "up", "down", "left", "right",
    "a", "c", "v", "x", "z", "e", "w", "d", "l", "s", "p", "t", "f", "f5",
}


def press_key(key: str) -> None:
    if key not in KEY_WHITELIST:
        raise ValueError(f"按键不在白名单: {key}")
    if DRY:
        log(f"DRYRUN 按键 {key}")
    else:
        pyautogui.press(key)


def combo(keys: list) -> None:
    for k in keys:
        if k not in COMBO_WHITELIST:
            raise ValueError(f"组合键不在白名单: {k}")
    if DRY:
        log(f"DRYRUN 组合键 {'+'.join(keys)}")
    else:
        pyautogui.hotkey(*keys)


# ---------------------------------------------------------------- 系统命令
def run(args: list) -> None:
    if DRY:
        log(f"DRYRUN 命令: {args}")
    else:
        log(f"执行命令: {args}")
        subprocess.Popen(args)


def monitor_off() -> None:
    """关闭显示器, 动鼠标/按键即唤醒"""
    if DRY:
        log("DRYRUN 关闭显示器")
    else:
        log("关闭显示器")
        ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)


def do_cmd(cmd: str, data: dict = None) -> str:
    data = data or {}
    if cmd == "ping":
        return "pong"
    if cmd == "focus_input":
        return focus_input_entry(str(data.get("name") or ""))
    if cmd == "focus_read":
        return focus_read_entry(data)
    if cmd == "client_info":
        log(f"client: 版本={data.get('ver') or '?'} ua={str(data.get('ua') or '')[:44]}")
        return "ok"
    if cmd == "focus_learn":
        return focus_learn(str(data.get("name") or ""))
    if cmd == "focus_delete":
        return focus_delete(str(data.get("id") or ""))
    if cmd == "shutdown":
        run(["shutdown", "/s", "/t", "5"])
        return "5 秒后关机（可点「取消关机」）"
    if cmd == "cancel_shutdown":
        run(["shutdown", "/a"])
        return "已取消关机"
    if cmd == "restart":
        run(["shutdown", "/r", "/t", "5"])
        return "5 秒后重启（可点「取消关机」）"
    if cmd == "sleep":
        run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
        return "进入睡眠"
    if cmd == "lock":
        run(["rundll32.exe", "user32.dll,LockWorkStation"])
        return "已锁屏"
    if cmd == "monitor_off":
        monitor_off()
        return "屏幕已关闭"
    if cmd == "display_switch":
        run(["DisplaySwitch.exe"])
        return "打开显示器切换菜单"
    if cmd == "display_identify":
        if DRY:
            log("DRYRUN 打开显示设置(ms-settings:display)")
        else:
            log("打开显示设置(可点'标识'看屏幕编号)")
            os.startfile("ms-settings:display")
        return "打开显示设置(标识屏幕)"
    if cmd == "display_internal":
        run(["DisplaySwitch.exe", "/internal"])
        return "切换: 仅电脑屏幕"
    if cmd == "display_clone":
        run(["DisplaySwitch.exe", "/clone"])
        return "切换: 复制屏幕"
    if cmd == "display_extend":
        run(["DisplaySwitch.exe", "/extend"])
        return "切换: 扩展屏幕"
    if cmd == "display_external":
        run(["DisplaySwitch.exe", "/external"])
        return "切换: 仅第二屏幕"
    if cmd == "vol_up":
        press_key("volumeup")
        return "音量 +"
    if cmd == "vol_down":
        press_key("volumedown")
        return "音量 -"
    if cmd == "vol_mute":
        press_key("volumemute")
        return "静音切换"
    if cmd == "media_play":
        press_key("playpause")
        return "播放/暂停"
    if cmd == "media_next":
        press_key("nexttrack")
        return "下一首"
    if cmd == "media_prev":
        press_key("prevtrack")
        return "上一首"
    if cmd == "vdesk_left":
        combo(["ctrl", "win", "left"])
        return "上一个虚拟桌面"
    if cmd == "vdesk_right":
        combo(["ctrl", "win", "right"])
        return "下一个虚拟桌面"
    if cmd == "show_desktop":
        combo(["win", "d"])
        return "显示桌面"
    if cmd == "remote_on":
        target = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        if not target or not _fg_info():
            raise ValueError("取不到前台窗口")
        info = _win_info(target)
        # 首次开启也走同一套引擎: 浏览器优先 CDP 语义层, 否则 UIA
        res = nav_engine().enter(target, url="", title=(info[2] if info else ""),
                                 exe=(info[3] if info else ""))
        if not res.get("ok"):
            raise ValueError(res.get("msg") or "遥控模式开启失败：请再按一次重试")
        _HL_TRACK["on"] = True
        _HL_TRACK["rect"] = None
        rect = _rect4(nav_engine().current_rect)
        if rect:
            FOCUS_BOX.show(rect)   # 开启即有选中框, 随焦点移动
        return res.get("msg") or "遥控模式开"
    if cmd == "remote_off":
        _HL_TRACK["on"] = False
        _HL_TRACK["rect"] = None
        FOCUS_BOX.hide()
        # 完全重置: target 残留会让下一次懒激活跳过、导航打到看不见的旧窗口
        _nav_reset()
        _SPATIAL_CACHE["target"] = None
        _SPATIAL_CACHE["hwnd"] = None
        _SPATIAL_CACHE["focus"] = None
        _SPATIAL_CACHE["popups"] = []
        _SPATIAL_CACHE["stale"] = False
        return "遥控模式关"
    raise ValueError(f"未知命令: {cmd}")


# ---------------------------------------------------------------- 自定义按钮
def load_customs() -> list:
    """读 custom.json: [{"name":.., "type":"run|combo|url", "value":..}, ...] 每次现读, 改完即生效"""
    try:
        with open(CUSTOM_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception as e:
        log(f"custom.json 解析失败: {e}")
        return []


CUSTOM_KEY_RE = re.compile(
    r"^(ctrl|alt|shift|win|left|right|up|down|enter|esc|tab|space|backspace|[a-z0-9]|f(?:[1-9]|1[0-2]))$")


def custom_combo(spec: str) -> None:
    keys = [k.strip().lower() for k in str(spec).split("+") if k.strip()]
    if not keys:
        raise ValueError("自定义组合键为空")
    for k in keys:
        if not CUSTOM_KEY_RE.match(k):
            raise ValueError(f"自定义组合键不合法: {k}")
    if DRY:
        log(f"DRYRUN 自定义组合键 {'+'.join(keys)}")
    else:
        pyautogui.hotkey(*keys)
        log(f"自定义组合键 {'+'.join(keys)}")


def run_custom(name: str) -> str:
    for item in load_customs():
        if item.get("name") == name:
            typ = str(item.get("type") or "run")
            val = str(item.get("value") or "")
            if typ == "combo":
                custom_combo(val)
                return f"{name}: 组合键 {val}"
            if typ == "url":
                if DRY:
                    log(f"DRYRUN 打开网页 {val}")
                else:
                    log(f"打开网页 {val}")
                    os.startfile(val)
                return f"{name}: 已打开网页"
            if DRY:
                log(f"DRYRUN 运行 {val}")
            else:
                log(f"运行 {val}")
                subprocess.Popen(val, shell=True)
            return f"{name}: 已启动"
    raise ValueError(f"自定义命令不存在: {name}")


# ---------------------------------------------------------------- 聚焦输入框
# 点击手机输入框 -> 电脑自动聚焦目标输入框。四层兜底(按序尝试, 命中即停):
#   1) 语义档案回放: 「📍 记录位置」学习当前焦点控件的语义特征(automation_id/类名/名称/归一化矩形),
#      在前台窗口内匹配(阈值 180 + 与第二名分差 40, control_type 必须一致);
#   2) 记忆位置: 档案同时存点击位置(水平比例+垂直边距), 语义失效或 UIA 盲区应用(微信 4.x)时回放;
#   3) UIA 通用打分 + 相邻动作语义: 词表三档 + 几何 + 已有焦点 +40 + 发送/搜索按钮同行 +60;
#   4) 兜底: 鼠标当前位置点击(旧行为)。
# 档案身份 = 进程 exe + 站点(origin, 浏览器从地址栏读取) + 窗口类名; 不用窗口标题——
#   标题是意图上下文的坏代理(换对话/换项目即变), 站点 origin 才跨项目窗口稳定。
# 全部 UIA 调用在专职线程(套间说明见下)。

try:
    HAS_UIA = importlib.util.find_spec("uiautomation") is not None
except Exception:
    HAS_UIA = False
if not HAS_UIA:
    log("未安装 uiautomation, UIA 聚焦层不可用 (pip install uiautomation)")

# UIA/COM 客户端绑定创建它的套间, 跨线程直接复用会挂起(实测请求线程 20s 无响应)。
# 因此 import/初始化/所有调用都固定在专职工作线程上串行执行, 请求线程只做派发与超时放弃。
_UIA_AUTO = None           # 专职线程里的 uiautomation 模块(仅该线程可触碰)
_UIA_TASKS = None          # (fn, args, result_queue) 队列, worker 启动后创建
_UIA_READY = threading.Event()
_UIA_STARTLOCK = threading.Lock()


def _uia_worker() -> None:
    global _UIA_AUTO, _UIA_TASKS
    try:
        # comtypes 导入时会自行 CoInitializeEx; 用 sys.coinit_flags 指定 MTA
        # (UIA 官方建议 MTA; 手动先初始化会与 comtypes 的导入期初始化冲突抛 RPC_E_CHANGED_MODE)
        sys.coinit_flags = 0  # COINIT_MULTITHREADED
        import uiautomation as auto_mod  # 导入与 COM 客户端创建都发生在专职线程
        _UIA_AUTO = auto_mod
    except Exception as e:
        log(f"uiautomation 初始化失败: {e}")
        _UIA_AUTO = None
    _UIA_TASKS = queue.Queue()
    _UIA_READY.set()
    while True:
        try:
            fn, args, rq = _UIA_TASKS.get(timeout=0.12)
        except queue.Empty:
            if _HL_TRACK["on"] and _UIA_AUTO is not None:
                _hl_tick()   # 遥控高亮追踪: 只占 UIA 线程空闲期, 请求永远优先
            continue
        if fn is None:
            break
        try:
            rq.put((True, fn(*args)))
        except Exception as e:
            rq.put((False, e))


# ---- 焦点追踪节拍(仅 UIA worker 空闲): Tab 式选中框跟随 + 导航起点缓存刷新 ----
_HL_TRACK = {"on": False, "rect": None}   # 遥控焦点追踪开关(remote_on/off 切换)


def _hl_tick() -> None:
    try:
        focused = _UIA_AUTO.GetFocusedControl()
        if focused is None:
            return
        r = focused.BoundingRectangle
        rect = (r.left, r.top, r.right, r.bottom)
        if rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
            return
        trect = _SPATIAL_CACHE.get("target_rect")
        if trect and (rect[2] <= trect[0] or rect[0] >= trect[2]
                      or rect[3] <= trect[1] or rect[1] >= trect[3]):
            return   # 焦点跑到遥控目标窗口之外: 框不跟随、起点缓存不被污染
        crect = _SPATIAL_CACHE.get("content_rect")
        if crect and (rect[2] <= crect[0] or rect[0] >= crect[2]
                      or rect[3] <= crect[1] or rect[1] >= crect[3]):
            return   # 焦点进了浏览器地址栏/标签栏(chrome UI): 视作未变化,
                     # 起点缓存保持最后的内容元素——Tab 出去再回来导航平滑
        if _HL_TRACK["rect"] != rect:
            _HL_TRACK["rect"] = rect
            FOCUS_BOX.show(rect)   # Tab 式选中框跟随焦点
        try:
            rid = list(focused.GetRuntimeId() or [])
        except Exception:
            rid = None
        fok = _SPATIAL_CACHE.get("focus")
        if fok and fok[1] and fok[2] is not None and rid is not None and fok[2] == rid:
            dx, dy = rect[0] - fok[1][0], rect[1] - fok[1][1]
            if abs(dx) >= 80 or abs(dy) >= 80:
                # 大幅位移 = 虚拟列表回收/整页跳转: 平移不可信, 标记缓存过期待重扫
                _SPATIAL_CACHE["stale"] = True
            elif abs(dx) >= 2 or abs(dy) >= 2:
                # 小幅位移 = 页面/容器滚动: 候选缓存整体平移对齐(零额外查询)
                _SPATIAL_CACHE["items"] = _spatial_shift_cache(_SPATIAL_CACHE["items"], dx, dy)
        _SPATIAL_CACHE["focus"] = (focused, rect, rid)
    except Exception:
        pass


_UIA_COOLDOWN_UNTIL = 0.0   # UIA 熔断: 连续超时后冷却期内直接跳过, 不再逐次等满 timeout 卡住定位/打字
_UIA_COOLDOWN_FAILS = 0
_UIA_COOLDOWN_LOG_TS = 0.0


def _uia_ready() -> bool:
    """UIA 熔断是否解除(冷却期外)"""
    return time.time() >= _UIA_COOLDOWN_UNTIL


def _uia_call(fn, *args, timeout: float = 15.0):
    """派发 UIA 调用到专职线程; 超时/异常返回 None, 请求线程不被卡死。
    连续超时进入冷却: 后续调用立即返回 None(调用方走快速兜底), 冷却到期自动恢复试探"""
    global _UIA_TASKS, _UIA_COOLDOWN_UNTIL, _UIA_COOLDOWN_FAILS, _UIA_COOLDOWN_LOG_TS
    if not HAS_UIA:
        return None
    if not _uia_ready():
        return None
    if _UIA_TASKS is None:
        with _UIA_STARTLOCK:
            if _UIA_TASKS is None:
                threading.Thread(target=_uia_worker, daemon=True, name="kzq-uia").start()
                if not _UIA_READY.wait(timeout=5):
                    return None
    rq = queue.Queue(1)
    _UIA_TASKS.put((fn, args, rq))
    try:
        ok, val = rq.get(timeout=timeout)
    except queue.Empty:
        _UIA_COOLDOWN_FAILS += 1
        if _UIA_COOLDOWN_FAILS >= 2:
            _UIA_COOLDOWN_UNTIL = time.time() + 5.0
            if time.time() - _UIA_COOLDOWN_LOG_TS > 3.0:
                log("UIA 连续超时, 熔断 5 秒(期间走 Win32/箭头快速通道)")
                _UIA_COOLDOWN_LOG_TS = time.time()
        else:
            log("UIA 调用超时, 放弃本轮聚焦")
        return None
    _UIA_COOLDOWN_FAILS = 0
    if not ok:
        log(f"UIA 调用异常: {val}")
        return None
    return val


FOCUS_POS_FILE = BASE / "focus_pos.json"           # 旧版通用位置(仅迁移读取)
FOCUS_PROFILES_FILE = BASE / "focus_profiles.json"  # 学习档案: 语义特征 + 记忆位置, 按目标身份键

# ---- 词表与阈值: 对齐 SayAll focus.rs ----
# 敏感词: 命中即拒绝(密码/密钥类)
FOCUS_SENSITIVE_TERMS = (
    "password", "passcode", "secret", "api key", "apikey", "token",
    "credit card", "密码", "口令", "密钥", "令牌", "银行卡",
)
# 排除词: 明确不该聚焦的(命令面板/代码编辑器/重命名; 自动路径用, 学习路径放宽)
FOCUS_EXCLUDED_TERMS = (
    "command palette", "code editor", "monaco",
    "rename", "title", "命令面板", "代码编辑器", "重命名",
)
# 任何路径都排除(含学习): 地址栏不是手机打字的目标——误采会生成
# "对全部浏览器窗口生效"的垃圾档案(2026-11-22 实测: 焦点总被拉到地址栏)
FOCUS_EXCLUDED_ALWAYS_TERMS = (
    "omnibox", "address bar", "地址和搜索栏", "地址栏", "搜索或输入",
)
# 强语义词: 命中即很可能是对话/主输入框 (+120)
FOCUS_STRONG_TERMS = (
    "composer", "prompt-editor", "prompt_editor", "chat-input", "chat_input",
    "message-input", "message_input", "prompt input", "message input",
    "消息输入", "输入消息", "发送消息", "发送信息", "说点什么", "输入内容",
    "有问题尽管", "写点什么",
)
# 辅助语义词 (+70)
FOCUS_SUPPORTING_TERMS = (
    "message", "prompt", "reply", "chat", "ask anything", "提问", "回复",
    "输入框", "对话", "留言",
)
# 显式允许 (+80)
FOCUS_ALLOWED_TERMS = (
    "search", "find", "filter", "settings", "preferences", "terminal",
    "console", "shell", "xterm", "搜索", "查找", "筛选", "设置", "偏好",
    "终端", "控制台", "创建任务", "新建任务", "新建对话", "新对话",
)
# 富文本编辑器特征 (TipTap/ProseMirror/Quill/CodeMirror/Slate 的 contenteditable 在 UIA 上是 Group)
FOCUS_EDITOR_HINTS = (
    "prosemirror", "tiptap", "contenteditable", "cm-editor", "codemirror",
    "ql-editor", "slate-editor",
)
# 邻域动作语义: 输入框旁边(同一行)的按钮/图标名命中即 +60 (发送/搜索/创建图标旁的输入框)
FOCUS_CUE_TERMS = (
    "发送", "搜索", "创建", "新建", "提交", "ask",
    "send", "search", "submit", "create",
)
FOCUS_COMPOSER_THRESHOLD = 80   # 通用打分门槛 (COMPOSER_SCORE_THRESHOLD)
FOCUS_RECORDED_THRESHOLD = 180  # 档案匹配门槛 (RECORDED_SCORE_THRESHOLD)
FOCUS_RECORDED_MARGIN = 40      # 与第二名的最小分差 (RECORDED_SCORE_MARGIN)
FOCUS_RETRY_ATTEMPTS = 4        # 重试次数(SayAll 8×200ms; Python 扫描更贵, 减半保总时长)
FOCUS_RETRY_INTERVAL = 0.25
FOCUS_MAX_NODES = 600           # 单次扫描节点上限
FOCUS_BROWSER_EXES = ("msedge.exe", "chrome.exe")  # Chromium 系浏览器: 有站点(origin)身份


def _win_info(hwnd: int):
    """指定窗口的 (rect, 类名, 标题, 进程exe名) 或 None"""
    user32 = ctypes.windll.user32
    if not hwnd:
        return None
    rect = ctypes.wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    tbuf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, tbuf, 512)
    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        try:
            pbuf = ctypes.create_unicode_buffer(512)
            psize = ctypes.wintypes.DWORD(512)
            if k32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(psize)):
                exe = os.path.basename(pbuf.value)
        finally:
            k32.CloseHandle(h)
    return (rect.left, rect.top, rect.right, rect.bottom), buf.value, tbuf.value, exe


def _fg_info():
    """(rect(left,top,right,bottom), 窗口类名, 窗口标题, 进程exe名) 或 None; 学习与回放共用"""
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        return None
    return _win_info(hwnd)


_CT_NAMES = None   # {ControlType int: 名称}, 懒构建(仅专职线程触达)


def _ct_name(ctrl) -> str:
    global _CT_NAMES
    try:
        ct = int(ctrl.ControlType)
    except Exception:
        return ""
    if _CT_NAMES is None:
        CT = _UIA_AUTO.ControlType
        _CT_NAMES = {int(getattr(CT, n)): n[:-7] for n in dir(CT) if n.endswith("Control")}
    return _CT_NAMES.get(ct, str(ct))


def _sem_match(lhs, rhs) -> bool:
    """语义特征是否同一 (SayAll semantic_values_match: 相等, 或长度>=4 且互相包含)"""
    lhs = (lhs or "").strip().lower()
    rhs = (rhs or "").strip().lower()
    if not lhs or not rhs:
        return False
    if lhs == rhs:
        return True
    return min(len(lhs), len(rhs)) >= 4 and (lhs in rhs or rhs in lhs)


def _candidate_from(ctrl, win_rect, learn: bool = False, allow_always: bool = False,
                    intent: bool = False, trace=None):
    """探测 UIA 控件并过门槛 (对齐 SayAll passes_hard_gate / passes_learning_gate);
    learn=True 时排除词与类型要求放宽(用户主动点击是明确意图);
    allow_always=True 时解除地址栏永远排除(联动层专用: 直接几何意图 ≠ 档案语义,
    地址栏垃圾档案问题只影响学习/回放层);
    intent=True 时跳过 可用/可聚焦/离屏 检查(联动层专用: 物理点击即意图, 且部分
    WebView2 桥对网页元素一律报 IsKeyboardFocusable=False, 该属性在联动层不可信);
    trace 给列表时记录每个拒绝步骤的原因(联动诊断用)。返回候选 dict 或 None"""
    def _reject(reason):
        if trace is not None:
            trace.append(reason)
        return None
    try:
        ct = _ct_name(ctrl)
        # 联动层额外接受 ComboBox: X/ChatGPT 等现代搜索框是 role=combobox,
        # Chromium 暴露成 ComboBoxControl 而非 EditControl
        if ct not in ("Edit", "Document", "Group") and not (intent and ct == "ComboBox"):
            return _reject(f"类型={ct}")
        if not intent and (not (ctrl.IsEnabled and ctrl.IsKeyboardFocusable) or ctrl.IsOffscreen):
            return _reject("不可用/不可聚焦/离屏")
        if getattr(ctrl, "IsPassword", False):
            return _reject("密码框")
        try:
            ro = ctrl.GetPropertyValue(30046)  # UIA_IsReadOnlyPropertyId
            ro = bool(ro) if ro is not None else None
        except Exception:
            ro = None
        if ro is True:
            # Chromium 把网页根暴露成 IsReadOnly=true 的 Document, 聚焦它只会抢走真输入框
            return _reject("IsReadOnly=true")
        aid = (ctrl.AutomationId or "").strip()
        cls = (ctrl.ClassName or "").strip()
        name = (ctrl.Name or "").strip()
        try:
            focused = bool(ctrl.HasKeyboardFocus)
        except Exception:
            focused = False
        try:
            rect = ctrl.BoundingRectangle
        except Exception:
            rect = None
    except Exception as e:
        return _reject(f"属性读取异常 {e}")
    semantic = f"{aid} {cls} {name}".lower()
    if any(w in semantic for w in FOCUS_SENSITIVE_TERMS):
        return _reject("敏感词")
    if not allow_always and any(w in semantic for w in FOCUS_EXCLUDED_ALWAYS_TERMS):
        return _reject("地址栏永远排除")
    if not learn and any(w in semantic for w in FOCUS_EXCLUDED_TERMS):
        return _reject("排除词")
    has_tp = False
    try:
        has_tp = bool(ctrl.GetPropertyValue(_UIA_AUTO.PropertyId.IsTextPatternAvailableProperty))
    except Exception:
        pass
    if not learn and not (ct in ("Edit", "Document") or has_tp
                          or any(h in semantic for h in FOCUS_EDITOR_HINTS)):
        return _reject("类型要求")
    norm = None
    abs_rect = None
    if win_rect and rect is not None:
        try:
            l, t, r, b = win_rect
            ww = max(1.0, float(r - l)); wh = max(1.0, float(b - t))
            norm = {"x": (rect.left - l) / ww, "y": (rect.top - t) / wh,
                    "w": rect.width() / ww, "h": rect.height() / wh}
            norm_area = norm["w"] * norm["h"]
            abs_rect = (rect.left, rect.top, rect.width(), rect.height())
        except Exception:
            norm = None
    else:
        norm_area = None
    # 网页根节点不是可输入目标: Chromium/Edge 部分无障碍模式下它甚至是唯一可见节点,
    # 学习/聚焦到它会出现 toast「已聚焦」但打字进不了输入框 (2026-11-21 现场档案实证)。
    # 例外: RichEdit 系(Win11 记事本 RichEditD2DPT / RICHEDIT50W)的整窗 Document 是真编辑器,
    # 误杀会导致聚焦五层全 miss + 误判盲区 (2026-11-25 记事本探针实证)
    if aid.lower() == "rootwebarea" or (ct == "Document" and norm_area is not None and norm_area > 0.85
                                        and "richedit" not in cls.lower()):
        return _reject("网页根/整页Document")
    if rect is not None and (rect.width() <= 0 or rect.height() <= 0):
        return _reject("零尺寸")
    return {"ctrl": ctrl, "ct": ct, "aid": aid.lower(), "cls": cls.lower(),
            "name": name, "norm": norm, "rect": abs_rect, "focused": focused}


def _composer_score(c) -> int:
    """通用打分原始分 (对齐 SayAll composer_candidate_score; 阈值在候选选择阶段应用)"""
    semantic = f"{c['aid']} {c['cls']} {c['name'].lower()}"
    score = 50 if c["ct"] in ("Edit", "Document") else 30
    if c["focused"]:
        score += 40  # 用户正在用的输入框优先保留
    if any(w in semantic for w in FOCUS_STRONG_TERMS):
        score += 120
    elif any(w in semantic for w in FOCUS_ALLOWED_TERMS):
        score += 80
    elif any(w in semantic for w in FOCUS_SUPPORTING_TERMS):
        score += 70
    n = c.get("norm")
    if n:
        if n["w"] >= 0.30:
            score += 25
        if 0.02 <= n["h"] <= 0.60:
            score += 10
        if n["w"] >= 0.45:
            score += 30
        if n["y"] >= 0.55:
            score += 20   # 下半区加分(聊天框贴底)
        elif n["y"] <= 0.25:
            score -= 15   # 顶部惩罚
    return score


def _cue_boost(cand, cues) -> int:
    """相邻动作语义加成: 候选同一行(垂直同排)且水平邻近的位置上有
    发送/搜索/创建/提交等提示元素时 +60 —— 模拟『发送图标、搜索图标旁边的输入框』识别。
    cues 来自扫描阶段收集的 (名字, 绝对矩形), 纯几何判定, 不做 COM 调用"""
    rect = cand.get("rect")
    if not rect or not cues:
        return 0
    cl, ct, cw, ch = rect
    cand_cy = ct + ch / 2
    cand_cx = cl + cw / 2
    for _name, (ql, qt, qw, qh) in cues:
        dy = abs((qt + qh / 2) - cand_cy)
        if dy > ch * 0.75 + 12:
            continue  # 不同排
        dx = abs((ql + qw / 2) - cand_cx)
        if dx > cw * 1.5 + 60:
            continue  # 同排但相距过远(整页工具栏)
        return 60
    return 0


def _recorded_score(c, prof):
    """档案匹配打分 (对齐 SayAll recorded_candidate_score); control_type 必须完全一致"""
    if c["ct"] != prof.get("control_type", ""):
        return None
    score = 0
    strong = 0
    if _sem_match(c["aid"], prof.get("automation_id", "")):
        score += 500; strong += 1
    if _sem_match(c["name"], prof.get("target_name", "")):
        score += 220; strong += 1
    if _sem_match(c["cls"], prof.get("class_name", "")):
        score += 180; strong += 1
    pn, pc = prof.get("norm"), c.get("norm")
    dist = None
    if pn and pc:
        dist = (abs(pc["x"] - pn["x"]) + abs(pc["y"] - pn["y"])
                + abs(pc["w"] - pn["w"]) + abs(pc["h"] - pn["h"]))
        score += round(max(0.0, 120.0 - dist * 120.0))
    if strong == 0:
        # 无强语义命中: 只有"档案完全无语义特征 + 矩形 5% 内重合"的匿名输入框才认账
        rect_only = not (prof.get("automation_id") or prof.get("class_name")
                         or prof.get("target_name"))
        if not (rect_only and dist is not None and dist <= 0.05):
            return None
        return score
    return score if score >= FOCUS_RECORDED_THRESHOLD else None


def _recorded_best(cands, prof):
    scored = []
    for i, c in enumerate(cands):
        s = _recorded_score(c, prof)
        if s is not None:
            scored.append((i, s))
    if not scored:
        return None
    scored.sort(key=lambda p: -p[1])
    if len(scored) > 1 and scored[0][1] - scored[1][1] < FOCUS_RECORDED_MARGIN:
        return None  # 与第二名分差不足: 宁可不聚焦也不聚焦错
    return scored[0][0]


def _focus_verify(ctrl) -> bool:
    """读回验证: SetFocus 调用成功不算数, 要确认焦点真的落在该元素上。
    Chromium 的 contenteditable 等元素可能不报 HasKeyboardFocus, 用 RuntimeId 对比兜底
    (对齐 SayAll element_has_focus: HasKeyboardFocus OR CompareRuntimeIds)"""
    try:
        if bool(ctrl.HasKeyboardFocus):
            return True
    except Exception:
        pass
    try:
        focused = _UIA_AUTO.GetFocusedControl()
        if focused is None:
            return False
        return list(ctrl.GetRuntimeId() or []) == list(focused.GetRuntimeId() or [])
    except Exception:
        return False


def _scan_candidates(win_rect):
    """以前台窗口为根走 UIA 树: 采集输入框候选 + 动作提示元素(发送/搜索等按钮);
    在专职 UIA 线程内执行。返回 (候选列表, 提示列表[(名, 绝对矩形)])"""
    fg = _UIA_AUTO.GetForegroundControl()
    if fg is None:
        return [], []
    out = []
    cues = []
    for i, item in enumerate(_UIA_AUTO.WalkControl(fg, includeTop=True, maxDepth=12)):
        if i >= FOCUS_MAX_NODES:
            log(f"focus_scan 截断: 节点超过 {FOCUS_MAX_NODES}")
            break
        ctrl = item[0] if isinstance(item, tuple) else item
        c = _candidate_from(ctrl, win_rect)
        if c:
            out.append(c)
            continue
        # 动作提示元素: 按钮/链接/菜单项, 名字带 发送/搜索/创建/提交 等
        try:
            ctn = _ct_name(ctrl)
            if ctn in ("Button", "Hyperlink", "SplitButton", "MenuItem"):
                nm = (ctrl.Name or "").lower()
                if nm and any(w in nm for w in FOCUS_CUE_TERMS):
                    r = ctrl.BoundingRectangle
                    cues.append((nm[:20], (r.left, r.top, r.width(), r.height())))
        except Exception:
            pass
    return out, cues


def _looks_like_address(v: str) -> bool:
    """地址形态启发式: Edge/Chrome 的 omnibox Value 通常剥掉协议头
    (file:///D:/x → 'D:/x'; https://chatgpt.com/c/1 → 'chatgpt.com/c/1'),
    所以不能只认 '://', 要认『无空格 + 含 / + 含 .』的路径型文本"""
    if "://" in v:
        return True
    return " " not in v and "/" in v and "." in v and len(v) >= 6


def _browser_page_impl() -> dict:
    """读浏览器地址栏 -> 站点 origin + 完整 URL。浏览器 chrome(地址栏)永远暴露在 UIA,
    不受网页无障碍树完整度影响 (地址栏 Edit 的 Value 属性 = 当前地址文本, 通常剥掉协议头)。
    读不到时把扫到的 Edit 值写日志, 保证可观测"""
    fg = _UIA_AUTO.GetForegroundControl()
    if fg is None:
        log("browser_page: 前台控件取不到")
        return {"origin": "", "url": ""}
    best = ""
    seen = []
    for i, item in enumerate(_UIA_AUTO.WalkControl(fg, includeTop=True, maxDepth=12)):
        if i >= 600:
            break
        ctrl = item[0] if isinstance(item, tuple) else item
        try:
            if int(ctrl.ControlType) != int(_UIA_AUTO.ControlType.EditControl):
                continue
            v = ctrl.GetPropertyValue(30045)  # UIA_ValueValuePropertyId
            if not isinstance(v, str) or not v.strip():
                continue
            v = v.strip()
            if len(seen) < 6:
                seen.append(v[:40])
            if _looks_like_address(v) and len(v) > len(best):
                best = v
        except Exception:
            continue
    if not best:
        log(f"browser_page: 未读到地址 URL, Edit 值候选={seen}")
        return {"origin": "", "url": ""}
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/?#]+)([^#]*)", best)
    if m:
        return {"origin": m.group(1).lower(), "url": best}
    m = re.match(r"^([^/?#]+)([/?#].*)?$", best)  # 无协议头: host 到第一个 / 为止 (file 页 host=盘符)
    if m:
        return {"origin": m.group(1).lower(), "url": best}
    log(f"browser_page: 地址格式无法解析: {best[:60]}")
    return {"origin": "", "url": ""}


def _fg_page():
    """前台窗口的 (站点 origin, 完整 URL); 非浏览器/读不到返回 ("", "")"""
    info = _fg_info()
    if info is None:
        return "", ""
    if info[3].lower() not in FOCUS_BROWSER_EXES or not HAS_UIA:
        return "", ""
    page = _uia_call(_browser_page_impl, timeout=8.0) or {}
    return (page.get("origin") or "", page.get("url") or "")


def _page_key(url: str) -> str:
    """页面级作用域键: scheme+host+path (去掉查询参数与锚点); 兼容无协议头地址"""
    v = (url or "").strip()
    if not v:
        return ""
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/?#]+)([^?#]*)", v)
    if not m:
        m = re.match(r"^([^/?#]+)([/?#].*)?$", v)  # 无协议头 (Edge omnibox 常见)
    if not m:
        return ""
    return (m.group(1) + (m.group(2) or "")).rstrip("/").lower()


def _fg_site() -> str:
    """前台窗口的站点身份: 浏览器读地址栏 origin; 原生应用返回 ''"""
    site, _url = _fg_page()
    return site


def _uia_recorded_impl(profiles, name, site) -> str:
    """语义档案回放 (重试 + 读回验证); 在专职 UIA 线程内执行"""
    empty = 0
    for _attempt in range(FOCUS_RETRY_ATTEMPTS):
        info = _fg_info()
        if info is None:
            return ""
        win_rect, win_cls, win_title, exe = info
        cands, _cues = _scan_candidates(win_rect)
        if not cands:
            # Chromium 部分无障碍树转全量有构建延迟: 连续多次为空才放弃
            empty += 1
            if empty >= 3:
                return ""
            time.sleep(FOCUS_RETRY_INTERVAL)
            continue
        mine = [p for p in profiles
                if _profile_matches(p, exe, site) and p.get("control_type")]
        if name:
            mine.sort(key=lambda p: 0 if str(p.get("label") or "") == name else 1)
        for prof in mine:
            idx = _recorded_best(cands, prof)
            if idx is None:
                continue
            c = cands[idx]
            try:
                c["ctrl"].SetFocus()
                ok = _focus_verify(c["ctrl"])
            except Exception:
                ok = False
            if ok:
                return (c["name"] or c["cls"] or c["ct"])[:30]
        return ""  # 档案匹配是确定性的: 一轮扫描即可判定命中与否
    return ""


def _uia_composer_impl(profiles, name, site) -> str:
    """通用打分 + 相邻动作语义加成 (重试 + 读回验证); 在专职 UIA 线程内执行。
    该窗口若被用户学成"仅位置"模式, 不用通用打分覆盖用户学过的意图"""
    empty_scans = 0
    logged_scan = False
    for attempt in range(FOCUS_RETRY_ATTEMPTS):
        info = _fg_info()
        if info is None:
            return ""
        win_rect, win_cls, win_title, exe = info
        cands, cues = _scan_candidates(win_rect)
        if not cands:
            empty_scans += 1
            if empty_scans >= 3:
                log(f"focus_scan win={win_cls} 连续空树, 判定 UIA 盲区")
                return ""
            time.sleep(FOCUS_RETRY_INTERVAL)
            continue
        mine = [p for p in profiles if _profile_matches(p, exe, site)]
        point_only = any(p.get("point") and not p.get("control_type") for p in mine)
        scored = []
        for i, c in enumerate(cands):
            scored.append([i, _composer_score(c)])
        scored.sort(key=lambda e: -e[1])
        for e in scored[:3]:   # 只对前 3 名做邻域判定(纯几何, 无 COM 调用)
            e[1] += _cue_boost(cands[e[0]], cues)
        scored.sort(key=lambda e: -e[1])   # 加成后必须重排, 否则选的还是原始分最高者
        if not logged_scan:
            logged_scan = True
            top3 = [(e[1], f"{cands[e[0]]['ct']}|{(cands[e[0]]['name'] or '')[:14]}|rect={cands[e[0]]['rect']}")
                    for e in scored[:3]]
            log(f"focus_scan win={win_cls} cands={len(cands)} cues={cues} top3={top3}")
        if point_only:
            return ""
        scored = [e for e in scored if e[1] >= FOCUS_COMPOSER_THRESHOLD]
        if scored:
            c = cands[scored[0][0]]
            try:
                c["ctrl"].SetFocus()
                ok = _focus_verify(c["ctrl"])
            except Exception:
                ok = False
            if ok:
                return (c["name"] or c["cls"] or c["ct"])[:30]
        time.sleep(FOCUS_RETRY_INTERVAL)
    return ""


def _uia_learn_capture(win_rect, mouse) -> dict:
    """学习采集: 优先读当前焦点控件的语义特征; 失败时按鼠标位置命中测试,
    从命中元素向上最多 6 层找最近合格祖先 (Chromium 有时把 UIA 焦点报在网页根上,
    而学习时鼠标必然停在目标输入框上, 命中测试更可靠)"""
    c = None
    focused = _UIA_AUTO.GetFocusedControl()
    if focused is not None:
        c = _candidate_from(focused, win_rect, learn=True)
    if c is None and mouse:
        try:
            hit = _UIA_AUTO.ControlFromPoint(int(mouse[0]), int(mouse[1]))
        except Exception:
            hit = None
        cur = hit
        for _ in range(6):
            if cur is None:
                break
            c = _candidate_from(cur, win_rect, learn=True)
            if c:
                break
            try:
                cur = cur.GetParentControl()
            except Exception:
                break
    if c is None:
        return None
    return {"control_type": c["ct"], "automation_id": c["aid"],
            "class_name": c["cls"], "target_name": c["name"], "norm": c.get("norm")}


def _nearest_edit_label(ctrl, mx, my, win_rect, budget=80):
    """容器内就近找 Edit: 点击落在输入框的容器上(composer 边距/搜索框容器/对话框体)
    而不在 Edit 本身上时, 在该容器内做有界遍历找最近的输入框。
    物理点击已把焦点交给应用, SetFocus 仅保险。返回 label 或 None"""
    edit_id = int(_UIA_AUTO.ControlType.EditControl)
    combo_id = int(_UIA_AUTO.ControlType.ComboBoxControl)
    leaf_ids = {edit_id, combo_id}
    found = []  # (dist, candidate)
    n = 0

    def desc(c, depth):
        nonlocal n
        if depth > 25 or n > budget:
            return
        n += 1
        try:
            cti = int(c.ControlType)
        except Exception:
            return
        if cti in leaf_ids:
            c_ = _candidate_from(c, win_rect, learn=True, allow_always=True, intent=True)
            if c_ and c_.get("rect"):
                l, t, w, h = c_["rect"]
                d = ((l + w / 2 - mx) ** 2 + (t + h / 2 - my) ** 2) ** 0.5
                found.append((d, c_))
            return
        try:
            for ch in c.GetChildren():
                desc(ch, depth + 1)
        except Exception:
            pass

    desc(ctrl, 0)
    if not found:
        return None
    found.sort(key=lambda x: x[0])
    _dist, c_ = found[0]
    try:
        c_["ctrl"].SetFocus()
        _focus_verify(c_["ctrl"])
    except Exception:
        pass
    return (c_["name"] or c_["cls"] or c_["ct"])[:30]


def _uia_hit_test_impl(win_rect, mx, my) -> str:
    """鼠标命中(包含剪枝递归): 只深入『矩形包含鼠标点』的子树, 其余整棵剪枝。
    深度优先全走会耗尽节点上限还到不了输入框(DeepSeek Harness 这类深 DOM 实测:
    1500 节点还没走出侧边栏); 按几何剪枝后路径长度≈树深, 任意大树都能直达。
    不用 ElementFromPoint——Chromium 部分无障碍模式下它会命中窗口框架而非网页内容"""
    fg = _UIA_AUTO.GetForegroundControl()
    if fg is None:
        log("hit_test: GetForegroundControl=None")
        return ""
    CT = _UIA_AUTO.ControlType
    input_ids = {int(CT.EditControl), int(CT.DocumentControl), int(CT.GroupControl),
                 int(CT.ComboBoxControl)}
    edit_id = int(CT.EditControl)
    leaf_ids = {edit_id, int(CT.ComboBoxControl)}
    hits = []   # (ctrl, ct, rect, area) 包含鼠标点的可输入候选
    n_visited = 0

    def descend(ctrl, depth):
        nonlocal n_visited
        if depth > 40 or n_visited > 800:
            return
        n_visited += 1
        try:
            cti = int(ctrl.ControlType)
        except Exception:
            return
        try:
            r = ctrl.BoundingRectangle
            rect = (r.left, r.top, r.width(), r.height())
        except Exception:
            rect = None
        if rect is not None and (rect[2] <= 0 or rect[3] <= 0):
            rect = None  # 0 尺寸(Chromium 部分树的占位): 不能据此剪枝
        if rect is not None and not (rect[0] <= mx <= rect[0] + rect[2]
                                     and rect[1] <= my <= rect[1] + rect[3]):
            return  # 矩形不含鼠标点: 整棵子树剪枝
        if cti in input_ids and rect is not None:
            try:
                aid = (ctrl.AutomationId or "").strip()
                if aid.lower() != "rootwebarea":
                    # 联动层不按语义词表预排除(地址栏/搜索框都是直接点击意图),
                    # 完整判断交给后面的门槛 + 逐候选诊断
                    hits.append((ctrl, _ct_name(ctrl), rect, rect[2] * rect[3]))
            except Exception:
                pass
            if cti in leaf_ids:
                return  # Edit/ComboBox 是叶子
        try:
            for child in ctrl.GetChildren():
                descend(child, depth + 1)
        except Exception:
            pass

    descend(fg, 0)
    if not hits:
        log(f"hit_test: 未命中 visited={n_visited} @({mx},{my}) 前台cls={fg.ClassName!r}")
        return ""
    # 最小面积优先(内层元素), 过完整门槛 + SetFocus; 逐候选记录拒绝原因
    for idx, (ctrl, ct, rect, _area) in enumerate(sorted(hits, key=lambda x: x[3])):
        try:
            tag = (f"候选[{idx}] {ct} aid={(ctrl.AutomationId or '')[:20]!r} "
                   f"cls={(ctrl.ClassName or '')[:24]!r} name={(ctrl.Name or '')[:24]!r}")
        except Exception:
            tag = f"候选[{idx}] {ct}"
        trace = []
        c = _candidate_from(ctrl, win_rect, learn=True, allow_always=True,
                            intent=True, trace=trace)
        if c is None:
            log(f"hit_test {tag} rect={rect} 门槛拒绝: {trace[-1] if trace else '?'}")
            continue
        try:
            c["ctrl"].SetFocus()
            ok = _focus_verify(c["ctrl"])
        except Exception as e:
            ok = False
        if not ok:
            # 几何包含 + 门槛已确认是用户点中的可输入元素, 物理点击本身已把 DOM 焦点
            # 落位(SetFocus 只是保险), 读回不认也按几何意图接受——否则该元素永远不弹
            log(f"hit_test {tag} 读回未确认 -> 按几何意图接受")
            return (c["name"] or c["cls"] or c["ct"])[:30]
        return (c["name"] or c["cls"] or c["ct"])[:30]
    # 邻域意图: 点位落在输入框的容器上(composer 边距/搜索框容器/对话框体)而 Edit 不在点位上,
    # 在最小的几个包含容器内就近找 Edit —— 超过窗口 1/4 的容器(整页/时间线)不算意图容器
    try:
        if win_rect:
            warea = max(1.0, (win_rect[2] - win_rect[0]) * (win_rect[3] - win_rect[1]))
        else:
            warea = 1920.0 * 1032.0
    except Exception:
        warea = 1920.0 * 1032.0
    for ctrl, ct, rect, area in sorted(hits, key=lambda x: x[3]):
        if area / warea > 0.25:
            break
        res = _nearest_edit_label(ctrl, mx, my, win_rect)
        if res:
            log(f"hit_test: 容器内就近命中 Edit (容器 {ct})")
            return res
    # 对话框/展开式搜索: 点击后应用把焦点自动放到新出现的输入框上,
    # 输入框不在点击坐标处 —— 几何落空时读焦点元素补一刀
    try:
        focused = _UIA_AUTO.GetFocusedControl()
    except Exception:
        focused = None
    if focused is not None:
        trace = []
        c = _candidate_from(focused, win_rect, learn=True, allow_always=True,
                            intent=True, trace=trace)
        if c is not None:
            log(f"hit_test: 几何落空, 焦点元素命中: {c['ct']} name={(c['name'] or '')[:24]!r}")
            return (c["name"] or c["cls"] or c["ct"])[:30]
        log(f"hit_test: 几何落空且焦点元素未过门槛({trace[-1] if trace else '?'})")
    else:
        log("hit_test: 几何落空且焦点读取为空")
    log(f"hit_test: 全部候选被拒 hits={len(hits)} @({mx},{my})")
    return ""


def _read_value(ctrl):
    """读控件 ValuePattern 文本; 密码框/不支持 Value 的控件返回 None (UIA 专职线程内)"""
    try:
        if ctrl.GetPropertyValue(_UIA_AUTO.PropertyId.IsPasswordProperty):
            return None
    except Exception:
        return None
    try:
        return ctrl.GetValuePattern().Value or ""
    except Exception:
        return None


def _descend_to_edit(root, win_rect, budget=260, depth_max=10):
    """焦点被报在容器/网页根(RootWebArea)时, 从焦点节点向下找真正的可输入控件。
    只读探查不动焦点; 优先 Edit/Document, 其次带 Value/TextPattern 的过门槛控件。UIA 专职线程内"""
    stack = [(root, 0)]
    seen = 0
    while stack and seen < budget:
        node, d = stack.pop()
        seen += 1
        try:
            c = _candidate_from(node, win_rect, learn=True)
        except Exception:
            c = None
        if c:
            ok_type = c["ct"] in ("Edit", "Document")
            if not ok_type:
                try:
                    ok_type = bool(c["ctrl"].GetPropertyValue(_UIA_AUTO.PropertyId.IsValuePatternAvailableProperty)) \
                        or bool(c["ctrl"].GetPropertyValue(_UIA_AUTO.PropertyId.IsTextPatternAvailableProperty))
                except Exception:
                    ok_type = False
            if ok_type:
                return c
        if d >= depth_max:
            continue
        try:
            for ch in node.GetChildren():
                stack.append((ch, d + 1))
        except Exception:
            pass
    return None


# ---- 无 Value/Text 模式的富文本编辑器(ProseMirror/tiptap/Quill 等): 叶子 Name 拼接读 ----
# 这类编辑器(如 DSH 对话框)的焦点元素是 GroupControl 且不带任何文本模式,
# 文本只存在于子树叶子节点的 Name 上(实测: 打字后整树重组, 叶子 Name 实时更新);
# 传统 Value/TextPattern 读法读到的是不刷新的旧快照(僵死), 必须走叶子拼接。
_LEAF_EDITOR_MARKS = ("prosemirror", "tiptap", "cm-editor", "ql-editor", "public-drafteditor")
_LEAF_EDITOR_TOKEN_FILTER = ("发消息或创建任务", "使用技能", "添加资料", "调用指令", "@ 文件或对话")


def _uia_read_leaf_text(ctrl):
    """拼接 ctrl 子树下所有叶子节点(无子节点)的 Name, 按文档顺序; 返回 (文本, 叶子片段列表)"""
    parts = []
    count = 0
    stack = [(ctrl, 0)]
    while stack and count < 400:
        node, depth = stack.pop(0)
        count += 1
        try:
            kids = node.GetChildren()
            if not kids or depth >= 10:
                name = node.Name or ""
                if name:
                    parts.append(name)
            else:
                for k in reversed(kids):
                    stack.insert(0, (k, depth + 1))
        except Exception:
            continue
    return "".join(parts), parts


def _uia_try_leaf_editor_read(focused):
    """焦点是无模式富文本编辑器(ProseMirror 系)时, 用叶子 Name 拼接读取。
    光标位置不可得 -> 返回 caret=None, 调用方置盲区(定位走 Ctrl+Home 锚定)。
    占位符/按钮标签过滤: 若叶子片段全部由已知占位符词组成, 视为空输入框。"""
    try:
        fcls = (focused.ClassName or "").lower()
    except Exception:
        return None
    if not any(m in fcls for m in _LEAF_EDITOR_MARKS):
        return None
    text, parts = _uia_read_leaf_text(focused)
    if len(text) > 60000:
        text = ""
    tokens = [p.strip() for p in parts if p.strip()]
    if tokens and all(any(t == tok or tok in t for tok in _LEAF_EDITOR_TOKEN_FILTER) for t in tokens):
        text = ""   # 叶子全部由占位符/按钮词组成 = 输入框实际为空
    return {"label": "leaf-editor", "text": text, "caret": None, "total": None, "reason": ""}


def _uia_focus_text_read_impl(win_rect):
    """读当前焦点控件: 全文(Value 缺席时用 TextPattern 兜底, 30000 字封顶) + 光标绝对偏移(TextPattern)。
    焦点被报在网页根/容器时向下探查真正的输入控件(Chromium 常见)。
    返回 {"label", "text"|None, "caret"|None, "total"|None, "reason"}; UIA 专职线程内"""
    focused = _UIA_AUTO.GetFocusedControl()
    if focused is None:
        return {"label": "", "text": None, "caret": None, "total": None, "reason": "no-focus"}
    # 无模式富文本编辑器(ProseMirror 系, 如 DSH 对话框): 叶子 Name 拼接读取(实时),
    # 必须在门槛检查之前 —— 这类编辑器的焦点 Group 会被 IsReadOnly 门槛拒绝。
    leaf = _uia_try_leaf_editor_read(focused)
    if leaf is not None:
        return leaf
    try:
        c = _candidate_from(focused, win_rect, learn=True)
    except Exception:
        c = None
    if not c:
        c = _descend_to_edit(focused, win_rect)
        if not c:
            return {"label": "", "text": None, "caret": None, "total": None, "reason": "gate"}
    label = (c["name"] or c["cls"] or c["ct"])[:30]
    ctrl = c["ctrl"]
    text = _read_value(ctrl)
    caret = None
    total = None
    tp_text = None
    try:
        tp = ctrl.GetTextPattern()
        doc = tp.DocumentRange   # 注意: DocumentRange 是 property, 加括号调用会抛 TypeError('TextRange' object is not callable)
        EP = _UIA_AUTO.TextPatternRangeEndpoint
        try:
            total = abs(doc.CompareEndpoints(EP.Start, doc, EP.End))
        except Exception:
            total = None
        try:
            sel = tp.GetSelection()
            if sel:
                probe = sel[0].Clone()
                probe.MoveEndpointByRange(EP.Start, doc, EP.Start)   # 选区起点快照压到文档头
                # 符号约定: RichEdit 系 provider 返回 -(偏移) (2026-11-25 记事本探针实证),
                # 统一取绝对值再校验 —— 否则负偏移被丢弃 -> caret=None -> 兜底 Ctrl+End 拽尾
                off = abs(probe.CompareEndpoints(EP.Start, sel[0], EP.Start))  # 与选区起点的字符距 = 光标绝对偏移
                if total is None or 0 <= off <= total:
                    caret = off
        except Exception:
            pass
        try:
            if text is None:
                tp_text = doc.GetText(61001) or ""   # 全文兜底: contenteditable(无 Value)靠这里
                if len(tp_text) > 60000:
                    tp_text = ""                    # 超长跳过
        except Exception:
            pass
    except Exception:
        pass
    if text is None and tp_text:
        text = tp_text
    name_full = c["name"] or ""
    if text:
        # 清洗只用于占位符判定, 不改写镜像文本: 电脑框里真有零宽字符时, 长度必须与实际一致,
        # 否则所有锚点/光标偏移都会漂移
        probe = text.replace("\u200b", "").replace("\ufeff", "")
        if name_full.strip() and probe.strip() == name_full.strip():
            text = ""        # Chromium 把占位符文案当文本内容读出(输入框实际为空)
    if caret is not None and (caret < 0 or (text and caret > _u16_len(text))):
        caret = None         # 偏移超出 utf16 宽度 -> 不可信 -> 走原点跟随兜底
    if not text and caret is None:
        return {"label": label, "text": None, "caret": None, "total": total, "reason": "no-text"}
    return {"label": label, "text": text, "caret": caret, "total": total, "reason": ""}


def _uia_current_focus_impl(win_rect, mouse) -> str:
    """层0: 当前键盘焦点/鼠标命中已是可输入元素 -> 直接保留。
    物理点击是 0 秒新鲜度的最强意图证据, 优先于一切档案缓存; 找不到可输入元素返回 ""。
    在专职 UIA 线程内执行"""
    # a) OS 焦点: 触控板点击后焦点必然已落在被点的元素上
    focused = _UIA_AUTO.GetFocusedControl()
    if focused is not None:
        c = _candidate_from(focused, win_rect, learn=True)
        if c:
            return (c["name"] or c["cls"] or c["ct"])[:30]
        # Chromium 常把 UIA 焦点报在网页根(RootWebArea)而非实际输入框:
        # 门槛拒绝后不放弃, 改用鼠标命中(走树包含判定)定位真实落点
        log("layer0: 焦点报在网页根/未过门槛, 改用鼠标命中")
    # b) 鼠标命中(走树包含判定)
    if mouse:
        return _uia_hit_test_impl(win_rect, int(mouse[0]), int(mouse[1]))
    return ""


def _profile_id(p: dict) -> str:
    """档案身份: 进程 + 站点(origin)/窗口类 的稳定散列(重学同目标即覆盖)"""
    if p.get("id"):
        return p["id"]
    raw = f"{p.get('exe', '')}|{p.get('win_class', '')}|{p.get('site', '')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def _profile_matches(p: dict, exe: str, site: str) -> bool:
    """档案与当前窗口是否同一意图上下文。
    站点身份(origin, 取自浏览器地址栏)取代窗口标题——标题随对话/项目变化,
    origin 才是跨项目窗口稳定的锚:
    - 当前窗口是浏览器: 档案必须带相同站点(origin)。无站点的档案无法证明
      属于哪个网站, 一律不套用(否则一条档案会在所有页面间复用——2026-11-22 实测);
    - 当前窗口是原生应用: 只命中无站点的档案(浏览器档案不套用到原生窗口)。
    进程不同一律不命中。"""
    if (p.get("exe") or "") and exe and p["exe"].lower() != exe.lower():
        return False
    is_browser = exe.lower() in FOCUS_BROWSER_EXES
    ps = (p.get("site") or "").strip().lower()
    cs = (site or "").strip().lower()
    if is_browser:
        if not ps or ps != cs:
            return False  # 浏览器窗口: 必须带相同站点; 无站点档案跨页复用会点错
    elif ps:
        return False  # 浏览器档案不套用到原生窗口
    return True


def _load_profiles() -> list:
    """学习档案 = focus_profiles.json + 旧 focus_pos.json 迁移 + custom.json 的 focus_pos 字段;
    每条补齐稳定 id (exe+类名+标题 的散列)"""
    profiles = []
    try:
        with open(FOCUS_PROFILES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            profiles = [p for p in data if isinstance(p, dict) and (p.get("win_class") or p.get("exe"))]
    except Exception:
        pass
    if not profiles:
        try:
            with open(FOCUS_POS_FILE, encoding="utf-8") as f:
                pos = json.load(f)
            if isinstance(pos, dict) and "x_ratio" in pos:
                profiles = [{"label": "", "win_class": pos.get("win_class", ""), "point": pos}]
        except Exception:
            pass
    for item in load_customs():
        pos = item.get("focus_pos")
        if isinstance(pos, dict) and "x_ratio" in pos:
            profiles.append({"label": str(item.get("name") or ""),
                             "win_class": pos.get("win_class", ""), "point": pos})
    for p in profiles:
        p.setdefault("id", _profile_id(p))
    return profiles


def _save_profile(prof: dict) -> None:
    """学习结果落盘: 同一目标(exe+类名+标题)重学即覆盖, 不同目标各自一条"""
    prof["id"] = _profile_id(prof)
    try:
        with open(FOCUS_PROFILES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        keep = [p for p in data if isinstance(p, dict) and _profile_id(p) != prof["id"]]
    except Exception:
        keep = []
    keep.append(prof)
    with open(FOCUS_PROFILES_FILE, "w", encoding="utf-8") as f:
        json.dump(keep, f, ensure_ascii=False, indent=2)


def focus_delete(pid_: str) -> str:
    """删除一条应用档案(第二页列表的「删」按钮)"""
    try:
        with open(FOCUS_PROFILES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        items = [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []
    except Exception:
        items = []
    keep = [p for p in items if _profile_id(p) != pid_]
    if len(keep) == len(items):
        return "没找到该档案(可能已被删除)"
    with open(FOCUS_PROFILES_FILE, "w", encoding="utf-8") as f:
        json.dump(keep, f, ensure_ascii=False, indent=2)
    log(f"删除输入框档案 id={pid_} 剩余={len(keep)}")
    return "已删除该应用的记录位置"


def _replay_point(pt: dict, win_rect) -> bool:
    """按记忆位置点击(窗口身份已在上层匹配)"""
    l, t, r, b = win_rect
    x = l + float(pt.get("x_ratio", 0.5)) * (r - l)
    if pt.get("y_anchor") == "bottom":
        y = b - int(pt.get("y_dist", 0))   # 聊天框贴底: 窗口变高也跟着底边走
    else:
        y = t + int(pt.get("y_dist", 0))
    pyautogui.moveTo(int(x), int(y), duration=0)
    pyautogui.click()
    log(f"按记忆位置点击 ({int(x)},{int(y)})")
    return True


def focus_learn(name: str) -> str:
    """两段式学习的确认步: 采当前焦点控件的语义特征 + 当前鼠标位置, 按目标身份(exe+站点)存档案"""
    info = _fg_info()
    if info is None:
        return "取不到前台窗口，学习失败"
    (l, t, r, b), cls, _title, exe = info
    mx, my = pyautogui.position()
    inside = l <= mx <= r and t <= my <= b
    if DRY:
        log(f"DRYRUN 记录输入框档案 窗口={cls} exe={exe}")
        return f"DRYRUN 记录输入框（{exe or cls}）"
    site, page_url = _fg_page()
    cap = None
    if HAS_UIA:
        cap = _uia_call(_uia_learn_capture, (l, t, r, b), (mx, my), timeout=10.0)
    point = None
    if inside:
        anchor_bottom = abs(my - b) <= abs(my - t)
        point = {"x_ratio": round((mx - l) / max(1, r - l), 4),
                 "y_anchor": "bottom" if anchor_bottom else "top",
                 "y_dist": (b - my) if anchor_bottom else (my - t)}
    if cap is None and point is None:
        return "没采到输入框：请先用触控板点击目标输入框，再按一次「📍 记录位置」"
    prof = {"label": str(name or ""), "win_class": cls, "exe": exe, "site": site, "point": point}
    if cap:
        prof.update({"control_type": cap["control_type"], "automation_id": cap["automation_id"],
                     "class_name": cap["class_name"], "target_name": cap["target_name"]})
        if cap.get("norm"):
            prof["norm"] = cap["norm"]
    else:
        # 仅位置档案(浏览器): 绑定页面级作用域, 换页自动失效(位置是布局的投影, 布局随内容变)
        page_key = _page_key(page_url)
        if page_key:
            prof["page_key"] = page_key
    _save_profile(prof)
    kind = "语义+位置" if (cap and point) else ("语义" if cap else "仅位置")
    scope = " · 限定同一页面" if (not cap and page_key) else ""
    log(f"记录输入框档案 exe={exe or '?'} 窗口={cls} 模式={kind} 绑定={name or '通用'} 站点={site or '-'}")
    return f"已记录输入框（{kind}{scope} · {exe or cls}{(' · ' + site) if site else ''}）"


def _norm_newlines(text, caret=None):
    """\r\n / \r -> \n(手机框 textarea 会做同样归一化, 两端镜像坐标才一致); caret 偏移同步折算"""
    out = []
    ci = caret
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\r":
            if ci is not None and i < ci:
                ci -= 1
            if i + 1 < n and text[i + 1] == "\n":
                i += 2
            else:
                i += 1
            out.append("\n")
        else:
            out.append(ch)
            i += 1
    return "".join(out), ci


def _focus_read_clipboard() -> str:
    """剪贴板兜底读回(UIA 盲区应用, 聚焦时刻专用): 置哨兵 -> Ctrl+A 全选 -> Ctrl+C -> 读 -> → 收拢选区。
    光标随后在字段尾, 镜像原点=字段尾精确对齐。哨兵校验区分「字段为空」与「复制失败」;
    读完后还原用户剪贴板(文本部分)。侵入性: 输入框全选闪烁一次"""
    old = ""
    try:
        old = pyperclip.paste() or ""
    except Exception:
        old = ""
    sent = "KZQ-CLIP-9F3A7C2B"
    try:
        pyperclip.copy(sent)
    except Exception:
        pass
    log("focus readback: 剪贴板兜底启动(Ctrl+A 全选闪烁, 光标暂由选区替代)")
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.hotkey("ctrl", "c")
    text = sent
    deadline = time.time() + 0.8   # 轮询等复制落地(应用剪贴板写入有快有慢), 哨兵被替换即成功
    while time.time() < deadline:
        time.sleep(0.08)
        try:
            cur = pyperclip.paste() or ""
        except Exception:
            cur = sent
        if cur != sent:
            text = cur
            break
    _srv_tap("right")   # 收拢全选: 光标到选区尾=字段尾
    if text == sent:
        text = ""       # 轮询超时仍是哨兵 = 字段为空(全选复制没写入内容)
    try:
        pyperclip.copy(old)   # 无条件还原用户剪贴板(含空), 哨兵不残留
    except Exception:
        pass
    return text


# ---- Chromium CDP 通道: DSH/Edge/Chrome 开调试端口时的"Win32 等价物" ----
# 毫秒级读写文本+光标(selectionStart/End), 与记事本的 EM_SETSEL 同级; 无 UIA 超时/僵死。
# DSH 建议用 start_dsh_cdp.bat 启动(--remote-debugging-port=9223); 未开端口时自动回退 UIA。
_CDP_PORT_CANDIDATES = (9223, 9224, 9225, 9222)   # DSH=9223 豆包=9224 豆包工作=9225 兼容浏览器调试口 9222
_CDP_LOCK = threading.Lock()
_CDP = {"conns": {}, "fail_ts": 0.0, "mid": 0}
_CDP_EVAL_READ = (
    "(function(){var a=document.activeElement;if(!a)return null;"
    "var t=(a.tagName==='TEXTAREA'||a.tagName==='INPUT'||a.isContentEditable);if(!t)return null;"
    "var v=a.isContentEditable?a.innerText:(a.value||'');"
    "if(v.length>60000)return null;"
    "return {v:v,s:a.selectionStart,e:a.selectionEnd};})()"
)


def _cdp_discover():
    """发现全部候选端口上带调试页的 CDP 端点(可多个应用同时运行); 失败 30 秒退避"""
    import urllib.request
    if time.time() - _CDP["fail_ts"] < 30:
        return bool(_CDP["conns"])
    for port in _CDP_PORT_CANDIDATES:
        if port in _CDP["conns"]:
            continue
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=0.5) as resp:
                targets = json.loads(resp.read().decode("utf-8"))
            page = next((t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
            if page:
                import websocket
                ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=2)
                _CDP["conns"][port] = ws
                log(f"CDP 通道已连接: port={port}")
        except Exception:
            continue
    if not _CDP["conns"]:
        _CDP["fail_ts"] = time.time()
    return bool(_CDP["conns"])


def _cdp_eval(expr):
    """CDP Runtime.evaluate: 在所有已连接端点上求值, 返回第一个有焦点可编辑输入的
    应用结果(按 activeElement 判定, 天然区分同时运行的多个应用); 断连自动摘除重探"""
    with _CDP_LOCK:
        if not _CDP["conns"] and not _cdp_discover():
            return None
        _CDP["mid"] += 1
        mid = _CDP["mid"]
        payload = json.dumps({"id": mid, "method": "Runtime.evaluate",
                              "params": {"expression": expr, "returnByValue": True}})
        results = []
        dead = []
        for port, ws in list(_CDP["conns"].items()):
            try:
                ws.send(payload)
                ws.settimeout(1.5)
                deadline = time.time() + 1.5
                val = None
                while time.time() < deadline:
                    raw = ws.recv()
                    if not raw:
                        break
                    msg = json.loads(raw)
                    if msg.get("id") == mid:
                        val = msg.get("result", {}).get("result", {}).get("value")
                        break
                results.append(val)
                if isinstance(val, dict) and isinstance(val.get("v"), str):
                    return val   # 该应用的焦点正是可编辑输入: 直接采用
            except Exception:
                dead.append(port)
        for port in dead:
            try:
                _CDP["conns"][port].close()
            except Exception:
                pass
            _CDP["conns"].pop(port, None)
        if dead and not _CDP["conns"]:
            _CDP["fail_ts"] = time.time()
        for val in results:
            if isinstance(val, dict):
                return val
    return None


def _focus_readback(info, clip: bool = False):
    """聚焦成功后读回焦点控件全文+光标 -> 建整框镜像(手机框是文本唯一事实源,
    删除/光标/点定位覆盖全部内容)。光标位置(TextPattern)拿到就精确对齐, 拿不到才
    Ctrl+End 归位字段尾对齐原点。UIA 读不到且 clip=True 时走剪贴板兜底。
    gate/no-text 时延迟重试一次(慢应用聚焦后文本未就绪), 再失败才判盲区。
    返回 (text|None, caret|None, reason); caret 为规范化文本中的 utf16 偏移(手机端直接可用)"""
    global _SRV_BUF, _SRV_CARET, _SRV_FULL, _SRV_BLIND, _CHROMIUM_READ_LAST
    # Win32 直控优先(Edit/RichEdit 系): 不进 UIA 队列 —— 无超时/读数僵死, 毫秒级
    w32 = _win32_readback()
    if w32 is not None:
        _hwnd, text_raw, raw_caret = w32
        text, _ = _norm_newlines(text_raw)
        if len(text) > 30000:
            log(f"focus: 输入框文本过长({len(text)}字), 跳过预填")
            return None, None, "too-long"
        # _win32_readback 已返回规范化 utf16 光标, 直接入账
        u16 = raw_caret
        with _SRV_LOCK:
            _SRV_BUF = text
            _SRV_CARET = _g_u16_index(text, u16) if u16 is not None else _g_len(text)
            _SRV_FULL = True
            _SRV_BLIND = False
        if u16 is None:
            log("focus readback: win32 光标读数缺失, Ctrl+End 归位字段尾(光标会跳到末尾)")
            pyautogui.hotkey("ctrl", "end")
            u16 = _u16_len(text)
        log(f"focus readback: win32 ok({len(text)}字, caret={u16})")
        return text, u16, ""
    if not HAS_UIA or info is None:
        return None, None, "no-uia"
    if _fg_is_chromium():
        # Chromium/Electron(Chrome/Edge/DSH/豆包): UIA 光标读数不可靠(僵死),
        # 文本读数亦可能滞后 —— 读写首选 CDP(带调试端口时=记事本同级);
        # 无 CDP 时 UIA 只同步文本(光标不可信, 置盲区锚定), 不做剪贴板全选(闪烁)。
        # 周期巡逻(clip=0)与显式点按(clip=1)同一读回通道, 由 0.3s 限频统一节流;
        # 读回不走写链且定位走箭头, 不影响打字延迟。
        global _CHROMIUM_READ_LAST
        if time.time() - _CHROMIUM_READ_LAST < _CHROMIUM_READ_MIN_GAP:
            return None, None, "chromium-ratelimit"
        _CHROMIUM_READ_LAST = time.time()
        # 首选 CDP: DSH 带 --remote-debugging-port 启动时, 与记事本同级(毫秒级+光标精确)
        cd = _cdp_eval(_CDP_EVAL_READ)
        if isinstance(cd, dict) and isinstance(cd.get("v"), str):
            if len(cd["v"]) > 60000:
                return None, None, "too-long"
            text, ci = _norm_newlines(cd["v"], cd.get("s"))
            g = _g_u16_index(text, ci) if ci is not None else _g_len(text)
            with _SRV_LOCK:
                _SRV_BUF = text
                _SRV_FULL = True
                _SRV_BLIND = False
                _SRV_CARET = g
            log(f"focus readback: CDP 同步({len(text)}字, caret={ci})")
            return text, ci, ""
        r = _uia_call(_uia_focus_text_read_impl, info[0], timeout=2.5)
        if not r or r.get("text") is None:
            reason = (r or {}).get("reason") or "no-text"
            log(f"focus readback: chromium 读回失败({reason}), 保持岛模式")
            return None, None, "chromium-read-fail"
        text, _ = _norm_newlines(r["text"])
        if len(text) > 30000:
            return None, None, "too-long"
        with _SRV_LOCK:
            _SRV_BUF = text
            _SRV_FULL = True
            _SRV_BLIND = True       # UIA 光标读数不可信: 定位走 Ctrl+Home 锚定, 编辑就近应用
            _SRV_CARET = None
        log(f"focus readback: chromium 文本同步({len(text)}字, 转盲区锚定)")
        return text, None, ""
    if not _uia_ready():
        # UIA 熔断冷却中: 不进队列空等, 也不做剪贴板闪烁兜底, 保持岛模式由锚定自愈
        return None, None, "uia-hot"
    r = _uia_call(_uia_focus_text_read_impl, info[0], timeout=4.0)
    if not r or r.get("text") is None:
        if not _uia_ready():
            return None, None, "uia-hot"   # 首次调用触发了熔断: 不再重试, 保持岛模式
        time.sleep(0.3)   # 慢应用聚焦后文本/焦点未就绪: 重试一次再判盲区
        r = _uia_call(_uia_focus_text_read_impl, info[0], timeout=4.0)
    if not r or r.get("text") is None:
        reason = (r or {}).get("reason") or "no-text"
        _SRV_BLIND = True    # UIA 读不到: 盲区模式, 纯定位改绝对锚定
        if clip:
            text, _ = _norm_newlines(_focus_read_clipboard())
            if text:
                with _SRV_LOCK:
                    unchanged = bool(_SRV_BUF) and text == _SRV_BUF
                    _SRV_BUF = text
                    _SRV_FULL = True
                    if unchanged and _SRV_CARET is not None and 0 <= _SRV_CARET <= _g_len(text):
                        # 文本未变: 光标账目仍可信。物理收拢(Ctrl+A -> →)把光标带到了字段尾,
                        # 按账目还原 —— 否则每次读回都把真实光标拽到尾, 后续 {at} 整体偏移。
                        # 还原列车超过 800(长文档)时放弃还原, 两端一致记到字段尾(防锁死服务端)
                        back = _g_len(text) - _SRV_CARET
                        if back > 800:
                            _SRV_CARET = _g_len(text)
                            caret_u16 = _u16_len(text)
                        else:
                            if back > 0:
                                _srv_tap("left", back)
                            caret_u16 = _g_u16_offset(text, _SRV_CARET)
                    else:
                        _SRV_CARET = _g_len(text)
                        caret_u16 = _u16_len(text)
                log(f"focus readback: UIA 读不到({reason}), 剪贴板兜底成功({len(text)}字)")
                return text, caret_u16, "clip"   # reason=clip: 客户端保持盲区模式, 防读回通道振荡
            reason = "clip-empty"
        log(f"focus readback: 读不到文本({reason}), 保持岛模式")
        return None, None, reason
    _SRV_BLIND = False    # UIA 读成功: 可读应用, 账目可信
    text_raw = r["text"]
    raw_caret = r.get("caret")
    text, _ = _norm_newlines(text_raw)
    if len(text) > 30000:
        log(f"focus: 输入框文本过长({len(text)}字), 跳过预填")
        return None, None, "too-long"
    u16 = _caret_norm_u16(text_raw, raw_caret if isinstance(raw_caret, int) else None)
    new_g = _g_u16_index(text, u16) if u16 is not None else _g_len(text)
    # 僵死读数交叉核对: 刚做过光标移动(2s 内)而读回与账目不符 -> provider 读数缓存未更新,
    # 不可信: 保持账目、不回写手机(否则手机光标被旧值掰走), 也不做 Ctrl+End 兜底
    stale = (u16 is not None and _SRV_FULL and _SRV_CARET is not None
             and time.time() - _SRV_LAST_MOVE_TS < 2.0 and new_g != _SRV_CARET)
    with _SRV_LOCK:
        _SRV_BUF = text          # 镜像 = 整个电脑输入框(坐标=字形簇)
        _SRV_FULL = True         # 整框镜像: 大跳可用 Ctrl+Home/End 文档级锚点
        if not stale:
            _SRV_CARET = new_g
    if u16 is None:
        # 光标位置未知: 不再 Ctrl+End 拽到末尾(用户会看到光标突然跳走)。
        # 转盲区模式: 纯定位走字段两端绝对锚定, 编辑按当前真实光标就近应用, 不依赖账目。
        log("focus readback: UIA 光标读数缺失, 转盲区模式(不移动电脑光标)")
        with _SRV_LOCK:
            _SRV_BLIND = True
            _SRV_CARET = None
        return text, None, ""
    if stale:
        log(f"focus readback: caret 读数疑似僵死({new_g} != 账目{_SRV_CARET}), 保持账目")
        return text, None, ""
    log(f"focus readback: ok({len(text)}字, caret={u16})")
    return text, u16, ""


def focus_read_entry(data: dict = None) -> dict:
    """读回当前焦点输入框全文+光标(点按联动/重锚定用): 不动电脑焦点不按键, 只建整框镜像;
    clip=1 时 UIA 失败后走剪贴板兜底(手动「⇻ 同步」专用)"""
    data = data or {}
    info = _fg_info()
    if DRY or info is None:
        return {"msg": "", "text": None, "caret": None, "reason": "no-focus"}
    text, caret, reason = _focus_readback(info, clip=bool(data.get("clip")))
    return {"msg": "", "text": text, "caret": caret, "reason": reason}


def focus_input_entry(name: str) -> "str | dict":
    """点手机输入框时的服务端入口, 五层兜底(按证据新鲜度排序):
    层0 当前焦点/鼠标命中(0秒新鲜) > 语义档案回放(origin+角色锚) > 记忆位置点击(仅位置档案限定同一页面)
    > UIA 通用打分+邻域语义 > 鼠标当前位置
    任一层聚焦成功都读回输入框全文+光标: 返回 {"msg","text","caret"}, 客户端预填手机框,
    镜像从"岛"升级为整个输入框, 删除/光标/点定位可作用于全部内容"""
    profiles = _load_profiles()
    site, page_url = _fg_page()
    page_key = _page_key(page_url)
    info = _fg_info()
    mx, my = pyautogui.position()
    inside = info is not None and (info[0][0] <= mx <= info[0][2] and info[0][1] <= my <= info[0][3])
    if DRY:
        if info:
            win_rect, cls, _t2, exe = info
            n = len([p for p in profiles if _profile_matches(p, exe, site)])
            log(f"DRYRUN 聚焦输入框 exe={exe} site={site or '-'} 档案={n}")
            return f"DRYRUN 聚焦输入框（{exe or cls} · 站点 {site or '-'} · 档案 {n} 条）"
        return "DRYRUN 聚焦输入框"
    # 层0: 当前键盘焦点/鼠标命中已是可输入元素 -> 最高新鲜度证据, 直接保留(不覆盖)
    uia_ok = _uia_ready()   # UIA 熔断冷却中: 跳过全部 UIA 扫描层, 且不盲点击(防焦点被点丢)
    if HAS_UIA and uia_ok and info is not None:
        mouse_arg = (mx, my) if inside else None
        label = _uia_call(_uia_current_focus_impl, info[0], mouse_arg, timeout=8.0)
        if label:
            text, caret, _ = _focus_readback(info, clip=True)
            return {"msg": f"已聚焦: {label}（当前）", "text": text, "caret": caret}
    # 1) 语义档案回放
    if HAS_UIA and uia_ok:
        label = _uia_call(_uia_recorded_impl, profiles, name, site, timeout=25.0)
        if label:
            text, caret, _ = _focus_readback(info, clip=True)
            return {"msg": f"已聚焦: {label}", "text": text, "caret": caret}
    # 2) 记忆位置点击 (仅位置档案带页面级作用域: 跨页自动失效并提示, 防布局漂移误点)
    info = _fg_info()
    skipped_page = False
    if info is not None:
        win_rect, cls, _t2, exe = info
        for p in profiles:
            if not _profile_matches(p, exe, site) or not isinstance(p.get("point"), dict):
                continue
            pk = (p.get("page_key") or "").strip()
            if pk:
                if not page_key or pk != page_key:
                    skipped_page = True  # 位置属于另一个页面: 布局已变, 不套用
                    continue
            if _replay_point(p["point"], win_rect):
                text, caret, _ = _focus_readback(info, clip=True)
                return {"msg": "已按记忆位置点击输入框", "text": text, "caret": caret}
    # 3) UIA 通用打分 + 相邻动作语义
    if HAS_UIA and uia_ok:
        label = _uia_call(_uia_composer_impl, profiles, name, site, timeout=30.0)
        if label:
            text, caret, _ = _focus_readback(info, clip=True)
            return {"msg": f"UIA 已聚焦: {label}", "text": text, "caret": caret}
    tail = "（已记录的位置属于另一个页面，未套用）" if skipped_page else ""
    if not uia_ok:
        # UIA 熔断期间不盲点击: 鼠标可能停在任意位置, 点下去会把前台焦点偷走(光标消失假象)。
        # 保留当前焦点直接读回(Win32 毫秒级), 打字/定位由锚定协议自愈。
        log("UIA 熔断冷却中: 跳过聚焦扫描与盲点击, 保留当前焦点")
        text, caret, _ = _focus_readback(info, clip=False)
        return {"msg": "输入框定位暂不可用，已保留当前焦点" + tail, "text": text, "caret": caret}
    log("全部通道未命中(当前焦点/档案/记忆位置/UIA), 兜底鼠标当前位置点击")
    # 4) 兜底
    mouse_click()
    text, caret, _ = _focus_readback(info, clip=True)
    return {"msg": "未定位到输入框，已在鼠标当前位置点击" + tail, "text": text, "caret": caret}


# ---------------------------------------------------------------- 遥控模式 (Spatial Navigation)
# 手机当电视遥控器: 方向键在电脑前台窗口内按空间位置移动焦点, OK 激活, 返回键 = Esc。
#   - 元素坐标来自 UIA 树 (Chromium 网页 / 原生软件都暴露无障碍树, 一套算法通吃)
#   - 方向评分对齐 WICG Spatial Navigation: 候选须在方向象限内, 且
#     「副轴与当前元素有重叠区间」或「中心落在 ±45° 方向锥内」, 按入口点距离取最优
#   - 焦点可视化: 交由应用原生 focus ring——nav 成功后注入 Ctrl 单按触发键盘模态
#     (等效 Tab 选中效果); 不再叠加自绘高亮框(与原生提示重复, 已按需求移除)
# 与聚焦输入框层共用专职 UIA 线程。

SPATIAL_MAX_NODES = 2500      # 空间导航单次扫描节点上限
SPATIAL_CACHE_TTL = 3.0       # 候选缓存有效期(秒): 连按时零扫描, 超时/换窗口才重扫
SPATIAL_TOPK = 6              # 每次方向键的试链长度: 最优不行自动试次优
SPATIAL_SCAN_BUDGET = 2.5     # 单次扫描时间上限(秒): 超时用已有候选, 防大页面卡死

_SPATIAL_CACHE = {"target": None, "hwnd": None, "items": [], "ts": 0.0,
                  "focus": None, "target_rect": None,
                  "popups": [], "last_rescan": 0.0}   # 仅专职 UIA 线程读写, 无需锁
# target = 遥控目标窗口 hwnd (remote_on 时记录): 扫描目标锁死, 与实时前台无关;
# focus = (焦点ctrl, rect, rid) 导航起点缓存: 方向键省掉 GetFocusedControl 往返,
# 由 _hl_tick 与每次成功移动刷新, 手动点选后 ≤120ms 自动跟上;
# popups = 弹层容器矩形(Menu/List), 焦点在弹层内时导航限定弹层, 不再乱跳进聊天区


class _FocusBox:
    """Tab 式选中效果框: Win32 原生 layered 窗口。UpdateLayeredWindow 一次提交
    位置+内容(原子), 无 Tk transparentcolor 的合成残影; 点击穿透/不抢焦点/置顶。
    独立线程跑消息泵, 控制线程 PostMessage 通知画框。

    视觉规范: 主色 #38A5FF, 2px 边框, 柔和外发光, 移动带插值动画, 全程点击穿透,
    按显示器 DPI 缩放(per-monitor v2)。焦点框跟的是语义焦点矩形, 不是鼠标坐标。"""

    COLOR_KEY = 0x030201
    BORDER_BGR = 0xFFA538   # COLORREF=0x00BBGGRR → RGB(0x38,0xA5,0xFF) = #38A5FF
    PAD = 2                 # 外扩极小: Tab 式紧贴元素边缘, 只留 2px 给抗锯齿
    LINE_W = 2              # 边框厚度(2px, 按 DPI 缩放)
    RADIUS = 3              # 圆角半径(逻辑像素, 小而紧凑)
    GLOW = 0.0              # 发光关闭: Tab 式聚焦框无光晕
    ANIM_MS = 0             # 动画关闭: 直接跳到目标位置, 零延迟
    ANIM_FRAME = 15         # 动画帧间隔(已禁用, 保留字段兼容)

    def __init__(self) -> None:
        self._thread = None
        self._hwnd = None
        self._pending = None
        self._lock = threading.Lock()
        self._cur = None        # 当前已绘制矩形(动画中间态)
        self._anim = False

    def _run(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            k32 = ctypes.windll.kernel32

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", ctypes.c_void_p),
                            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                            ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                            ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                            ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p)]

            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, ctypes.c_uint,
                                         ctypes.c_ulonglong, ctypes.c_longlong)
            user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                             ctypes.c_ulonglong, ctypes.c_longlong]
            user32.DefWindowProcW.restype = ctypes.c_longlong
            WM_APP_SHOW = 0x8000 + 1
            WM_APP_HIDE = 0x8000 + 2
            WM_CLOSE_DONE = 0x8000 + 3
            WM_APP_TICK = 0x8000 + 4
            self_c = self

            def wndproc(hwnd, msg, wp, lp):
                if msg == WM_APP_SHOW and self_c._pending:
                    try:
                        self_c._paint(hwnd, self_c._pending, user32, gdi32)
                    except Exception:
                        pass
                    return 0
                if msg == WM_APP_TICK:
                    try:
                        self_c._tick(hwnd, user32, gdi32)
                    except Exception:
                        pass
                    return 0
                if msg == 0x0113:   # WM_TIMER: 动画续帧
                    try:
                        self_c._tick(hwnd, user32, gdi32)
                    except Exception:
                        pass
                    return 0
                if msg == WM_APP_HIDE:
                    self_c._anim = False
                    self_c._cur = None
                    user32.ShowWindow(hwnd, 0)   # SW_HIDE
                    return 0
                if msg == 0x0010:   # WM_CLOSE
                    user32.PostQuitMessage(0)
                    return 0
                return user32.DefWindowProcW(hwnd, msg, wp, lp)

            proc_ref = WNDPROC(wndproc)
            self._proc_ref = proc_ref   # 防 GC
            hinst = k32.GetModuleHandleW(None)
            cls = WNDCLASSW()
            cls.lpfnWndProc = ctypes.cast(proc_ref, ctypes.c_void_p).value
            cls.hInstance = hinst
            cls.lpszClassName = "KZQFocusBox"
            cls.hbrBackground = None
            if not user32.RegisterClassW(ctypes.byref(cls)):
                log("FocusBox RegisterClass 失败")
                return
            WS_POPUP = 0x80000000
            # LAYERED|TRANSPARENT(点击穿透)|NOACTIVATE|TOOLWINDOW|TOPMOST
            ex = 0x80000 | 0x20 | 0x8000000 | 0x80 | 0x8

            # 单实例: 之前每重启一次服务就多一个焦点框窗口, 旧的还停在老位置继续画,
            # 用户看到屏幕上好几个蓝框、位置还是过期的 —— 这是"框乱飘/框太大"的
            # 主要来源之一。开机先清掉本进程留下的旧窗口。
            stale = []
            _fn = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

            def _collect(h, _l):
                try:
                    buf = ctypes.create_unicode_buffer(128)
                    user32.GetClassNameW(h, buf, 128)
                    if buf.value == "KZQFocusBox":
                        stale.append(h)
                except Exception:
                    pass
                return True

            try:
                user32.EnumWindows(_fn(_collect), 0)
                for h in stale:
                    user32.PostMessageW(h, 0x8002, 0, 0)    # WM_APP_HIDE
                    user32.PostMessageW(h, 0x0010, 0, 0)    # WM_CLOSE: 让它自己退出
            except Exception:
                pass
            if stale:
                log(f"FocusBox 清理旧窗口 {len(stale)} 个")

            hwnd = user32.CreateWindowExW(ex, "KZQFocusBox", None, WS_POPUP,
                                          0, 0, 1, 1, None, None, hinst, None)
            if not hwnd:
                log("FocusBox CreateWindow 失败")
                return
            with self._lock:
                self._hwnd = hwnd
            # 注意: 不调 SetLayeredWindowAttributes——它与 UpdateLayeredWindow 互斥
            # (MSDN: 调过 SetLWA 后 ULW 会一直 err=87), 透明由 ULW_ALPHA 位图实现
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            log(f"FocusBox 线程退出: {e}")

    def _tick(self, hwnd, user32, gdi32) -> None:
        """动画帧: 从 _cur 向 _pending 插值, 到达后停止"""
        if not self._anim or not self._cur or not self._pending:
            return
        cur, tgt = self._cur, self._pending
        k = 0.55   # 缓动系数: 前快后慢, 视觉上像"吸"过去
        nxt = tuple(cur[i] + (tgt[i] - cur[i]) * k for i in range(4))
        done = all(abs(nxt[i] - tgt[i]) < 1.2 for i in range(4))
        if done:
            nxt = tuple(tgt)
            self._anim = False
        self._cur = nxt
        self._paint(hwnd, nxt, user32, gdi32)
        if self._anim:
            user32.SetTimer(hwnd, 1, self.ANIM_FRAME, None)
        else:
            user32.KillTimer(hwnd, 1)

    def _scale(self) -> float:
        """按窗口所在显示器的 DPI 缩放(96 = 1.0)"""
        try:
            hwnd = self._hwnd
            if not hwnd:
                return 1.0
            user32 = ctypes.windll.user32
            try:
                dpi = user32.GetDpiForWindow(hwnd) or 96
            except Exception:
                hdc = user32.GetDC(None)
                dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88) or 96   # LOGPIXELSX
                user32.ReleaseDC(None, hdc)
            return max(1.0, min(3.0, dpi / 96.0))
        except Exception:
            return 1.0

    def _paint(self, hwnd, rect, user32, gdi32) -> None:
        s = self._scale()
        pad = int(round(self.PAD * s))
        line = max(2, int(round(self.LINE_W * s)))
        radius = self.RADIUS * s
        x = int(rect[0]) - pad
        y = int(rect[1]) - pad
        w = max(12, int(rect[2] - rect[0]) + pad * 2)
        h = max(12, int(rect[3] - rect[1]) + pad * 2)

        class BMIH(ctypes.Structure):
            _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                        ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                        ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                        ("biSizeImage", ctypes.c_uint32), ("xPPM", ctypes.c_int32),
                        ("yPPM", ctypes.c_int32), ("clrUsed", ctypes.c_uint32),
                        ("clrImportant", ctypes.c_uint32)]

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class SIZE(ctypes.Structure):
            _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

        bi = BMIH()
        bi.biSize = ctypes.sizeof(BMIH)
        bi.biWidth, bi.biHeight = w, -h   # top-down
        bi.biPlanes, bi.biBitCount = 1, 32
        bits = ctypes.c_void_p()
        hdc_screen = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(hdc_screen)
        hbmp = gdi32.CreateDIBSection(hdc_screen, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
        gdi32.SelectObject(mem_dc, hbmp)
        buf = ctypes.cast(bits, ctypes.POINTER(ctypes.c_ubyte))
        ctypes.memset(bits, 0, w * h * 4)

        # #38A5FF → BGRA 字节序: Tab 式紧凑边框, 无发光
        r0, g0, b0 = 0x38, 0xA5, 0xFF
        # 简化渲染: 只画圆角矩形 2px 实线边框, 无发光/无阴影
        # 比之前的逐像素 SDF 计算快很多, 且视觉干净无残影
        def ring_alpha(px, py):
            """返回边框不透明度 0..1"""
            cx = px + 0.5
            cy = py + 0.5
            dx = min(cx - pad, (w - pad) - cx)
            dy = min(cy - pad, (h - pad) - cy)
            if dx < radius and dy < radius:
                ax = radius - dx
                ay = radius - dy
                dist = radius - (ax * ax + ay * ay) ** 0.5
            else:
                dist = min(dx, dy)
            if dist < 0.0:
                return 0.0   # 外部: 完全透明(无发光)
            if dist <= line:
                return 1.0   # 边框区域: 完全不透明
            if dist < line + 0.75:
                return max(0.0, 1.0 - (dist - line) / 0.75)  # 抗锯齿过渡
            return 0.0       # 内部: 完全透明

        for py in range(h):
            row = py * w
            for px in range(w):
                a = ring_alpha(px, py)
                if a <= 0.004:
                    continue
                o = (row + px) * 4
                buf[o] = b0
                buf[o + 1] = g0
                buf[o + 2] = r0
                buf[o + 3] = int(min(1.0, a) * 255)

        # UpdateLayeredWindow 原子提交位置+内容 (零残影)
        pt_dst = POINT(x, y)
        pt_src = POINT(0, 0)
        size = SIZE(w, h)
        blend = ctypes.c_ulong(0x01FF0000)   # BLENDFUNCTION(AC_SRC_OVER,0,255,AC_SRC_ALPHA) 打包
        ok = user32.UpdateLayeredWindow(hwnd, hdc_screen, ctypes.byref(pt_dst), ctypes.byref(size),
                                        mem_dc, ctypes.byref(pt_src), 0, ctypes.byref(blend), 2)   # ULW_ALPHA
        if not ok and not getattr(self, "_warned", False):
            self._warned = True
            log(f"FocusBox UpdateLayeredWindow 失败 err={ctypes.GetLastError()}")
        user32.ShowWindow(hwnd, 5)   # SW_SHOW
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, hdc_screen)

    def show(self, rect) -> None:
        """画框到 rect。直接跳到位, 不做插值动画。

        之前做过一版 85ms 缓动动画, 结果: 连续按方向键时 timer 不停被重置,
        框长期停在"中间插值位置", 用户看到的就是"框追不上焦点/框画在空地上"。
        遥控场景要的是跟手和准确, 不是丝滑 —— 位置永远等于最新焦点矩形。
        """
        self._ensure()
        self._pending = tuple(float(v) for v in rect[:4])
        self._cur = self._pending
        self._anim = False
        with self._lock:
            hwnd = self._hwnd
        if not hwnd:
            return
        ctypes.windll.user32.PostMessageW(hwnd, 0x8001, 0, 0)   # WM_APP_SHOW

    def hide(self) -> None:
        with self._lock:
            hwnd = self._hwnd
        self._cur = None
        self._anim = False
        if hwnd:
            ctypes.windll.user32.PostMessageW(hwnd, 0x8002, 0, 0)   # WM_APP_HIDE

    def _ensure(self) -> None:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name="kzq-box")
                self._thread.start()


FOCUS_BOX = _FocusBox()


def _spatial_rect(ctrl):
    """控件屏幕矩形 (l,t,r,b) 或 None (0 尺寸/读不到的占位节点直接丢弃)"""
    try:
        r = ctrl.BoundingRectangle
        rect = (r.left, r.top, r.right, r.bottom)
        if rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
            return None
        return rect
    except Exception:
        return None


def _focus_verify_retry(ctrl, tries: int = 3, gap: float = 0.04) -> bool:
    """SetFocus 后立即读 HasKeyboardFocus 存在竞态——Chromium 焦点更新异步,
    首次读取常拿到旧状态误判失败, 试链就会跳到次优元素(实测 B31→down 跳过 B41
    直达 L1 的根因)。40ms 间隔重试 3 次, 正常路径首次即命中零额外成本"""
    for i in range(tries):
        if _focus_verify(ctrl):
            return True
        if i < tries - 1:
            time.sleep(gap)
    return False


def _spatial_fg_for(target):
    """目标窗口的 UIA 控件: 优先 GetForegroundControl(实测对 Chromium 树更可靠,
    诊断脚本同逻辑能找到 Document 而 ControlFromHandle 不能), 校验确实是目标窗口
    防前台漂移; 不匹配(用户已切走)再退回 ControlFromHandle"""
    try:
        fgc = _UIA_AUTO.GetForegroundControl()
        if fgc is not None and int(fgc.NativeWindowHandle or 0) == int(target):
            return fgc
    except Exception:
        pass
    try:
        return _UIA_AUTO.ControlFromHandle(target)
    except Exception:
        return None


def _spatial_find_content_root(fg, vrect):
    """找浏览器/文档类应用的内容区 Document: 可见矩形与窗口可视区交集最大者。
    第一性原理: 导航域必须锁定「激活的内容区」——Chromium 的 UIA 树包含
    全部标签页的内容树(含标签栏/地址栏/非激活标签), 整窗口扫描会让方向键
    逃逸进浏览器框架(实测: right 从网格跳进别的标签页)。取可见交集最大的
    Document 即激活内容区; 找不到(文件管理器等)返回 None, 保持整窗口扫描"""
    best = None
    best_score = 0.0
    visited = 0
    try:
        CT_DOC = int(_UIA_AUTO.ControlType.DocumentControl)
    except Exception:
        return None

    def walk(ctrl, depth):
        nonlocal best, best_score, visited
        if depth > 18 or visited > 1200:
            return
        visited += 1
        try:
            if int(ctrl.ControlType) == CT_DOC:
                try:
                    off = bool(ctrl.IsOffscreen)
                except Exception:
                    off = True   # 可见性读不到: 保守排除(非激活标签常读不到)
                try:
                    aid = (ctrl.AutomationId or "").strip().lower()
                except Exception:
                    aid = ""
                try:
                    r = ctrl.BoundingRectangle
                    rect = (r.left, r.top, r.right, r.bottom)
                except Exception:
                    rect = None
                if (not off and rect and rect[2] - rect[0] > 50
                        and rect[3] - rect[1] > 50 and vrect):
                    ix = max(0, min(rect[2], vrect[2]) - max(rect[0], vrect[0]))
                    iy = max(0, min(rect[3], vrect[3]) - max(rect[1], vrect[1]))
                    s = ix * iy
                    # Chromium 是壳链结构: 顶层 view_xxx Document 是空壳, 真正的页面
                    # 内容在更深处的 RootWebArea Document——双倍权重让壳链下钻后优中选优
                    score = s * (2.0 if "rootwebarea" in aid else 1.0)
                    if s > 0 and score > best_score:
                        best_score = score
                        best = ctrl
                if "rootwebarea" in aid:
                    return   # 真内容根, 不再深入
                # 壳 Document: 继续下钻找真正的 RootWebArea(壳链只有几层)
                for ch in ctrl.GetChildren():
                    walk(ch, depth + 1)
                return
            for ch in ctrl.GetChildren():
                walk(ch, depth + 1)
        except Exception:
            return

    walk(fg, 0)
    return best if best_score > 0 else None


def _uia_zone_of(rect, profile=None, name: str = "", ct: str = "") -> str:
    """UIA 候选的区域推断（无 DOM 祖先，只能几何 + 语义名）。

    这是 CDP 不可用时的降级路径：区域粒度比 DOM Profile 粗，但仍能锁住
    TOPBAR/SIDEBAR/MAIN/COMPOSER/SEARCH/PLAYER，杜绝整页乱跳。
    """
    tr = _SPATIAL_CACHE.get("target_rect")
    if not rect or not tr:
        return "MAIN"
    wl, wt, wr, wb = tr
    ww = max(1.0, wr - wl)
    wh = max(1.0, wb - wt)
    l, t, r, b = rect
    low = (name or "").lower()
    pid = (profile or {}).get("id") or "generic"
    # 搜索框：语义名优先
    if any(k in low for k in ("search", "搜索", "查找", "filter")) and t <= wt + wh * 0.28:
        return "SEARCH"
    # 播放器：视频站点的大面积中央容器 / 控件
    if pid in ("youtube", "bilibili", "douyin"):
        if (r - l) * (b - t) > ww * wh * 0.12 and t >= wt + wh * 0.08:
            return "PLAYER"
    # 顶栏
    if t <= wt + min(110.0, wh * 0.14) and (r - l) >= ww * 0.35:
        return "TOPBAR"
    # 侧栏
    if l <= wl + ww * 0.20 and (r - l) <= ww * 0.45:
        return "SIDEBAR"
    # 输入区
    if (b - t) <= wh * 0.30 and t >= wt + wh * 0.55 and (r - l) >= ww * 0.28:
        return "COMPOSER"
    return "MAIN"


def _uia_sem_ok(ct, name, zone, profile=None, link_zones=None):
    """UIA 语义筛选（与 nav_helper.js 的 ROLE_WHITELIST 对齐）。

    规范要求：只允许 button / tab / textbox / searchbox / combobox / menuitem /
    checkbox / radio / switch / slider 进入候选；链接必须在 Profile 允许的区域内
    （默认 TOPBAR / SIDEBAR）才收。段落、标题、正文、图片、评论、推荐、广告、
    装饰/隐藏/禁用元素一律不进候选。
    返回 (ok, reason)。
    """
    role_map = {
        "Button": "button", "SplitButton": "button", "Edit": "textbox",
        "ComboBox": "combobox", "CheckBox": "checkbox", "RadioButton": "radio",
        "Slider": "slider", "TabItem": "tab", "MenuItem": "menuitem",
        "Hyperlink": "link", "Text": "text", "Image": "image",
        "ListItem": "listitem", "DataItem": "dataitem", "TreeItem": "treeitem",
        "Group": "group", "Document": "document", "Pane": "pane",
    }
    role = role_map.get(ct or "", (ct or "").lower()) or "unknown"
    if role == "link":
        if not name:
            return False, "link 无名"
        lz = link_zones
        if lz is None:
            lz = set((profile or {}).get("link_zones") or ("TOPBAR", "SIDEBAR"))
        if zone not in lz:
            return False, "link 区域外"
        return True, ""
    if role in ("textbox", "combobox", "checkbox", "radio",
                "slider", "tab", "menuitem", "group", "document"):
        return True, ""
    if role == "button":
        if not name:
            return True, ""
        # 行政/版权/法律等纯噪音信息
        if re.search(r"ICP备|公网安备|增值电信|营业执照|许可证|版权所有|©|隐私政策|使用条款|意见反馈|免责声明", name):
            return False, "noise: legal"
        # MAIN 内容区降噪: 排除视频卡片悬浮按钮、重复的卡片互动操作(点赞/投币/复制等)
        if zone == "MAIN":
            if re.search(r"^(添加至|稍后再看|添加到|watch later|add to queue)", name, re.I):
                return False, "noise: card-hover"
            if re.search(r"^(点赞|投币|转发|转推|复制|朗读|copy|like|retweet|repost|thumbs|read aloud)$", name, re.I):
                return False, "noise: card-action"
        return True, ""
    return False, f"role={role}"


def _spatial_scan_impl(fg, profile=None):
    """全树扫描候选 [(ctrl, 类型名, name, rect, focusable)]; 慢路径, 只在缓存失效时执行。
    扫描根优先取激活内容区 Document(见 _spatial_find_content_root)。
    - 语义白名单(规范): 只收可操作控件, ListItem/TreeItem/DataItem 全部剔除
      (它们在网页里是消息行/列表项/帖子卡片, 实测 B站/知乎/X 一次扫 300~550 候选的主因);
    - Hyperlink 仅保留在 Profile 允许的链接区(默认 TOPBAR/SIDEBAR)且必须有名字;
    - Group/Document 只有带 TextPattern(富文本编辑器/contenteditable)才收——
      GPT 聊天框这类 contenteditable 在 Chromium 上不是 Edit 而是带 TextPattern 的 Group;
    - IsKeyboardFocusable 在部分网页元素上一律报 False(WebView2 桥实证), 不硬滤只作排序惩罚;
    - 视口外超过 1.5 屏的候选直接丢弃(整页扫描的噪音), 视口附近保留以维持「按到底翻页」;
    - RuntimeId/矩形去重: 同一控件的包装层与真实控件不再重复计数"""
    CT = _UIA_AUTO.ControlType
    leaf_ints = {int(CT.EditControl), int(CT.ComboBoxControl), int(CT.ButtonControl),
                 int(CT.HyperlinkControl), int(CT.CheckBoxControl), int(CT.RadioButtonControl),
                 int(CT.SplitButtonControl)}
    # 语义白名单: 剔除 ListItem / TreeItem / DataItem（列表行、帖子卡片、评论项）
    focusable_ints = leaf_ints | {int(CT.TabItemControl), int(CT.MenuItemControl)}
    editor_ints = {int(CT.GroupControl), int(CT.DocumentControl)}
    link_zones = set((profile or {}).get("link_zones") or ("TOPBAR", "SIDEBAR"))
    seen_sig = set()
    rejects = {}
    TP_ID = _UIA_AUTO.PropertyId.IsTextPatternAvailableProperty
    try:
        wr = fg.BoundingRectangle
        win_area = max(1.0, float((wr.right - wr.left) * (wr.bottom - wr.top)))
        win_bottom, win_width = float(wr.bottom), float(wr.right - wr.left)
        win_top, win_height = float(wr.top), float(wr.bottom - wr.top)
        _SPATIAL_CACHE["target_rect"] = (wr.left, wr.top, wr.right, wr.bottom)
    except Exception:
        win_area = 0.0
        win_bottom, win_width = 0.0, 0.0
        win_top, win_height = 0.0, 0.0
    cands = []
    popups = []   # 弹层容器矩形(Menu/List): 焦点在其中时导航限定弹层, 不乱跳进页面内容
    visited = 0
    t_start = time.time()
    popup_ints = {int(CT.MenuControl), int(CT.ListControl)}
    # 根定位阶段放宽超时: Chromium 无障碍树懒构建, Document 下钻首次会很慢
    try:
        _UIA_AUTO.SetGlobalSearchTimeout(3.0)
    except Exception:
        pass
    root = fg
    try:
        root = _spatial_find_content_root(fg, _SPATIAL_CACHE.get("target_rect")) or fg
    except Exception:
        root = fg
    if root is fg:
        # 内容区根找不到: 窗口刚切换时 Chromium a11y 树常未同步——等 0.8s 重找一次
        time.sleep(0.8)
        try:
            root = _spatial_find_content_root(fg, _SPATIAL_CACHE.get("target_rect")) or fg
        except Exception:
            root = fg
    root_is_doc = root is not fg   # 内容区根(Document): 其内嵌 iframe/子 Document 不再作为候选
    try:
        rr = root.BoundingRectangle
        _SPATIAL_CACHE["content_rect"] = (rr.left, rr.top, rr.right, rr.bottom)
    except Exception:
        _SPATIAL_CACHE["content_rect"] = None
    if root_is_doc:
        # 内容根必须有孩子——Chromium 懒激活时 Document 可能暂时为空, 回退整窗口扫描
        try:
            if not root.GetChildren():
                log("spatial: 内容区根无孩子(无障碍树未激活?), 回退整窗口扫描")
                root = fg
                root_is_doc = False
        except Exception:
            root = fg
            root_is_doc = False

    def descend(ctrl, depth):
        nonlocal visited
        if depth > 30 or visited > SPATIAL_MAX_NODES:
            return
        visited += 1
        if visited % 96 == 0 and time.time() - t_start > SPATIAL_SCAN_BUDGET:
            log(f"spatial: 扫描超预算({SPATIAL_SCAN_BUDGET}s), 用已有候选")
            return
        try:
            cti = int(ctrl.ControlType)
        except Exception:
            return
        if cti in focusable_ints or cti in editor_ints or cti == int(CT.MenuControl):
            rect = _spatial_rect(ctrl)
            if cti == int(CT.MenuControl) and rect is not None:
                pr_area = float(rect[2] - rect[0]) * float(rect[3] - rect[1])
                if pr_area < win_area * 0.6 and rect[3] - rect[1] > 24:
                    popups.append((rect, True))   # 只有 Menu 当弹层——ListControl 在现代
                    # 网页组件库泛滥(role=listbox 的页面内列表也是), 按它限定会劫持导航
                    # (实测 B 站首页 42 个 List), 焦点逃逸用视口罚分自然缓解
            if rect is not None and (ctrl is not root):
                accept = False
                focusable = False
                if cti in editor_ints:
                    tp = False
                    try:
                        tp = bool(ctrl.GetPropertyValue(TP_ID))
                    except Exception:
                        tp = False
                    if tp:
                        # IsReadOnly 滤网只用于 Document(网页根只读)——实测 Chromium 对
                        # contenteditable Group 一律报 IsReadOnly=True, 按它滤会把
                        # GPT 聊天框这类编辑器全部误杀(实锤: fixture editor-1 ro=True)
                        ro_bad = False
                        if cti == int(CT.DocumentControl):
                            try:
                                ro_bad = ctrl.GetPropertyValue(30046) is True  # UIA_IsReadOnlyPropertyId
                            except Exception:
                                ro_bad = False
                        aid_root = False
                        try:
                            aid_root = (ctrl.AutomationId or "").strip().lower() == "rootwebarea"
                        except Exception:
                            aid_root = False
                        area = float(rect[2] - rect[0]) * float(rect[3] - rect[1])
                        if not ro_bad and not aid_root and area < win_area * 0.85 \
                                and not (root_is_doc and cti == int(CT.DocumentControl)):
                            accept = True
                            try:
                                focusable = bool(ctrl.IsKeyboardFocusable)
                            except Exception:
                                focusable = False
                    else:
                        # 空 contenteditable 无文本不暴露 TextPattern, 但可聚焦——
                        # focusable 的 Group/Document 同样收(GPT 式空编辑框可达)
                        try:
                            focusable = bool(ctrl.IsKeyboardFocusable)
                        except Exception:
                            focusable = False
                        if focusable:
                            accept = True
                else:
                    accept = True
                    try:
                        focusable = bool(ctrl.IsKeyboardFocusable)
                    except Exception:
                        focusable = False
                if accept and (rect[2] - rect[0] < 14 or rect[3] - rect[1] < 14):
                    accept = False   # <8px 的装饰/零碎元素, 遥控没有可达意义
                if accept and win_width > 0:
                    l2, t2, r2, b2 = rect
                    if win_bottom - b2 <= 6 and (b2 - t2) <= 32 and (r2 - l2) >= win_width * 0.6:
                        # 底部状态栏特征: 贴窗口底缘的矮横条(如状态条/托盘条)——
                        # down 方向的几何死区, 遥控导航几乎从不想去, 直接排除
                        accept = False
                        rejects["状态栏"] = rejects.get("状态栏", 0) + 1
                if accept:
                    try:
                        name = (ctrl.Name or "").strip()
                    except Exception:
                        name = ""
                    ctn = _ct_name(ctrl)
                    zone = _uia_zone_of(rect, profile, name, ctn)
                    ok, why = _uia_sem_ok(ctn, name, zone, profile, link_zones)
                    if not ok:
                        # 语义白名单外的元素不进候选(正文/标题/图片/列表行/区域外链接)
                        rejects[why] = rejects.get(why, 0) + 1
                        accept = False
                    elif win_height > 0:
                        # 视口外超过 1.5 屏: 整页扫描噪音, 丢掉; 视口附近保留,
                        # 这样「按到底」仍能 SetFocus 触发浏览器滚动翻页
                        l2, t2, r2, b2 = rect
                        if b2 < win_top - win_height * 1.5 or t2 > win_bottom + win_height * 1.5:
                            rejects["offscreen"] = rejects.get("offscreen", 0) + 1
                            accept = False
                if accept:
                    # 同控件去重: 包装层与真实控件矩形几乎重合时只留一个
                    sig = (int(rect[0]) // 2, int(rect[1]) // 2,
                           int(rect[2]) // 2, int(rect[3]) // 2, ctn)
                    if sig in seen_sig:
                        rejects["dup"] = rejects.get("dup", 0) + 1
                        accept = False
                    else:
                        seen_sig.add(sig)
                if accept:
                    cands.append((ctrl, ctn, name, rect, focusable))
                    if cti in leaf_ints:
                        return   # 语义控件是叶子, 内部不再有独立焦点目标
        try:
            for child in ctrl.GetChildren():
                descend(child, depth + 1)
        except Exception:
            pass

    try:
        _UIA_AUTO.SetGlobalSearchTimeout(1.5)   # 遍历中消失元素的属性读取不卡默认 10s
    except Exception:
        pass
    descend(root, 0)
    if not cands and root is not fg:
        # 内容区根扫不到候选(server 冷启动时 Chromium 无障碍树尚未激活, 树残缺):
        # 降级整窗口重扫一次——有候选总比没有强, 下次重扫自然恢复内容区
        visited = 0
        descend(fg, 0)
        log(f"spatial: 内容区候选为空, 降级整窗口 -> 候选={len(cands)}")
    try:
        _UIA_AUTO.SetGlobalSearchTimeout(10.0)
    except Exception:
        pass
    if visited >= SPATIAL_MAX_NODES:
        log(f"spatial 扫描截断: 节点达到 {SPATIAL_MAX_NODES}, 候选 {len(cands)}")
    zcount = {}
    for _c, _ctn, _n, _r, _f in cands:
        _z = _uia_zone_of(_r, profile, _n, _ctn)
        zcount[_z] = zcount.get(_z, 0) + 1
    log(f"spatial 扫描: visited={visited} 候选={len(cands)} 弹层={len(popups)} "
        f"区域={zcount} 剔除={rejects} "
        f"root={'内容区' if root is not fg else (fg.ClassName or '')[:30]!r}")
    _SPATIAL_CACHE["popups"] = popups
    _SPATIAL_CACHE["rejects"] = rejects
    _SPATIAL_CACHE["zones"] = zcount
    return cands


def _spatial_candidates_impl(target, force=False, profile=None):
    """候选获取(缓存优先): 目标窗口 SPATIAL_CACHE_TTL 秒内零扫描直取——
    方向键连发时单步只剩纯几何计算 + SetFocus, 不再每次全树跨进程扫描。
    扫描根固定 ControlFromHandle(target): 遥控目标窗口, 与实时前台无关。
    profile 变化(换站)必须让缓存失效: 语义白名单/区域规则随站点变。
    返回 (items, cached)。仅专职 UIA 线程内调用(缓存 dict 无锁)"""
    if not target:
        return [], False
    pid = (profile or {}).get("id") or ""
    now = time.time()
    if (not force and target == _SPATIAL_CACHE["hwnd"]
            and pid == _SPATIAL_CACHE.get("profile_id", "")
            and now - _SPATIAL_CACHE["ts"] < SPATIAL_CACHE_TTL):
        return _SPATIAL_CACHE["items"], True
    fg = _spatial_fg_for(target)
    if fg is None:
        return [], False
    items = _spatial_scan_impl(fg, profile)
    _SPATIAL_CACHE["hwnd"] = target
    _SPATIAL_CACHE["items"] = items
    _SPATIAL_CACHE["profile_id"] = pid
    _SPATIAL_CACHE["ts"] = now
    return items, False


def _spatial_shift_cache(items, dx, dy):
    """滚动补偿: 焦点元素移动引发滚动后, 按焦点元素新旧矩形差值整体平移缓存矩形,
    让下一键的几何计算仍对得上(零额外 COM 调用)。候选为 5 元组(ctrl,ct,name,rect,f)"""
    if abs(dx) < 2 and abs(dy) < 2:
        return items
    return [(c, ct, nm, (r[0] + dx, r[1] + dy, r[2] + dx, r[3] + dy), f)
            for c, ct, nm, r, f in items]


def _spatial_focus_feedback(rect) -> None:
    """焦点视觉反馈双通道(选中效果):
    ① 鼠标指针移到元素中心——应用的悬停效果 100% 可见(网页 :hover / 原生悬停高亮),
       指针本身也是位置指示, 不点击无副作用;
    ② Ctrl 单按注入键盘模态, 让浏览器显示原生 focus ring(等效 Tab 选中效果)"""
    try:
        cx = int((rect[0] + rect[2]) / 2.0)
        cy = int((rect[1] + rect[3]) / 2.0)
        pyautogui.moveTo(cx, cy, duration=0)
    except Exception:
        pass
    try:
        _KB.press(KbKey.ctrl)
        _KB.release(KbKey.ctrl)
    except Exception:
        pass


def _spatial_rank(start_rect, cands, direction, k=SPATIAL_TOPK,
                  profile=None, zone=None, free=False):
    """简化版方向评分: 严格遵循按键方向, 选最近的元素。

    核心原则:
    1. 候选中心必须在按键方向的前方(fwd > 0)
    2. 主轴距离为第一排序键, 副轴偏移为第二排序键
    3. 水平移动时严格约束同行(drift < 1.0), 垂直移动时允许较大副轴偏移
    4. 副轴有重叠的元素优先(在"正前方"的直觉)
    5. 罚分项尽量少, 只保留必要的: 视口外、不可聚焦、无名容器
    """
    l1, t1, r1, b1 = start_rect
    cx1, cy1 = (l1 + r1) / 2.0, (t1 + b1) / 2.0
    vertical = direction in ("up", "down")
    trect = _SPATIAL_CACHE.get("target_rect")
    vp_h = (trect[3] - trect[1]) if trect else 0.0
    vp_w = (trect[2] - trect[0]) if trect else 0.0
    lock_zone = None if free else (zone or None)
    if lock_zone == "DIALOG":
        lock_zone = None   # 弹层由 _spatial_nav_impl 的 popup 矩形限定, 不按几何区域切
    scored = []
    for ctrl, ct, name, rect, focusable in cands:
        if rect == start_rect:
            continue
        if lock_zone and _uia_zone_of(rect, profile, name, ct) != lock_zone:
            continue
        l2, t2, r2, b2 = rect
        cx2, cy2 = (l2 + r2) / 2.0, (t2 + b2) / 2.0
        if vertical:
            fwd = (cy2 - cy1) if direction == "down" else (cy1 - cy2)
            main = (t2 - b1) if direction == "down" else (t1 - b2)
            side = max(0.0, l1 - r2, l2 - r1)
            side_center = cx2 - cx1
        else:
            fwd = (cx2 - cx1) if direction == "right" else (cx1 - cx2)
            main = (l2 - r1) if direction == "right" else (l1 - r2)
            side = max(0.0, t1 - b2, t2 - b1)
            side_center = cy2 - cy1
        if fwd <= 0:
            continue
        # 横向 overflow 排除: 轮播图在窗口外, 物理不可达
        if trect and (rect[2] <= trect[0] or rect[0] >= trect[2]):
            continue
        # 水平移动: 严格同行约束, 防止斜向跳行
        if not vertical:
            start_h = max(1.0, b1 - t1)
            drift = abs(side_center) / start_h
            if drift > 1.0:
                continue   # 超过 1 倍自身高度: 不同行, 不是水平方向的"下一个"
        # 垂直移动: 用宽松的方向锥(60°), 允许较大水平偏移
        else:
            if side > 0 and abs(side_center) > fwd * 1.73:
                continue   # tan(60°)≈1.73, 超过 60° 锥外排除
        gap = max(0.0, main)
        # 副轴重叠量: 正前方的元素 overlap > 0, 是用户直觉的"下一个"
        if vertical:
            overlap = max(0.0, min(r1, r2) - max(l1, l2))
        else:
            overlap = max(0.0, min(b1, b2) - max(t1, t2))
        # 评分: 主轴距离 + 副轴偏移 * 权重, 有重叠的加分
        score = gap + abs(side_center) * 0.3
        if overlap > 4:
            score -= min(50.0, overlap * 0.2)   # 正前方优先
        # 视口外推迟
        if trect and not (trect[0] <= cx2 <= trect[2] and trect[1] <= cy2 <= trect[3]):
            score += max(vp_h, vp_w) * 1.5
        # 不可聚焦降权
        if not focusable:
            score += 400.0
        # 无名容器降权
        if ct == "Group" and not name:
            score += 300.0
        scored.append((score, ctrl, ct, rect))
    scored.sort(key=lambda t: t[0])
    return scored[:k]


def _spatial_editable_focus(ctrl):
    """(是否真可编辑, 标签)。只读文本区(消息正文/文章)不算编辑态——按 TextPattern
    判定会把方向键发进没有光标的只读区, 毫无反应 = 「Tab 后无法控制」的根因。
    编辑器特征词表识别 contenteditable 怪癖(Chromium 对 prosemirror/tiptap 报
    IsReadOnly=True, 不能只看只读属性)。"""
    try:
        fct = _ct_name(ctrl)
        editable = False
        if fct == "Edit":
            try:
                editable = ctrl.GetPropertyValue(30046) is not True   # 非只读
            except Exception:
                editable = True
        if not editable and fct in ("Group", "Document"):
            try:
                blob = f"{ctrl.AutomationId or ''} {ctrl.ClassName or ''} {ctrl.Name or ''}".lower()
            except Exception:
                blob = ""
            if any(h in blob for h in FOCUS_EDITOR_HINTS):
                editable = True
        try:
            label = (ctrl.Name or fct or "")[:24]
        except Exception:
            label = fct
        return (editable, label)
    except Exception:
        return (False, "")


def _spatial_editing_impl():
    """编辑态判定(不发按键): 当前焦点是否为真可编辑控件。专职 UIA 线程内执行"""
    fok = _SPATIAL_CACHE.get("focus")
    ctrl = fok[0] if fok else None
    if ctrl is None:
        try:
            ctrl = _UIA_AUTO.GetFocusedControl()
        except Exception:
            ctrl = None
    if ctrl is None:
        return False
    return bool(_spatial_editable_focus(ctrl)[0])


def _spatial_zone_first_impl(zone=None, profile=None):
    """切区/弹窗恢复用: 聚焦指定区域内第一个视口内候选。返回 (label, rect) 或 None。
    zone 为空时退化为整窗第一个候选。专职 UIA 线程内执行"""
    target = _SPATIAL_CACHE["target"]
    if not target:
        return None
    fg = _spatial_fg_for(target)
    if fg is None:
        _SPATIAL_CACHE["target"] = None
        return None
    try:
        wr = fg.BoundingRectangle
        vrect = (wr.left, wr.top, wr.right, wr.bottom)
        _SPATIAL_CACHE["target_rect"] = vrect
    except Exception:
        vrect = None
    items, _ = _spatial_candidates_impl(target, profile=profile)
    if not items:
        items, _ = _spatial_candidates_impl(target, force=True, profile=profile)
    pool = []
    for ctrl, ct, name, cr, _focusable in items:
        if zone and _uia_zone_of(cr, profile, name, ct) != zone:
            continue
        if vrect:
            if cr[2] <= vrect[0] or cr[0] >= vrect[2] or cr[3] <= vrect[1] or cr[1] >= vrect[3]:
                continue
        key = (cr[0] - vrect[0] if vrect else cr[0]) + (cr[1] - vrect[1] if vrect else cr[1])
        pool.append((key, ctrl, ct, cr))
    pool.sort(key=lambda t: t[0])
    for _k, ctrl, ct, cr in pool:
        try:
            label = (ctrl.Name or ct or "")[:30]
        except Exception:
            label = ct
        if DRY:
            log(f"DRYRUN 区域首焦点 {zone} -> {label} rect={cr}")
            return (label, cr)
        try:
            ctrl.SetFocus()
        except Exception:
            continue
        if not _focus_verify_retry(ctrl):
            continue
        _SPATIAL_CACHE["focus"] = (ctrl, cr, None)
        _spatial_focus_feedback(cr)
        FOCUS_BOX.show(cr)
        return (label, cr)
    return None


def _spatial_nav_impl(direction, force_spatial=False, profile=None,
                      zone=None, mode=None):
    """方向键移动焦点; 专职 UIA 线程内执行。返回 (label, rect 或 None, moved)。
    快路径像 Tab 键: 起点复用缓存焦点元素(省 GetFocusedControl), SetFocus+轻验证
    立即返回。候选走缓存(TTL 2s/同窗口); TopK 试链逐个 SetFocus+验证, 不可聚焦/
    已消失的自动落到次优; 滚动补偿按焦点矩形差值平移缓存, 连按翻页几何不漂移。
    区域锁: zone 非空且 mode != 'free' 时只在区域内评分, 区域内无可达目标即停
    (写 _SPATIAL_CACHE['last_reason'] = 'zone-empty' / 'zone-end'), 不跨区域兜底。
    Raw/Spatial 智能模式(空间导航的核心交互细节): 焦点在文本上下文(输入框/
    contenteditable/只读文本区)时方向键原样发给应用——移文字光标/滚动页面,
    而不是跳控件; 想跳控件按 Tab, 或长按方向键(连发段 force_spatial=True)
    强制空间导航直接往外跳"""
    free = (mode == "free") or not zone
    target = _SPATIAL_CACHE["target"]
    if not target:
        return ("", None, False)
    fg = _spatial_fg_for(target)
    if fg is None:
        _SPATIAL_CACHE["target"] = None   # 目标窗口已关闭: 遥控失效, 待下次开启重定位
        return ("", None, False)
    # Raw/Spatial 智能判定: 只认「真可编辑」——普通 Edit(非只读)或编辑器特征
    # (contenteditable/prosemirror/tiptap 等, 见 FOCUS_EDITOR_HINTS)。
    # 不再用「带 TextPattern」判定: 只读文本区(消息正文/文章)也带 TP, 按它判会
    # 把方向键发进没有光标的只读区, 毫无反应 = 用户说的「Tab 后无法控制」
    fok = _SPATIAL_CACHE.get("focus")
    if fok and fok[0] is not None and not force_spatial:
        editable, flabel = _spatial_editable_focus(fok[0])
        if editable:
            _srv_tap(direction)   # 原样发物理方向键: 移文字光标/滚动页面
            return (f"[文本区·方向键] {flabel}", None, True)
    start_rect = None
    focus_entry = _SPATIAL_CACHE.get("focus")
    if focus_entry:
        try:
            rr = focus_entry[0].BoundingRectangle   # 复用上一步焦点: 1 次 COM 即得新起点
            start_rect = (rr.left, rr.top, rr.right, rr.bottom)
            if start_rect[2] - start_rect[0] <= 0 or start_rect[3] - start_rect[1] <= 0:
                start_rect = None
        except Exception:
            start_rect = None
    if start_rect is None:
        # 无缓存起点: 读真实焦点; 再不行用目标窗口可视中心做虚拟原点
        focused = None
        try:
            focused = _UIA_AUTO.GetFocusedControl()
        except Exception:
            pass
        start_rect = _spatial_rect(focused) if focused is not None else None
        if start_rect is None:
            try:
                wr = fg.BoundingRectangle
                cx, cy = (wr.left + wr.right) / 2.0, (wr.top + wr.bottom) / 2.0
            except Exception:
                return ("", None, False)
            start_rect = (cx - 1, cy - 1, cx + 1, cy + 1)
    if _SPATIAL_CACHE.pop("stale", False):
        items, cached = _spatial_candidates_impl(target, force=True, profile=profile)
    else:
        items, cached = _spatial_candidates_impl(target, profile=profile)
    # 弹层 containment(防乱跳核心): Menu 弹出即无条件限定(菜单只在弹出时存在);
    # List(下拉列表)仅在焦点中心已在其内时限定——页面内普通 <ul> 也是 ListControl,
    # 无条件限定会把导航永久劫持到侧栏列表。限定后无候选则回退全池
    sl, st, sr, sb = start_rect
    scx, scy = (sl + sr) / 2.0, (st + sb) / 2.0
    limit = None
    for pr, is_menu in (_SPATIAL_CACHE.get("popups") or []):
        if is_menu:
            limit = pr   # Menu: 无条件
            break
        if pr[0] <= scx <= pr[2] and pr[1] <= scy <= pr[3]:
            limit = pr   # List: 焦点已在内
            break
    if limit is not None:
        filtered = [c for c in items if limit[0] <= c[3][0] and c[3][2] <= limit[2]
                    and limit[1] <= c[3][1] and c[3][3] <= limit[3]]
        if filtered:
            items = filtered
            start_rect = (max(sl, limit[0]), max(st, limit[1]),
                          min(sr, limit[2]), min(sb, limit[3]))   # 起点钳到弹层内
            sl, st, sr, sb = start_rect
            scx, scy = (sl + sr) / 2.0, (st + sb) / 2.0
    # 试链最多两轮: 第一轮全失败(候选全部不可聚焦/已消失/方向到顶)则强制重扫再算
    # 一轮——重扫后矩形更新, 最优解可能完全不同。重扫有 1s 节流: 「到底/到边」时
    # 连按不再每键都全树重扫(250-600ms 的卡顿)
    eff_zone = "DIALOG" if limit is not None else zone
    eff_free = free and limit is None
    zone_pool = items
    if not eff_free and eff_zone != "DIALOG":
        zone_pool = [c for c in items
                     if _uia_zone_of(c[3], profile, c[2], c[1]) == eff_zone]
    _SPATIAL_CACHE["last_reason"] = None if zone_pool else (
        "zone-empty" if not eff_free else "empty")
    if DRY:
        log(f"空间导航 {direction}: 区域={eff_zone} free={eff_free} "
            f"池={len(zone_pool)}/{len(items)}")
    for attempt in range(2):
        ranked = _spatial_rank(start_rect, zone_pool, direction,
                               profile=profile, zone=eff_zone, free=eff_free)
        for score, ctrl, ct, rect in ranked:
            try:
                label = (ctrl.Name or ct or "")[:30]
            except Exception:
                label = ct
            if limit is not None:
                label = f"{label}｜弹层内·返回键关闭"
            if DRY:
                log(f"DRYRUN 空间导航 {direction} -> {label} rect={rect}")
                return (label, rect, True)   # 不动真焦点, 便于验证几何
            try:
                ctrl.SetFocus()
            except Exception:
                continue   # 不可聚焦/元素已消失: 试次优
            if not _focus_verify_retry(ctrl):
                continue   # SetFocus 静默失败/竞态未生效: 重试验证后仍失败才试次优
            # 滚动补偿(每次移动都做, 可靠不依赖 tick 的同元素推断): 焦点元素现读矩形与
            # 候选缓存矩形之差 = SetFocus 引发的页面/容器滚动量 → 候选缓存整体平移。
            # 固定定位的 composer(滚动时矩形不变)会让 tick 推断失效, 这里用元素自身对比兜底
            try:
                rr = ctrl.BoundingRectangle
                nrect = (rr.left, rr.top, rr.right, rr.bottom)
            except Exception:
                nrect = None
            if nrect and nrect[2] - nrect[0] > 0 and nrect[3] - nrect[1] > 0:
                dx, dy = nrect[0] - rect[0], nrect[1] - rect[1]
                if cached and (abs(dx) >= 2 or abs(dy) >= 2):
                    _SPATIAL_CACHE["items"] = _spatial_shift_cache(_SPATIAL_CACHE["items"], dx, dy)
                _SPATIAL_CACHE["focus"] = (ctrl, nrect, None)   # 下一键的起点; _hl_tick 会补 rid
                _spatial_focus_feedback(nrect)   # 选中效果: 鼠标悬停 + 原生 focus ring
                FOCUS_BOX.show(nrect)
                return (label, nrect, True)
            _SPATIAL_CACHE["focus"] = (ctrl, rect, None)
            _spatial_focus_feedback(rect)
            FOCUS_BOX.show(rect)
            return (label, rect, True)
        if attempt == 0:
            now = time.time()
            if now - _SPATIAL_CACHE.get("last_rescan", 0.0) < 1.0:
                break   # 刚重扫过仍空: 到底/到边, 连按不再重复全树扫描
            _SPATIAL_CACHE["last_rescan"] = now
            items, cached = _spatial_candidates_impl(target, force=True, profile=profile)
            zone_pool = items
            if not eff_free and eff_zone != "DIALOG":
                zone_pool = [c for c in items
                             if _uia_zone_of(c[3], profile, c[2], c[1]) == eff_zone]
            if zone_pool and _SPATIAL_CACHE.get("last_reason") == "zone-empty":
                _SPATIAL_CACHE["last_reason"] = None
            continue
        break
    _SPATIAL_CACHE["last_reason"] = _SPATIAL_CACHE.get("last_reason") or (
        "zone-end" if not eff_free else "end")
    # 软区域回退: 区域内到边(zone-end)时, 用全池(free)再找一次最近的元素。
    # 这样方向键永远有响应, 不会卡在区域边界上——用户感知是"自然过渡到相邻区域",
    # 而不是"死区/卡住"。弹层内不回退(弹层必须限定)。
    if not eff_free and eff_zone != "DIALOG" and _SPATIAL_CACHE.get("last_reason") == "zone-end":
        fallback = _spatial_rank(start_rect, items, direction,
                                 profile=profile, zone=None, free=True)
        for score, ctrl, ct, rect in fallback:
            try:
                label = (ctrl.Name or ct or "")[:30]
            except Exception:
                label = ct
            if DRY:
                log(f"DRYRUN 软区域回退 {direction} -> {label} rect={rect}")
                return (label, rect, True)
            try:
                ctrl.SetFocus()
            except Exception:
                continue
            if not _focus_verify_retry(ctrl):
                continue
            try:
                rr = ctrl.BoundingRectangle
                nrect = (rr.left, rr.top, rr.right, rr.bottom)
            except Exception:
                nrect = None
            if nrect and nrect[2] - nrect[0] > 0 and nrect[3] - nrect[1] > 0:
                dx, dy = nrect[0] - rect[0], nrect[1] - rect[1]
                if cached and (abs(dx) >= 2 or abs(dy) >= 2):
                    _SPATIAL_CACHE["items"] = _spatial_shift_cache(_SPATIAL_CACHE["items"], dx, dy)
                _SPATIAL_CACHE["focus"] = (ctrl, nrect, None)
                _spatial_focus_feedback(nrect)
                FOCUS_BOX.show(nrect)
                _SPATIAL_CACHE["last_reason"] = None
                return (label, nrect, True)
            _SPATIAL_CACHE["focus"] = (ctrl, rect, None)
            _spatial_focus_feedback(rect)
            FOCUS_BOX.show(rect)
            _SPATIAL_CACHE["last_reason"] = None
            return (label, rect, True)
    return ("", None, False)


def _spatial_activate_impl():
    """OK 键: 对当前焦点元素执行默认动作 (Invoke→Toggle→ExpandCollapse→Select);
    全部不可用时返回 None, 由调用方退回物理回车。专职 UIA 线程内执行"""
    try:
        focused = _UIA_AUTO.GetFocusedControl()
    except Exception:
        return None
    if focused is None:
        return None
    if DRY:
        log(f"DRYRUN 遥控激活: {_ct_name(focused)} {(focused.Name or '')[:30]!r}")
        return True
    for get, act in (
        ("GetInvokePattern", lambda p: p.Invoke()),
        ("GetTogglePattern", lambda p: p.Toggle()),
        ("GetSelectionItemPattern", lambda p: p.Select()),
    ):
        try:
            act(getattr(focused, get)())
            return True
        except Exception:
            continue
    try:
        p = focused.GetExpandCollapsePattern()
        if int(p.ExpandCollapseState) == 1:   # Expanded
            p.Collapse()
        else:
            p.Expand()
        return True
    except Exception:
        pass
    return None


def _spatial_enter_impl(target, profile=None, zone=None):
    """遥控模式开启: 记录遥控目标窗口并重扫候选, 聚焦目标区域内第一个视口内候选。
    不沿用「当前焦点」当起点——实测它可能落在浏览器标签栏上(chrome UI),
    从那里按 down 会一竿子插到页面底部。返回 (label, rect) 或 None。
    zone 非空时只在该区域选初始落点(区域优先: COMPOSER/SEARCH 先于 MAIN)。
    专职 UIA 线程内执行"""
    _SPATIAL_CACHE["target"] = target
    _SPATIAL_CACHE["hwnd"] = None   # 开启即作废旧缓存
    # 强制目标窗口到前台: SetFocus 对后台窗口元素不生效(实测 AppActivate 不可靠时
    # 初始候选 SetFocus/verify 全失败 → enter None → 「未就绪」)
    try:
        ctypes.windll.user32.SetForegroundWindow(target)
        time.sleep(0.15)
    except Exception:
        pass
    fg = _spatial_fg_for(target) if target else None
    vrect = None
    try:
        wr = fg.BoundingRectangle if fg is not None else None
        vrect = (wr.left, wr.top, wr.right, wr.bottom) if wr else None
        # 任何 return 路径都要有 target_rect: tick 的「焦点在目标窗口外」检查靠它,
        # 缺了就会给任务栏/其他窗口的焦点也画框(实测底部状态栏闪框的根因之一)
        _SPATIAL_CACHE["target_rect"] = vrect
    except Exception:
        vrect = None
    items, _ = _spatial_candidates_impl(target, force=True, profile=profile)
    if not items:
        # 冷启动时 Chromium 无障碍树激活需要 1~3 秒: 空结果等一下再扫一次
        time.sleep(1.2)
        items, _ = _spatial_candidates_impl(target, force=True, profile=profile)
    # 初始落点: 视口内距窗口左上最近的候选——树序第一个可能是浏览器注入的
    # 图片占位提示等噪音元素(HN 实测), 与用户认知的「第一个元素」不符
    ranked0 = []
    for ctrl, ct, _name, cr, _focusable in items:
        if zone and _uia_zone_of(cr, profile, _name, ct) != zone:
            continue   # 区域优先: 先只在目标区域里找落点
        if vrect:
            l, t, r, b = cr
            if r <= vrect[0] or l >= vrect[2] or b <= vrect[1] or t >= vrect[3]:
                continue   # 矩形完全在视口外(零成本几何判断, 不读 IsOffscreen)
        key = (cr[0] - vrect[0] if vrect else cr[0]) + (cr[1] - vrect[1] if vrect else cr[1])
        ranked0.append((key, ctrl, ct, cr))
    if not ranked0 and zone:
        # 目标区域空: 退到整窗口, 但保持区域标记不变(下一次按键仍按区域锁评分)
        for ctrl, ct, _name, cr, _focusable in items:
            if vrect:
                l, t, r, b = cr
                if r <= vrect[0] or l >= vrect[2] or b <= vrect[1] or t >= vrect[3]:
                    continue
            key = (cr[0] - vrect[0] if vrect else cr[0]) + (cr[1] - vrect[1] if vrect else cr[1])
            ranked0.append((key, ctrl, ct, cr))
    ranked0.sort(key=lambda t: t[0])
    if not ranked0:
        return None
    for _key, ctrl, ct, cr in ranked0:
        try:
            label = (ctrl.Name or ct or "")[:30]
        except Exception:
            label = ct
        if DRY:
            log(f"DRYRUN 遥控初始焦点 -> {label} rect={cr}")
            return (label, cr)
        try:
            ctrl.SetFocus()
        except Exception:
            continue   # 不可聚焦/已消失: 试下一个候选
        if not _focus_verify_retry(ctrl):
            continue   # 初始候选验证失败(窗口刚切换 a11y 未同步等): 试下一个
        _SPATIAL_CACHE["focus"] = (ctrl, cr, None)
        return (label, cr)
    return None


class UIAAdapter:
    """NavEngine → UIA 层的适配器。所有方法都必须跑在专职 UIA 线程里:
    从这里再 _uia_call 会自己等自己死锁, 所以 NavEngine 由 _nav_call 统一调度。"""

    def enter(self, hwnd, profile=None, zone=None):
        res = _uia_call(_spatial_enter_impl, hwnd, profile, zone, timeout=15.0)
        if res is None:
            # 冷启动场景(无障碍树未激活)首试常空: 等 1.2s 重试一次再判
            time.sleep(1.2)
            res = _uia_call(_spatial_enter_impl, hwnd, profile, zone, timeout=15.0)
        return res

    def nav(self, direction, profile=None, zone=None, force=False):
        return _uia_call(_spatial_nav_impl, direction, force, profile, zone, None,
                         timeout=8.0)

    def zone_first(self, hwnd, profile=None, zone=None):
        return _uia_call(_spatial_zone_first_impl, zone, profile, timeout=12.0)

    def editing(self):
        r = _uia_call(_spatial_editing_impl, timeout=6.0)
        return bool(r)

    def press_direction(self, direction):
        _srv_tap(direction)

    def press_key(self, name):
        press_key(name)

    def activate(self):
        res = _uia_call(_spatial_activate_impl, timeout=6.0)
        return res is True

    def client_origin(self, hwnd):
        """目标窗口客户区左上角的屏幕坐标 + DPI。
        网页给的 CSS 坐标是相对视口(≈客户区)的, 换算屏幕坐标必须用真实客户区原点;
        客户端自报的 window.screenX/screenY 在各 Chrome 版本语义不一致(有的是窗口
        左上角、有的是视口左上角), 用它算会整体偏移一个标题栏高度。"""
        try:
            hwnd = int(hwnd or 0)
            if not hwnd:
                return None
            user32 = ctypes.windll.user32
            if not user32.IsWindow(hwnd):
                return None
            pt = ctypes.wintypes.POINT(0, 0)
            if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
                return None
            try:
                dpi = user32.GetDpiForWindow(hwnd) or 96
            except Exception:
                dpi = 96
            return (int(pt.x), int(pt.y), float(dpi) / 96.0)
        except Exception:
            return None

    def browser_window(self, title_hint="", url_hint=""):
        """按页面标题找一个 Chromium 浏览器窗口(顶层、可见、有标题)。
        CDP 只知道页面, 不知道窗口 hwnd; 画焦点框必须拿到真实客户区原点,
        否则框会整体偏一个标题栏高度/窗口位置。"""
        try:
            user32 = ctypes.windll.user32
            found = []

            def _cb(hwnd, _lp):
                try:
                    if not user32.IsWindowVisible(hwnd):
                        return True
                    cls = ctypes.create_unicode_buffer(128)
                    user32.GetClassNameW(hwnd, cls, 128)
                    if not cls.value.startswith("Chrome_WidgetWin"):
                        return True
                    buf = ctypes.create_unicode_buffer(512)
                    user32.GetWindowTextW(hwnd, buf, 512)
                    t = buf.value or ""
                    if not t:
                        return True
                    found.append((hwnd, t))
                except Exception:
                    pass
                return True

            user32.EnumWindows(
                ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(_cb), 0)
            if not found:
                return 0
            # 标题匹配优先(页面标题通常含在窗口标题里), 其次前台窗口, 最后第一个
            if title_hint:
                key = title_hint.strip().lower()[:40]
                for hwnd, t in found:
                    if key and key in t.lower():
                        return int(hwnd)
            fg = int(user32.GetForegroundWindow() or 0)
            for hwnd, _t in found:
                if hwnd == fg:
                    return int(hwnd)
            return int(found[0][0])
        except Exception:
            return 0


NAV = None          # NavEngine 单例(懒建)
NAV_LOCK = threading.Lock()


def nav_engine():
    global NAV
    if NAV is None:
        with NAV_LOCK:
            if NAV is None:
                NAV = NavEngine(UIAAdapter())
    return NAV


def _nav_reset():
    """遥控关闭/换窗口: 丢掉 DOM 连接与所有焦点状态"""
    global NAV
    with NAV_LOCK:
        if NAV is not None:
            try:
                NAV.shutdown()
            except Exception:
                pass
        NAV = None


def _rect4(rect):
    """把各种矩形表示统一成 (l, t, r, b)。
    支持: 4 元组/列表; {'l','t','r','b'}(nav_helper 的 screenRect);
    {'left','top','right','bottom'}; UIA 的 BoundingRectangle 风格对象。
    拿不到返回 None —— 调用方必须容忍没有矩形(焦点框就不画)。"""
    if not rect:
        return None
    try:
        if isinstance(rect, dict):
            if all(k in rect for k in ("l", "t", "r", "b")):
                return (float(rect["l"]), float(rect["t"]), float(rect["r"]), float(rect["b"]))
            if all(k in rect for k in ("left", "top", "right", "bottom")):
                return (float(rect["left"]), float(rect["top"]),
                        float(rect["right"]), float(rect["bottom"]))
            return None
        if hasattr(rect, "left") and hasattr(rect, "right"):
            return (float(rect.left), float(rect.top), float(rect.right), float(rect.bottom))
        if len(rect) >= 4:
            return (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
    except Exception:
        return None
    return None


def api_remote(data: dict) -> dict:
    """遥控入口。返回 dict(状态栏要显示区域/控件名/模式/引擎)。"""
    action = str(data.get("dir") or data.get("action") or "")
    eng = nav_engine()
    fgw = int(ctypes.windll.user32.GetForegroundWindow() or 0)
    if not fgw:
        raise ValueError("取不到前台窗口")

    # 目标窗口选择(这里踩过坑, 说明白):
    # 早先的规则是"target 与前台不符就重新 enter", 听起来合理, 实际很糟 ——
    # 浏览器窗口标题一变(切标签页/页面加载完)、或用户中途点了一下手机/别的窗口,
    # 就会重新 enter, 而重新 enter 会重新扫候选并重置焦点, 表现出来就是
    # "按着按着突然从头开始 / 焦点框跳到别处 / 引擎从 dom 掉回 uia"。
    # 现在的规则:
    #   1) 已绑定的目标还活着、且它现在就是前台 -> 直接复用, 什么都不重做
    #   2) 不在前台但还活着 -> 先把它拉回前台再复用(遥控目标是"用户想控制的窗口",
    #      不该被测试脚本/后台进程抢走焦点就丢失绑定)
    #   3) 目标没了, 或者当前前台明确是另一个窗口且用户意图不明 -> 重新 enter
    reuse = False
    if eng.target and ctypes.windll.user32.IsWindow(eng.target):
        if eng.target == fgw:
            reuse = True
        else:
            try:
                ctypes.windll.user32.SetForegroundWindow(eng.target)
                time.sleep(0.05)
                reuse = (ctypes.windll.user32.GetForegroundWindow() == eng.target)
            except Exception:
                reuse = False

    if not reuse:
        info = _win_info(fgw)
        title = info[2] if info else ""
        exe = info[3] if info else ""
        res = eng.enter(fgw, url="", title=title, exe=exe)
        if not res.get("ok"):
            # 未就绪不能让下一次按键还去 enter 一遍: 清掉状态重来
            return res
        _HL_TRACK["on"] = True
        _HL_TRACK["rect"] = None
    if not action:
        action = "status"
    out = eng.action(action, data)
    # 焦点框用「这次响应里一起返回的 rect」, 不要另外再读 eng.current_rect ——
    # 两者可能不是同一时刻的快照(name 是新元素、rect 还是上一个元素的),
    # 表现出来就是"框停在上一格, 名字已经变了"。同一份 payload 里取, 永远同步。
    r4 = _rect4(out.get("rect")) or _rect4(eng.current_rect)
    if r4:
        FOCUS_BOX.show(r4)
    return out


# ---------------------------------------------------------------- API 路由
def api_mouse(data: dict) -> str:
    mx, my = pyautogui.position()  # 光标位置(点击前) = 用户意图落点
    if "move" in data:
        dx, dy = data["move"]
        mouse_move(dx, dy)
    if "scroll" in data:
        mouse_scroll(data["scroll"])
    if "click" in data:
        log(f"mouse click={data['click']} check={bool(data.get('check'))} @({mx},{my})")
        mouse_click(data["click"])
    if "dblclick" in data:
        mouse_click("left", 2)
    if data.get("drag") == "down":
        mouse_down()
    if data.get("drag") == "up":
        mouse_up()
    if data.get("check") and HAS_UIA:
        # 联动模式: 用『点击落点的几何命中』判定是否输入框。
        # 不读焦点——Edge 处理点击的焦点更新是异步的, 立即查询会读到上一次的
        # 状态(用户实测"永远慢一步"的根因); 页面布局此刻已渲染, 坐标即证据, 零竞态。
        info = _fg_info()
        if info:
            label = _uia_call(_uia_hit_test_impl, info[0], mx, my, timeout=8.0)
            log(f"click_check: 几何命中({mx},{my}) label={label!r}")
            if label:
                return f"input:{label}"
    return "ok"


def api_key(data: dict) -> str:
    if "combo" in data:
        combo(data["combo"])
    elif "key" in data:
        press_key(data["key"])
    return "ok"


def handle_api(path: str, data: dict):
    if path == "/api/mouse":
        return api_mouse(data)
    if path == "/api/text":
        type_text(str(data.get("text", "")))
        return "ok"
    if path == "/api/stream":
        return stream_input(data)
    if path == "/api/key":
        return api_key(data)
    if path == "/api/remote":
        return api_remote(data)
    if path == "/api/cmd":
        return do_cmd(str(data.get("cmd", "")), data)
    if path == "/api/custom":
        return run_custom(str(data.get("name", "")))
    raise ValueError(f"未知接口: {path}")


# ---------------------------------------------------------------- 页面
def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def qr_svg(url: str) -> str:
    qr = qrcode.QRCode(image_factory=qrcode.image.svg.SvgPathImage, box_size=12, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image().save(buf)
    return buf.getvalue().decode()


def qr_page() -> str:
    url = f"http://{local_ip()}:{PORT}/?t={TOKEN}"
    svg = qr_svg(url) if HAS_QR else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>KZQ 连接二维码</title>
<style>body{{background:#0f1115;color:#e8eaf0;font-family:system-ui;display:flex;flex-direction:column;
align-items:center;justify-content:center;min-height:100vh;margin:0;gap:16px}}svg{{width:min(70vw,320px);
background:#fff;border-radius:12px;padding:12px}}code{{background:#1c2030;padding:6px 12px;border-radius:8px}}
p{{margin:0;color:#9aa3b5}}</style></head><body>
<h2>KZQ 手机控制台 · 扫码或输入地址</h2>
{svg}
<code>{url}</code>
<p>手机与电脑连同一 Wi-Fi，用手机浏览器扫码打开</p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "KZQ/1.0"

    def log_message(self, fmt, *args):  # 关掉逐条请求日志, 保留动作日志
        pass

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _check_token(self) -> bool:
        q = parse_qs(urlparse(self.path).query)
        t = (q.get("t") or [""])[0] or self.headers.get("X-Token", "")
        ok = bool(t) and secrets.compare_digest(t, TOKEN)
        if not ok:
            log(f"403: 无效token(前8位={t[:8]}) path={urlparse(self.path).path} —— 手机需重新扫码或刷新")   # 诊断: 抓旧版/失效页面
        return ok

    def _json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 65536:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, (STATIC / "index.html").read_bytes())
        elif path == "/qr":
            self._send(200, qr_page().encode("utf-8"))
        elif path.startswith("/anims/") and path.endswith(".json"):
            fp = STATIC / "anims" / os.path.basename(path)  # basename 防目录穿越
            if fp.exists():
                self._send(200, fp.read_bytes(), "application/json; charset=utf-8")
            else:
                self._send(404, b"not found")
        elif path.startswith("/vendor/") and path.endswith(".js"):
            fp = STATIC / "vendor" / os.path.basename(path)
            if fp.exists():
                self._send(200, fp.read_bytes(), "application/javascript")
            else:
                self._send(404, b"not found")
        elif path == "/api/ver":
            # 客户端版本自检: 返回 index.html 的 mtime, 手机页面据此自动刷新(不再挂旧版页面)
            if not self._check_token():
                self._send(403, json.dumps({"ok": False, "ver": ""}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                return
            try:
                ver = str(int(os.path.getmtime(STATIC / "index.html")))
            except Exception:
                ver = "0"
            self._send(200, json.dumps({"ok": True, "ver": ver}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        elif path == "/api/customs":
            if not self._check_token():
                body = json.dumps({"ok": False, "items": [], "error": "token"}, ensure_ascii=False).encode("utf-8")
                self._send(403, body, "application/json; charset=utf-8")
                return
            body = json.dumps({"ok": True, "items": load_customs()}, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/focus_profiles":
            if not self._check_token():
                body = json.dumps({"ok": False, "items": [], "error": "token"}, ensure_ascii=False).encode("utf-8")
                self._send(403, body, "application/json; charset=utf-8")
                return
            items = []
            for p in _load_profiles():
                sem = bool(p.get("control_type"))
                pt = isinstance(p.get("point"), dict)
                kind = "语义+位置" if (sem and pt) else ("语义" if sem else ("仅位置" if pt else "位置"))
                items.append({
                    "id": p.get("id") or _profile_id(p),
                    "exe": p.get("exe", ""),
                    "win_class": p.get("win_class", ""),
                    "site": (p.get("site") or "")[:42],
                    "kind": kind,
                })
            body = json.dumps({"ok": True, "items": items}, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/favicon.ico":
            self._send(204, b"", "text/plain")
        else:
            self._send(404, b"not found")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if not self._check_token():
                body = json.dumps({"ok": False, "error": "token 无效, 请重新扫码"}, ensure_ascii=False).encode("utf-8")
                self._send(403, body, "application/json; charset=utf-8")
                return
            msg = handle_api(path, self._json_body())
            if isinstance(msg, dict):
                # 遥控状态栏要区域/控件名/模式/引擎: dict 直接展开, 手机端不用二次请求
                payload = {"ok": bool(msg.get("ok", True))}
                payload.update(msg)
                payload.setdefault("msg", "")
            else:
                payload = {"ok": True, "msg": msg}
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        except Exception as e:
            body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode("utf-8")
            self._send(500, body, "application/json; charset=utf-8")


class Server(ThreadingHTTPServer):
    # http.server 默认 allow_reuse_address=1, 在 Windows 上会和残留进程静默双绑定同一端口,
    # 导致请求在两个实例间乱跳; 这里关掉, 端口被占时直接报错退出
    allow_reuse_address = False


def main() -> None:
    url = f"http://{local_ip()}:{PORT}"
    log("=" * 52)
    log(f"KZQ PC Remote 启动  port={PORT}  {'DRYRUN 测试模式' if DRY else '实机模式'}")
    log(f"手机连接: {url}/?t={TOKEN}")
    log(f"二维码页: {url}/qr")
    log("首次运行如弹出 Windows 防火墙提示, 勾选「专用网络」允许")
    log("=" * 52)
    if not NO_BROWSER:
        webbrowser.open(f"{url}/qr")
    Server(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
