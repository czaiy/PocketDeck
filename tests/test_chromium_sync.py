# -*- coding: utf-8 -*-
"""Chromium 显式同步回归: 点按聚焦低频读回恢复 PC→手机同步, 不碰 Ctrl+A/剪贴板。"""
import ast
import threading
import time
import unicodedata

SRC = r"D:\AI-agent\KZQ\pc-remote\server.py"
source = open(SRC, encoding="utf-8").read()
tree = ast.parse(source)
WANT = {"_g_spans", "_g_cp_index", "_u16_len", "_g_len", "_g_u16_index",
        "_norm_newlines", "_focus_readback", "_uia_read_leaf_text",
        "_uia_try_leaf_editor_read"}
ns = {"unicodedata": unicodedata, "_VS_SET": {0xFE0E, 0xFE0F}, "time": time,
      "_SRV_LOCK": threading.Lock(), "_CHROMIUM_READ_LAST": 0.0,
      "_CHROMIUM_READ_MIN_GAP": 0.3,
      "_EM_SPACE": {}, "_EM_GETSEL": 0xB0, "_EM_SETSEL": 0xB1,
      "HAS_UIA": True, "_uia_ready": lambda: True,
      "_fg_is_chromium": lambda: True,
      "_cdp_eval": lambda expr: None,
      "_CDP_EVAL_READ": "(function(){return null;})()",
      "_LEAF_EDITOR_MARKS": ("prosemirror", "tiptap", "cm-editor", "ql-editor", "public-drafteditor"),
      "_LEAF_EDITOR_TOKEN_FILTER": ("发消息或创建任务", "使用技能", "添加资料", "调用指令", "@ 文件或对话"),
      "_win32_readback": lambda: None,
      "_uia_focus_text_read_impl": lambda win_rect: None}

hotkey_calls = []
class _FakePyautogui:
    @staticmethod
    def hotkey(*a):
        hotkey_calls.append(a)

ns["pyautogui"] = _FakePyautogui
ns["pyperclip"] = type("P", (), {"paste": staticmethod(lambda: ""), "copy": staticmethod(lambda s: None)})

uia_calls = []
READ_RESULT = {"ok": True}


def fake_uia_call(fn, *args, timeout=0, **kw):
    if READ_RESULT["ok"]:
        uia_calls.append(1)
        return {"text": "电脑端文本内容", "caret": 3, "reason": ""}
    uia_calls.append(1)
    return None


ns["_uia_call"] = fake_uia_call
ns["_SRV_BUF"] = ""
ns["_SRV_CARET"] = None
ns["_SRV_FULL"] = False
ns["_SRV_BLIND"] = False
ns["_SRV_LAST_MOVE_TS"] = 0.0
ns["log"] = lambda msg: None

for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in WANT:
        exec(compile(ast.get_source_segment(source, node), SRC, "exec"), ns)

checks = []
def snap(x):
    if isinstance(x, (list, tuple)):
        return [snap(i) for i in x]
    return x

def ck(name, got, want):
    checks.append((name, snap(got), snap(want), snap(got) == snap(want)))

# 周期巡逻(clip=0): 与显式同步同通道, CDP 可用时读文本+真实光标(自动同步恢复)。
ns["_CHROMIUM_READ_LAST"] = 0.0
ns["_cdp_eval"] = lambda expr: {"v": "电脑端文本内容", "s": 4}
r = ns["_focus_readback"](("r", "Chrome_WidgetWin_1", "t", "e"), clip=False)
ck("chromium patrol syncs via CDP", (r[0], r[1], r[2]), ("电脑端文本内容", 4, ""))
ck("chromium patrol caret bookkeeping",
   [ns["_SRV_BLIND"], ns["_SRV_CARET"], ns["_SRV_BUF"]], [False, 4, "电脑端文本内容"])

# 限频窗口内重复请求: 跳过并返回 chromium-ratelimit。
r = ns["_focus_readback"](("r", "Chrome_WidgetWin_1", "t", "e"), clip=False)
ck("chromium read rate-limited", r, (None, None, "chromium-ratelimit"))

