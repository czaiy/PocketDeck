# -*- coding: utf-8 -*-
"""端到端遥控接口测试: 直接打 /api/remote, 验证真实服务链路(引擎/CDP/焦点框)。"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:9761"
TOKEN = "testtok"


def call(payload):
    req = urllib.request.Request(
        BASE + "/api/remote",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Token": TOKEN},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return {"ok": False, "error": f"{e} {body}"}


def show(tag, j):
    if not j.get("ok"):
        print(f"  {tag:>10} ! {j.get('error') or j.get('msg')}")
        return j
    print(f"  {tag:>10}  {j.get('label','')!r:28} zone={j.get('zone','') :8} "
          f"engine={j.get('engine','')} mode={j.get('mode','')} :: {j.get('msg','')}")
    return j


def main():
    print("== 初始状态 ==")
    show("status", call({"dir": "status"}))

    print("\n== MAIN 区连按右 (应只在本区, 到最后停) ==")
    for i in range(6):
        show(f"right#{i+1}", call({"dir": "right"}))

    print("\n== 切到 COMPOSER, 走功能组顺序 ==")
    show("zone_set", call({"dir": "zone_set", "zone": "COMPOSER"}))
    for i in range(6):
        show(f"right#{i+1}", call({"dir": "right"}))

    print("\n== 模式切换 ==")
    for _ in range(3):
        j = show("mode", call({"dir": "mode"}))
        if j.get("mode") == "smart":
            break

    print("\n== 区域循环 ==")
    for i in range(3):
        show(f"zone_next#{i+1}", call({"dir": "zone_next"}))

    print("\n== 回上一焦点 ==")
    show("focus_back", call({"dir": "focus_back"}))

    print("\n== 编辑态: 落在输入框上, 方向键应被文本区吃掉 ==")
    show("zone_set", call({"dir": "zone_set", "zone": "COMPOSER"}))
    for i in range(5):
        j = call({"dir": "right"})
        show(f"right#{i+1}", j)
        if j.get("editing"):
            break

    print("\n== 未知动作 ==")
    show("bogus", call({"dir": "bogus"}))


if __name__ == "__main__":
    main()
