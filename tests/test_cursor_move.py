# -*- coding: utf-8 -*-
"""光标直设回归: CRLF 原始坐标 + 同位置重复点击仍执行绝对定位。"""
import ast
import time
import unicodedata

SRC = r"D:\AI-agent\KZQ\pc-remote\server.py"
source = open(SRC, encoding="utf-8").read()
tree = ast.parse(source)
WANT = {"_g_spans", "_g_cp_index", "_u16_len", "_g_u16_offset", "_caret_raw_u16",
        "_caret_norm_u16", "_g_u16_index", "_norm_newlines", "_srv_move",
        "_uia_caret_set_impl", "_em_space_kind", "_probe_em_space",
        "_em_sel_to_norm_u16", "_norm_u16_to_em", "_srv_anchor_head"}
ns = {"unicodedata": unicodedata, "_VS_SET": {0xFE0E, 0xFE0F}, "time": time,
      "_EM_SPACE": {1: "raw", 7: "raw"}, "_EM_GETSEL": 0x00B0, "_EM_SETSEL": 0x00B1}
_em_state = {"last": 0}

def fake_w32(hwnd, msg, wp=0, lp=0):
    if msg == 0x00B1:          # EM_SETSEL: 记录设置值
        _em_state["last"] = wp
        return wp
    return _em_state.get("last")   # EM_GETSEL: 返回上次设置值

ns["_w32_send"] = fake_w32
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in WANT:
        exec(compile(ast.get_source_segment(source, node), SRC, "exec"), ns)

RAW = "ab\r\ncd\r\nef"
NORM = RAW.replace("\r\n", "\n")
calls = []

def fake_set_caret(hwnd, pos):
    _em_state["last"] = pos      # 仿真 EM_SETSEL -> EM_GETSEL 状态传递
    calls.append(pos)
    return True

ns.update({
    "_SRV_FULL": True,
    "_SRV_BLIND": False,
    "_SRV_BUF": NORM,
    "_SRV_CARET": 4,
    "_SRV_MISMATCH": False,
    "_SRV_MISMATCH_LOG_TS": 0.0,
    "_SRV_LAST_MOVE_TS": 0.0,
    "_win32_readback": lambda: (1, RAW, 0),
    "_win32_set_caret": fake_set_caret,
    "_win32_ensure_caret_visible": lambda hwnd, pos: None,
    "_uia_caret_set": lambda target, text: None,
    "_uia_caret_now": lambda: None,
    "_srv_doc_anchor": lambda end: None,
    "_srv_tap": lambda key, n=1: None,
    "_srv_anchor_head": lambda: None,
    "_fg_is_chromium": lambda: False,
    "log": lambda msg: None,
    "_mirror_calibrate_now": lambda: False,   # 早期用例桩: 触发对账会在后续断言中显式失败
})

checks = []
def ck(name, got, want):
    got = list(got) if isinstance(got, list) else got
    want = list(want) if isinstance(want, list) else want
    checks.append((name, got, want, got == want))

# 非定位编辑沿用 d==0 快路径。
ns["_SRV_CARET"] = 4
ns["_srv_move"](4)
ck("relative same-position no-op", calls, [])

# 纯光标定位即便账目相同也直设; 连续点击同一位置不丢命令。
for _ in range(2):
    ns["_SRV_CARET"] = 4
    ns["_srv_move"](4, absolute=True)
ck("absolute same-position repeated", calls, [5, 5])

# 第二个 CRLF 后的位置: 规范化 u16=6 -> 原始 u16=8。
calls.clear()
ns["_SRV_CARET"] = 4
ns["_srv_move"](6, absolute=True)
ck("raw offset after two CRLF", calls, [8])
ck("mirror caret updated", ns["_SRV_CARET"], 6)

# UIA 直设发现焦点原文 != 镜像: 必须校准真实现状, 禁止按旧坐标继续移动/发箭头。
calls.clear()
ns["_win32_readback"] = lambda: None
ns["_SRV_MISMATCH"] = False
ns["_SRV_CARET"] = 4

def fake_uia_set(target, text):
    ns["_SRV_MISMATCH"] = True
    return None

calibrated = []

def fake_calibrate():
    calibrated.append(True)
    ns["_SRV_BUF"] = "actual"
    ns["_SRV_CARET"] = 2
    return True

ns["_uia_caret_set"] = fake_uia_set
ns["_mirror_calibrate_now"] = fake_calibrate
ns["_srv_move"](6, absolute=True)
ck("uia mismatch calibrates mirror",
   [bool(calibrated), ns["_SRV_BUF"], ns["_SRV_CARET"], calls], [True, "actual", 2, []])