# CDP 不可用: 回退 UIA 文本同步(光标不可信置盲区), 依然不碰 Ctrl+A。
ns["_CHROMIUM_READ_LAST"] = 0.0
ns["_cdp_eval"] = lambda expr: None
r = ns["_focus_readback"](("r", "Chrome_WidgetWin_1", "t", "e"), clip=True)
ck("cdp absent falls back to UIA",
   (r[0], r[2], ns["_SRV_BLIND"]), ("电脑端文本内容", "", True))
ck("no Ctrl+A in chromium sync", hotkey_calls, [])

# UIA 失败: 返回失败原因, 依然不碰 Ctrl+A。
READ_RESULT["ok"] = False
ns["_CHROMIUM_READ_LAST"] = 0.0
r = ns["_focus_readback"](("r", "Chrome_WidgetWin_1", "t", "e"), clip=True)
ck("chromium read fail no flash",
   (r, hotkey_calls), [(None, None, "chromium-read-fail"), []])

# CDP 恢复可用: 立即退出失败态, 重新同步(文本+精确光标)。
READ_RESULT["ok"] = True
ns["_CHROMIUM_READ_LAST"] = 0.0
ns["_cdp_eval"] = lambda expr: {"v": "电脑端文本内容", "s": 4}
r = ns["_focus_readback"](("r", "Chrome_WidgetWin_1", "t", "e"), clip=True)
ck("cdp recovery works", (r[0], r[1]), ("电脑端文本内容", 4))

# ---- ProseMirror 类无模式编辑器: 叶子 Name 拼接读取 ----
class FakeCtrl:
    def __init__(self, name, cls="", kids=()):
        self._name = name
        self._cls = cls
        self._kids = list(kids)

    def GetChildren(self):
        return list(self._kids)

    @property
    def ClassName(self):
        return self._cls

    @property
    def Name(self):
        return self._name


def leaf(name):
    return FakeCtrl(name, "Text")


PM_CLS = "tiptap ProseMirror ProseMirror-focused"

# 有内容: 拼接叶子 Name(按文档顺序)
root = FakeCtrl("", PM_CLS, [leaf("第01行测试"), leaf("第02行测试")])
ck("leaf read joins in order", ns["_uia_read_leaf_text"](root), ("第01行测试第02行测试", ["第01行测试", "第02行测试"]))

# ProseMirror 焦点分支: 读取成功, 光标置 None(盲区)
r = ns["_uia_try_leaf_editor_read"](root)
ck("leaf editor read via branch",
   (r["text"], r["caret"], r["reason"]), ("第01行测试第02行测试", None, ""))

# 空输入框: 叶子全部由占位符/按钮词组成 -> 视为空
root_empty = FakeCtrl("", PM_CLS, [
    leaf("发消息或创建任务, / 调用指令, @ 文件或对话"),
    leaf("使用技能"), leaf("@ 添加资料"),
])
r = ns["_uia_try_leaf_editor_read"](root_empty)
ck("leaf editor placeholder filtered", (r["text"], r["caret"]), ("", None))

# 非编辑器类名: 不走该分支
ck("non-editor returns None", ns["_uia_try_leaf_editor_read"](FakeCtrl("", "SomeWindow")), None)

# 深层嵌套: 编辑器内容在多层子树下也能拼齐
root_deep = FakeCtrl("", PM_CLS, [
    FakeCtrl("", "p", [leaf("第一段"), leaf("第二段")]),
    FakeCtrl("", "p", [leaf("第三段")]),
])
ck("leaf read nested", ns["_uia_read_leaf_text"](root_deep)[0], "第一段第二段第三段")

bad = [c for c in checks if not c[3]]
for name, got, want, ok in checks:
    print(("PASS" if ok else "FAIL"), name, "got=", got, "want=", want)
print(f"\n{len(checks)-len(bad)}/{len(checks)} passed")
raise SystemExit(1 if bad else 0)
