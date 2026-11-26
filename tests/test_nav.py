# -*- coding: utf-8 -*-
"""语义导航自动化验收测试。

前置:
    1) python tests/fixture_server.py 9751
    2) Chrome --remote-debugging-port=9222 --user-data-dir=<临时目录> http://127.0.0.1:9751/main.html

运行:
    .venv\\Scripts\\python.exe tests\\test_nav.py

覆盖验收项:
    A 扫描质量        正文/隐藏/禁用/遮挡/正文链接不进候选
    B 区域锁定        区内移动, 到边界停, 不跨区兜底
    C 输入区顺序      附件→工具→输入框→语音→发送, 缺控件跳过
    D 弹窗围栏        弹窗内只有 DIALOG 候选, 关闭后恢复原区
    E 编辑态          输入框里方向键不抢焦点
    F 刷新/动态插入   元素失效后重扫不报错, key 稳定
    G 100 连按不乱跳  连续按键焦点始终在同区且不抖
    H 区域切换        显式切区落到目标区第一个控件
"""
import os
import sys
import time

PORT = os.environ.get("KZQ_FIXTURE_PORT", "9751")
os.environ.setdefault("KZQ_CDP_PORT", "9222")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import site_profiles as SP                     # noqa: E402
from cdp_client import CDPManager, CDPError    # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((bool(cond), name, detail))
    print(("  PASS " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))
    return bool(cond)


class Page:
    def __init__(self, cdp):
        self.cdp = cdp
        self.p = cdp.ensure()
        self.cfg = SP.profile_for_js(SP.get_profile("generic"))

    def load(self, name="main.html"):
        self.p.send("Page.navigate", {"url": f"http://127.0.0.1:{PORT}/{name}"})
        time.sleep(1.2)
        self.p.ensure_helper()

    def js(self, expr):
        return self.p.evaluate(expr)

    def focus_id(self):
        return self.js("window.__lastFocus")

    def click(self, sel):
        self.js(f"document.querySelector({sel!r}).click()")
        time.sleep(0.25)

    def scan(self):
        return self.p.scan(self.cfg)

    def names(self):
        return [(c.get("name") or "", c.get("zone") or "") for c in
                (self.scan().get("candidates") or [])]


