# -*- coding: utf-8 -*-
"""从 server.py 抽取字形簇助手函数, 跑断言(不 import server, 避免副作用)"""
import ast, unicodedata, sys

SRC = r"D:\AI-agent\KZQ\pc-remote\server.py"
tree = ast.parse(open(SRC, encoding="utf-8").read())
WANT = {"_g_spans", "_g_len", "_g_cp_index", "_g_splice", "_u16_len", "_g_u16_index", "_g_u16_offset", "_caret_norm_u16", "_caret_raw_u16"}
ns = {"unicodedata": unicodedata, "_VS_SET": {0xFE0E, 0xFE0F}}
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in WANT:
        code = ast.get_source_segment(open(SRC, encoding="utf-8").read(), node)
        exec(compile(code, SRC, "exec"), ns)

g_len = ns["_g_len"]; u16_len = ns["_u16_len"]
g_idx = ns["_g_u16_index"]; cn = ns["_caret_norm_u16"]
cr = ns["_caret_raw_u16"]
splice = ns["_g_splice"]; spans = ns["_g_spans"]
g_off = ns["_g_u16_offset"]

checks = []
def ck(name, got, want):
    checks.append((name, got, want, got == want))

# 1 纯 ASCII
ck("ascii len", g_len("abc"), 3)
# 2 CRLF 一簇
ck("crlf len", g_len("a\r\nb"), 3)
# 3 emoji(码点)
s3 = "a\U0001F44Db"
ck("emoji len", g_len(s3), 3)
ck("emoji u16len", u16_len(s3), 4)
# 4 JS 语义 caret 换算: "a👍b" utf16 = 4
for u16, want in [(0, 0), (1, 1), (3, 2), (4, 3)]:
    ck(f"gidx({u16})", g_idx(s3, u16), want)
# 5 ZWJ family 一簇
s5 = "\U0001F468‍\U0001F469‍\U0001F467"
ck("zwj len", g_len(s5), 1)
# 6 旗帜一簇
ck("flag len", g_len("🇨🇳x"), 2)
# 7 肤色修饰一簇
ck("skin len", g_len("\U0001F44D\U0001F3FB"), 1)
# 8 keycap 一簇
ck("keycap len", g_len("1️⃣"), 1)
# 9 caret 规范化: "ab\r\ncd"
ck("caret crlf(3)", cn("ab\r\ncd", 3), 3)   # \n 之后
ck("caret crlf(2)", cn("ab\r\ncd", 2), 2)   # \r 之前
ck("caret crlf(6)", cn("ab\r\ncd", 6), 5)   # 末尾
# 10 splice
ck("splice del", splice("a\U0001F44Dbc", 1, 1, "X"), "aXbc")
ck("splice ins", splice("abc", 1, 0, "XY"), "aXYbc")
# 11 多行文本 gidx 含 CRLF: 原始 "a\r\n👍", 规范化 "a\n👍"
raw = "a\r\n\U0001F44D"
norm, _ = {"": ""}, None
nrm = raw.replace("\r\n", "\n")
ck("crlf emoji idx(3)", g_idx(nrm, cn(raw, 3)), 2)  # raw u16=3(\n 之后) -> 规范化 u16=2 -> emoji 前第 2 簇
ck("crlf emoji idx(4)", g_idx(nrm, cn(raw, 4)), 3)  # raw u16=4(emoji 中段) -> 归簇尾 = 末尾第 3 簇
# 12 边界
ck("gidx beyond", g_idx(s3, 99), 3)
ck("splice beyond", splice("ab", 5, 3, "Z"), "abZ")
# 13 簇索引 -> utf16 偏移(clip 读回还原光标用)
ck("goff(0)", g_off(s3, 0), 0)
ck("goff(1)", g_off(s3, 1), 1)          # 'a' 之后
ck("goff(2)", g_off(s3, 2), 3)          # emoji 之后
ck("goff(3)", g_off(s3, 3), 4)          # 末尾
ck("goff beyond", g_off(s3, 9), 4)
# 14 设置电脑光标: 规范化坐标必须转回原始 CRLF 坐标
raw2 = "ab\r\ncd\r\n\U0001F44Def"
norm2 = raw2.replace("\r\n", "\n")
ck("raw caret after first CRLF", cr(raw2, 3), 4)
ck("raw caret after second CRLF", cr(raw2, 6), 8)
ck("raw caret end", cr(raw2, u16_len(norm2)), u16_len(raw2))
for g in range(g_len(norm2) + 1):
    norm_caret = g_off(norm2, g)
    ck(f"crlf roundtrip({g})", cn(raw2, cr(raw2, norm_caret)), norm_caret)

bad = [c for c in checks if not c[3]]
for name, got, want, ok in checks:
    print(("PASS" if ok else "FAIL"), name, "got=", got, "want=", want)
print(f"\n{len(checks)-len(bad)}/{len(checks)} passed")
sys.exit(1 if bad else 0)