# _uia_caret_set_impl: 原文与镜像不一致时直接拒绝, 连选区都不允许触碰。
class FakeDoc:
    def GetText(self, _n):
        return "actual\r\ntext"

    def Clone(self):
        raise AssertionError("text-mismatch must not touch selection")

class FakeTP:
    DocumentRange = FakeDoc()

class FakeCtrl:
    def GetTextPattern(self):
        return FakeTP()

class FakeEndpoint:
    Start = 0
    End = 1

class FakeTextUnit:
    Character = 1

class FakeAuto:
    TextPatternRangeEndpoint = FakeEndpoint
    TextUnit = FakeTextUnit

    @staticmethod
    def GetFocusedControl():
        return object()

ns["_UIA_AUTO"] = FakeAuto
ns["_candidate_from"] = lambda focused, rect, learn=True: {"ctrl": FakeCtrl()}
ns["_descend_to_edit"] = lambda focused, rect: None
r = ns["_uia_caret_set_impl"](None, 3, "expected")
ck("uia impl text-mismatch",
   (r.get("reason"), r.get("text")), ("text-mismatch", "actual\r\ntext"))

# EM 坐标空间探测(全选钳位): EM_SETSEL(0, 超大) 后 GETSEL 终点 = 控件 EM 长度。
_EM_GETSEL, _EM_SETSEL = 0x00B0, 0x00B1

def probe_kind(all_end):
    """saved 塌缩选区 5, 全选后 GETSEL 返回 (start=0, end=all_end) 打包值"""
    ns["_EM_SPACE"] = {}
    state = {"n": 0, "saved": 5 | (5 << 16)}
    ops = []
    def fake_w32(hwnd, msg, wp=0, lp=0):
        ops.append((msg, wp, lp))
        if msg == _EM_GETSEL:
            state["n"] += 1
            return state["saved"] if state["n"] == 1 else (0 | (all_end << 16))
        return None
    ns["_w32_send"] = fake_w32
    kind = ns["_em_space_kind"](7, 11, 9)   # raw=11(两个CRLF), norm=9
    return kind, ops

kind_n, ops_n = probe_kind(9)      # 全选终点 9 = 规范化长度 -> 换行算1
ck("em probe richedit norm", kind_n, "norm")
restore = [(w, l) for m, w, l in ops_n if m == _EM_SETSEL]
ck("em probe restores selection", restore[-1], (5, 5))
kind_r, _ = probe_kind(11)         # 全选终点 11 = 原文长度 -> 换行算2
ck("em probe classic raw", kind_r, "raw")
ns["_EM_SPACE"] = {}
ns["_w32_send"] = lambda hwnd, msg, wp=0, lp=0: None   # 探测消息全部失败
ck("em probe failure defaults norm", ns["_em_space_kind"](7, 11, 9), "norm")

# 两种空间的双向换算
ck("em sel->norm (norm kind)", ns["_em_sel_to_norm_u16"](RAW, 6, "norm"), 6)
ck("em norm->em (norm kind)", ns["_norm_u16_to_em"](RAW, 6, "norm"), 6)
ck("em sel->norm (raw kind)", ns["_em_sel_to_norm_u16"](RAW, 8, "raw"), 6)
ck("em norm->em (raw kind)", ns["_norm_u16_to_em"](RAW, 6, "raw"), 8)

# _srv_move 设置端按实测空间换算: norm 空间直传规范化偏移; raw 空间加 CRLF 修正。
em_args = []
ns["_SRV_BUF"] = NORM
ns["_win32_readback"] = lambda: (7, RAW, 4)
def recording_set(hwnd, pos):
    _em_state["last"] = pos      # 对账读回依赖 EM 状态传递
    em_args.append(pos)
    return True

ns["_win32_set_caret"] = recording_set
ns["_uia_caret_set"] = lambda target, text: None
ns["_mirror_calibrate_now"] = lambda: False
ns["_em_space_kind"] = lambda hwnd, rlen, nlen: "norm"
ns["_SRV_MISMATCH"] = False
ns["_SRV_CARET"] = 4
ns["_srv_move"](6, absolute=True)
ck("srv_move norm-space sets direct", em_args, [6])

em_args.clear()
ns["_em_space_kind"] = lambda hwnd, rlen, nlen: "raw"
ns["_SRV_MISMATCH"] = False
ns["_SRV_CARET"] = 4
ns["_srv_move"](6, absolute=True)
ck("srv_move raw-space adds CRLF", em_args, [8])