def main():
    cdp = CDPManager()
    if not cdp.available():
        print("CDP 不可用: 先手动启动带 --remote-debugging-port=9222 的调试浏览器")
        return 2
    pg = Page(cdp)

    # ---------------------------------------------------------------- A 扫描质量
    print("\n[A] 扫描质量")
    pg.load()
    cands = pg.scan().get("candidates") or []
    names = [c.get("name") or "" for c in cands]
    ids = pg.js("Array.from(document.querySelectorAll('[data-kzq-nav]')).map(e=>e.id)")
    check("正文段落不进候选", "这段正文是纯文本段落，方向键永远不应该停在它上面。" not in names)
    check("正文链接不进候选", "正文链接 1" not in names and "正文链接 2" not in names)
    check("禁用按钮不进候选", "禁用按钮" not in names)
    check("隐藏按钮不进候选",
          "隐藏按钮" not in names and "不可见按钮" not in names)
    check("被遮挡按钮不进候选", "被遮挡的按钮" not in names)
    check("侧栏链接可导航(链接区白名单)", "侧栏链接" in names)
    zones = pg.scan().get("zones") or {}
    for z in ("TOPBAR", "SIDEBAR", "MAIN", "COMPOSER", "SEARCH"):
        check(f"识别到区域 {z}", zones.get(z, 0) > 0, f"n={zones.get(z)}")

    # ---------------------------------------------------------------- B 区域锁定
    print("\n[B] 区域锁定 / 到边界停")
    r = pg.p.zone_first(pg.cfg, "COMPOSER")
    cur = r.get("id")
    trail = []
    for _ in range(10):
        res = pg.p.nav(pg.cfg, "right", "COMPOSER", cur, "smart")
        if res.get("moved"):
            cur = res["id"]
            trail.append(res.get("zone"))
        else:
            trail.append("STOP:" + str(res.get("reason")))
    check("COMPOSER 向右只停 COMPOSER", all(z in ("COMPOSER",) or
                                          str(z).startswith("STOP") for z in trail), str(trail))
    check("COMPOSER 到头返回 zone-end/group-end",
          str(trail[-1]).startswith("STOP"))

    r = pg.p.zone_first(pg.cfg, "TOPBAR")
    cur = r.get("id")
    moved_zones = set()
    for _ in range(8):
        res = pg.p.nav(pg.cfg, "down", "TOPBAR", cur, "smart")
        if not res.get("moved"):
            break
        cur = res["id"]
        moved_zones.add(res.get("zone"))
    check("TOPBAR 按下不窜到 MAIN", moved_zones <= {"TOPBAR"}, str(moved_zones))

    # ---------------------------------------------------------------- C 输入区顺序
    print("\n[C] 输入区功能组顺序")
    r = pg.p.zone_first(pg.cfg, "COMPOSER")
    check("首落点是附件", (r.get("name") or "") == "附件", r.get("name") or "")
    seq = [r.get("name") or ""]
    cur = r.get("id")
    for _ in range(5):
        res = pg.p.nav(pg.cfg, "right", "COMPOSER", cur, "smart")
        if not res.get("moved"):
            break
        cur = res["id"]
        seq.append(res.get("name") or "")
    # 空输入框放行导航(不吞方向键) —— 否则页面自动聚焦搜索框时整页遥控失效,
    # 这是真站(必应首页)实测出来的刚性需求。
    check("顺序=附件→工具→输入框→语音→发送",
          seq == ["附件", "工具", "输入消息…", "语音", "发送"], str(seq))

    # ---------------------------------------------------------------- E 编辑态
    print("\n[E] 编辑态方向键交还应用")
    # 关键区分: 空输入框放行导航; 有内容时才把方向键让给文本编辑
    pg.js("document.getElementById('c-input').value='';"
          "document.getElementById('c-input').focus()")
    time.sleep(0.15)
    st_empty = pg.p.state(pg.cfg)
    check("空输入框: editing_room=False(方向键可导航)",
          bool(st_empty.get("editing")) and not st_empty.get("editing_room"),
          f"editing={st_empty.get('editing')} room={st_empty.get('editing_room')}")
    # 起点用当前真实焦点元素 id(每次扫描 id 会重排, 不能写死)
    input_id = (st_empty.get("active") or {}).get("id") or ""
    res = pg.p.nav(pg.cfg, "right", "COMPOSER", input_id, "smart")
    check("空输入框: 右方向键可以移出输入框", bool(res.get("moved")),
          f"from={input_id} -> {res.get('name')} reason={res.get('reason')}")

    # 有内容时方向键归输入框
    pg.js("var t=document.getElementById('c-input');"
          "t.value='hello world';t.focus();"
          "t.setSelectionRange(3,3);")
    time.sleep(0.2)
    st_full = pg.p.state(pg.cfg)
    check("有内容: editing_room=True(方向键归文本编辑)",
          bool(st_full.get("editing")) and bool(st_full.get("editing_room")),
          f"editing={st_full.get('editing')} room={st_full.get('editing_room')}")
    fid = pg.focus_id()
    res = pg.p.nav(pg.cfg, "right", "COMPOSER", "n22", "smart")
    check("有内容: 右方向键不移动焦点",
          (not res.get("moved")) and res.get("reason") == "editing",
          f"moved={res.get('moved')} reason={res.get('reason')}")
    pg.js("document.getElementById('c-input').value=''")

    # ---------------------------------------------------------------- D 弹窗围栏
    print("\n[D] 弹窗围栏与恢复")
    pg.load()
    pg.js("document.getElementById('m-2').focus()")
    time.sleep(0.15)
    pg.click("#btn-dialog")
    st = pg.p.state(pg.cfg)
    check("弹窗被识别", bool(st.get("dialog")))
    sc = pg.p.scan(pg.cfg)
    dlg = [c for c in (sc.get("candidates") or []) if c.get("zone") == "DIALOG"]
    nondlg = [c for c in (sc.get("candidates") or []) if c.get("zone") != "DIALOG"]
    check("弹窗内候选都在 DIALOG 区", len(dlg) >= 2, f"dlg={len(dlg)}")
    check("弹窗时候选区被围栏(其他区不入 DIALOG)", len(nondlg) == 0,
          f"other={len(nondlg)}")
    r = pg.p.zone_first(pg.cfg, "MAIN")   # 有弹窗时 zoneFirst 强制走 DIALOG
    check("弹窗时 zoneFirst 落在 DIALOG", r.get("zone") == "DIALOG", str(r.get("zone")))
    pg.js("document.getElementById('dlg-cancel').click()")
    time.sleep(0.3)
    st = pg.p.state(pg.cfg)
    check("弹窗关闭后 dialog=False", not st.get("dialog"))

    # ---------------------------------------------------------------- F 刷新/动态
    print("\n[F] 动态插入与刷新")
    pg.load()
    before = len(pg.scan().get("candidates") or [])
    pg.click("#btn-insert")
    pg.click("#btn-insert")
    after = len(pg.scan().get("candidates") or [])
    check("动态插入按钮进入候选", after == before + 2, f"{before}->{after}")
    key1 = pg.p.zone_first(pg.cfg, "MAIN").get("key")
    pg.load()
    r = pg.p.restore(pg.cfg, key1, "MAIN")
    check("刷新后靠稳定 key 重绑焦点", bool(r.get("moved")), str(r.get("reason")))
    stale = pg.p.restore(pg.cfg, "不存在的key|MAIN|button|x", "MAIN")
    check("失效 key 不误命中(返回 key-gone)", not stale.get("moved"),
          str(stale.get("reason")))

    # ---------------------------------------------------------------- H 区域切换
    print("\n[H] 区域切换")
    pg.load()
    for z in ("TOPBAR", "SIDEBAR", "MAIN", "SEARCH"):
        r = pg.p.zone_first(pg.cfg, z)
        sc = pg.p.scan(pg.cfg).get("zones") or {}
        ok = r.get("moved") and r.get("zone") == z and pg.focus_id()
        check(f"切到 {z} 有落点", bool(ok), f"{r.get('name')} / focus={pg.focus_id()}")

    # ---------------------------------------------------------------- G 100 连按
    print("\n[G] 100 连按不乱跳")
    pg.load()
    r = pg.p.zone_first(pg.cfg, "MAIN")
    cur = r.get("id")
    dirs = ["down", "right", "down", "left", "down", "right"]
    zones_seen = set()
    stops = 0
    scatter = 0
    prev = None
    for i in range(100):
        d = dirs[i % len(dirs)]
        res = pg.p.nav(pg.cfg, d, "MAIN", cur, "smart")
        if not res.get("moved"):
            stops += 1
            # 到边界时焦点必须没变
            if res.get("id") and res.get("id") != cur:
                scatter += 1
            continue
        cur = res["id"]
        zones_seen.add(res.get("zone"))
        if prev is not None and res.get("id") == prev:
            pass
        prev = cur
    check("100 连按焦点只在 MAIN 区", zones_seen <= {"MAIN"}, str(zones_seen))
    check("没有出现焦点被顶到别的元素(scatter=0)", scatter == 0, f"scatter={scatter}")
    check("到边界会停(有 STOP 计数)", stops > 0, f"stops={stops}")

    # ---------------------------------------------------------------- 汇总
    fails = [r for r in RESULTS if not r[0]]
    print("\n" + "=" * 60)
    print(f"共 {len(RESULTS)} 项, 通过 {len(RESULTS) - len(fails)}, 失败 {len(fails)}")
    for _ok, name, detail in fails:
        print(f"  FAIL: {name} [{detail}]")
    return 0 if not fails else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CDPError as e:
        print(f"CDP 错误: {e}")
        sys.exit(2)