# Chromium/Electron(DSH): 不碰 UIA 也不碰 Win32 —— 纯定位 Ctrl+Home 锚定 + 箭头; 编辑走差值箭头。
anchors, taps = [], []
w32_calls_before = len(calls)
ns["_fg_is_chromium"] = lambda: True
ns["_win32_readback"] = lambda: (_ for _ in ()).throw(AssertionError("chromium must not readback"))
ns["_srv_doc_anchor"] = lambda end: anchors.append("end" if end else "head")
ns["_srv_anchor_head"] = lambda: anchors.append("head")
ns["_srv_tap"] = lambda key, n=1: taps.append((key, n))
ns["_SRV_MISMATCH"] = False
ns["_SRV_CARET"] = 0
ns["_srv_move"](6, absolute=True)
ck("chromium absolute anchors+arrows",
   [anchors, taps, ns["_SRV_CARET"], len(calls) - w32_calls_before],
   [["head"], [("right", 6)], 6, 0])

taps.clear()
ns["_srv_move"](8)
ck("chromium edit delta arrows", [taps, ns["_SRV_CARET"]], [[("right", 2)], 8])

# 无镜像(服务刚起/未读回): 纯定位用字段头锚定 + 原始 at, 不能被空镜像钳成 0。
ns["_fg_is_chromium"] = lambda: False
ns["_win32_readback"] = lambda: (_ for _ in ()).throw(AssertionError("no-mirror must not readback"))
ns["_win32_set_caret"] = lambda hwnd, pos: (_ for _ in ()).throw(AssertionError("no-mirror must not EM_SETSEL"))
ns["_SRV_BUF"] = ""
ns["_SRV_CARET"] = 0
ns["_SRV_FULL"] = False
ns["_SRV_BLIND"] = False
ns["_SRV_MISMATCH"] = False
ns["_srv_move"](12, absolute=True)
ck("no-mirror absolute anchors+arrows",
   [anchors[-1], taps[-1], ns["_SRV_CARET"]], ["head", ("right", 12), 12])

# 字段头锚定归一化: Ctrl+Home 后补 Left —— ProseMirror(落点1)与普通编辑器(落点0)都归零。
anchor_ops, anchor_taps = [], []
ns["_srv_doc_anchor"] = lambda end: anchor_ops.append(("anchor", end))
ns["_srv_tap"] = lambda key, n=1: anchor_taps.append((key, n))
real_anchor_head = ns["_srv_anchor_head"] if isinstance(ns["_srv_anchor_head"], type(lambda: 0)) and getattr(ns["_srv_anchor_head"], "__name__", "") == "_srv_anchor_head" else None
# 直接用 exec 出来的真实实现(此前被 chromium 测试桩覆盖)
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name == "_srv_anchor_head":
        exec(compile(ast.get_source_segment(source, node), SRC, "exec"), ns)
ns["_srv_anchor_head"]()

# ---- _srv_tap: n=0 必须零按键(此前 max(1,n) 把"右移0次"变成按一次 → at=0 偏到 1) ----
_WANT_TAP = {"_srv_tap"}
taps_log = []
class FakeKB:
    def press(self, k): taps_log.append(("press", k))
    def release(self, k): taps_log.append(("release", k))
ns2 = {"_SRV_KEYMAP": {"left": "L", "right": "R"}, "_KB": FakeKB(), "press_key": lambda k: taps_log.append(("pyautogui", k))}
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in _WANT_TAP:
        exec(compile(ast.get_source_segment(source, node), SRC, "exec"), ns2)
ns2["_srv_tap"]("right", 0)
ck("srv_tap n=0 zero presses", taps_log, [])
ns2["_srv_tap"]("right", 2)
ck("srv_tap n=2 two presses", len(taps_log), 4)   # 2次 press+release = 4 条
ns2["_srv_tap"]("right", -1)
ck("srv_tap negative zero presses", len(taps_log), 4)
ck("anchor head normalizes with Left",
   [anchor_ops, anchor_taps], [[("anchor", False)], [("left", 1)]])

# 无镜像但校准成功: 直接进入精确直设通道。
ns["_SRV_BUF"] = NORM
ns["_SRV_CARET"] = 0
ns["_SRV_FULL"] = True
ns["_SRV_BLIND"] = False
ns["_SRV_MISMATCH"] = False
ns["_win32_readback"] = lambda: (1, RAW, 0)
ns["_mirror_calibrate_now"] = lambda: True
ns["_win32_set_caret"] = fake_set_caret
ns["_srv_move"](6, absolute=True)
ck("no-mirror calibrate uses exact SET", [calls[-1], ns["_SRV_CARET"]], [8, 6])

bad = [c for c in checks if not c[3]]
for name, got, want, ok in checks:
    print(("PASS" if ok else "FAIL"), name, "got=", got, "want=", want)
print(f"\n{len(checks)-len(bad)}/{len(checks)} passed")
raise SystemExit(1 if bad else 0)
