# -*- coding: utf-8 -*-
"""Semantic Navigation + Zone Navigation + Spatial Navigation 编排层。

优先级（按需求）：
  1. Chromium CDP DOM 语义识别（本模块，页面内 nav_helper.js）
  2. Windows UI Automation（回调注入，server.py 内实现）
  3. OCR + OpenCV（可选层，ocr_scan.py；未安装时静默跳过）
  4. SendInput / 鼠标坐标（server 既有兜底）

关键不变量：
  - 默认锁定当前区域，方向键只在区域内找目标；没有目标就停。
  - 弹窗出现自动进入 DIALOG，关闭后恢复原区域与焦点（稳定 key，不是 DOM 引用）。
  - 输入框编辑态方向键交还应用，不触发导航（free 模式除外）。
  - 页面刷新/换页后清空元素引用，靠稳定 key 重绑。
  - DOM/UIA/OCR 不叠加：同一时刻只有一个 engine 产生焦点。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

import site_profiles as SP
from cdp_client import CDPManager

DIRS = ("up", "down", "left", "right")
MODES = ("smart", "zone", "free")
MODE_NAMES = {"smart": "智能导航", "zone": "区域导航", "free": "自由控制"}


class NavEngine:
    def __init__(self, uia_adapter: Any) -> None:
        self.uia = uia_adapter
        self.cdp = CDPManager()
        self.lock = threading.RLock()
        self.target = 0
        self.site_key = ""
        self.profile: Dict[str, Any] = SP.generic_profile()
        self.mode = "smart"
        self.engine = "none"          # dom | uia | ocr | none
        self.zone = "MAIN"
        self.zone_stack = []          # 弹窗/显式切区前的区域
        self.current_id = ""
        self.current_key = ""
        self.current_name = ""
        self.current_rect = None
        self.current_zone = "MAIN"
        self.history = []             # [(zone, key, name)]
        self.dialog = False
        self.editing = False
        self.url = ""
        self.title = ""
        self.last_msg = ""
        self.last_action = 0.0
        self.last_scan = 0.0
        self.zones: Dict[str, int] = {}
        self.rejects: Dict[str, int] = {}

    # ------------------------------------------------------------------ utils
    def _cfg(self) -> dict:
        return SP.profile_for_js(self.profile)

    def _profile_for(self, url: str, title: str) -> Dict[str, Any]:
        """站点切换必须加载对应 Profile；同 host 换路径保留 Profile。"""
        host = SP.site_key(url)
        p = SP.match_profile(url, title)
        if host:
            p2 = SP.get_profile(p.get("id") or "")
            p = p2 if p2 else p
        return p

    def _status(self, msg: str = "", **kw: Any) -> dict:
        self.last_msg = msg
        out = {
            "ok": True,
            "msg": msg,
            "engine": self.engine,
            "mode": self.mode,
            "modeName": MODE_NAMES.get(self.mode, self.mode),
            "zone": self.zone,
            "zoneName": self.zone,
            "label": self.current_name,
            "site": self.profile.get("name") or self.profile.get("id") or "",
            "siteId": self.profile.get("id") or "",
            "dialog": self.dialog,
            "editing": self.editing,
            "currentKey": self.current_key,
            "rect": self.current_rect,
            "zones": self.zones,
            "rejects": self.rejects,
        }
        out.update(kw)
        return out

    def _remember(self) -> None:
        if not self.current_key:
            return
        item = (self.current_zone, self.current_key, self.current_name)
        if self.history and self.history[-1][1] == item[1]:
            return
        self.history.append(item)
        if len(self.history) > 30:
            self.history = self.history[-30:]

    def _screen_rect(self, res: dict):
        """把 DOM 层给的 CSS 矩形换算成屏幕矩形。
        helper 现在只返回视口内 CSS 坐标(几何真值唯一来源), 屏幕坐标在这里算:
            screen = 客户区原点 + css * dpr
        客户区原点从 UIA 拿(hwnd 级别的真实值), 不用 window.screenX/screenY ——
        后者在不同 Chrome 版本里语义漂移(窗口左上角 vs 视口左上角), 用它会把
        标题栏高度算两遍。UIA 层返回的 rect 本来就是屏幕坐标, 直接透传。"""
        if not res:
            return None
        # UIA 层的 screen 已经是绝对屏幕坐标, 直接透传;
        # DOM 层的 screen 只是 CSS 坐标(helper 不再自行换算), 需要加窗口原点。
        if res.get("from_uia"):
            return res.get("screen")
        scr = res.get("screen")
        css = res.get("rect") or scr
        if not css:
            return None
        if isinstance(css, dict) and all(k in css for k in ("l", "t", "r", "b")):
            l, t, r, b = css["l"], css["t"], css["r"], css["b"]
        elif isinstance(css, dict) and "w" in css:
            l, t = css.get("l", 0), css.get("t", 0)
            r, b = l + css.get("w", 0), t + css.get("h", 0)
        else:
            return scr or css
        org = self._origin()
        if not org:
            return {"l": l, "t": t, "r": r, "b": b}
        ox, oy, scale = org
        return {
            "l": int(round(ox + l * scale)),
            "t": int(round(oy + t * scale)),
            "r": int(round(ox + r * scale)),
            "b": int(round(oy + b * scale)),
        }

    def _origin(self):
        """当前页面所在窗口的客户区原点。DOM 模式下 self.target 可能已经过期
        (用户切过窗口), 所以按标题重新找一次浏览器窗口再取原点。"""
        try:
            if self.engine == "dom" and hasattr(self.uia, "browser_window"):
                hwnd = self.uia.browser_window(self.title or "", self.url or "")
                if hwnd:
                    self.target = int(hwnd)
                    org = self.uia.client_origin(hwnd)
                    if org:
                        return org
            return self.uia.client_origin(self.target) if self.uia else None
        except Exception:
            return None

    def _apply_moved(self, res: dict) -> None:
        if not res or not res.get("moved"):
            return
        old_zone = self.current_zone
        old_key = self.current_key
        old_name = self.current_name
        if old_key:
            self.history.append((old_zone, old_key, old_name))
            if len(self.history) > 30:
                self.history = self.history[-30:]
        self.current_id = res.get("id") or ""
        self.current_key = res.get("key") or ""
        self.current_name = res.get("name") or res.get("role") or ""
        self.current_zone = res.get("zone") or self.zone
        self.zone = self.current_zone
        self.current_rect = self._screen_rect(res) or self.current_rect
        self.editing = bool(res.get("editing"))
        self.last_action = time.time()

    def _sync_dialog(self, st: dict) -> Optional[dict]:
        """弹窗开关 -> 区域栈。返回需要恢复的 (zone,key) 或 None。

        只有"弹窗里确实有可导航控件"才算进入弹窗。真站上大量浮层(推广条/
        cookie 提示/奖励弹窗)只是视觉覆盖、没有一个能聚焦的按钮, 一旦被判成
        DIALOG 就会把焦点锁死在里面, 用户按什么都没反应 —— 必应首页实测就是这样。
        """
        has = bool(st.get("dialog"))
        if has and not self.dialog and not self._dialog_has_controls():
            has = False
        if has and not self.dialog:
            self.dialog = True
            self.zone_stack.append((self.zone, self.current_key, self.current_name))
            self.zone = "DIALOG"
            self.current_key = ""
            self.current_id = ""
            self.current_name = ""
        elif not has and self.dialog:
            self.dialog = False
            if self.zone_stack:
                zone, key, name = self.zone_stack.pop()
                self.zone = zone or "MAIN"
                return {"zone": zone, "key": key, "name": name}
        return None

    def _dialog_has_controls(self) -> bool:
        """弹窗里有没有真正可聚焦的控件(按钮/输入框/链接)。
        DOM 还没连上时返回 False: 宁可不当弹窗, 也不要把焦点锁进一个空壳里。"""
        try:
            page = self.cdp.page
            if page is None or not page.alive():
                return False
            sc = page.scan(self._cfg(), "DIALOG") or {}
            return bool(sc.get("candidates"))
        except Exception:
            return False

    def _page_changed(self, st: dict) -> bool:
        url = (st or {}).get("url") or ""
        if not url:
            return False
        if not self.url:
            self.url = url
            return False
        if st.get("title") and st.get("title") != self.title:
            self.title = st.get("title") or self.title
        if SP.page_key(url) != SP.page_key(self.url):
            self.url = url
            self.current_id = ""
            self.current_key = ""
            self.current_name = ""
            self.current_rect = None
            self.history = []
            newp = self._profile_for(url, st.get("title") or "")
            if newp.get("id") != self.profile.get("id"):
                self.profile = newp
                self.zone = newp.get("default_zone") or "MAIN"
            return True
        self.url = url
        return False

    # ------------------------------------------------------------------ enter
    def enter(self, hwnd: int, url: str = "", title: str = "", exe: str = "") -> dict:
        with self.lock:
            self.target = int(hwnd or 0)
            self.url = url or ""
            self.title = title or ""
            self.profile = self._profile_for(self.url, self.title)
            self.engine = "none"
            self.zone = self.profile.get("default_zone") or "MAIN"
            self.zone_stack = []
            self.current_id = self.current_key = self.current_name = ""
            self.current_rect = None
            self.history = []
            self.dialog = False
            self.editing = False

            # 1) CDP DOM 语义层
            if exe.lower() in ("msedge.exe", "chrome.exe", "brave.exe", "vivaldi.exe", "opera.exe"):
                # 用窗口标题帮 CDP 挑对标签页: 调试端口下往往开着好几个浏览器窗口
                # (用户自己的、测试的), 不带线索会连到别的窗口上, 于是「明明在看这个
                # 页面, 方向键却在控制另一个页面」—— 表现为 zone_first 永远空。
                page = self.cdp.ensure(preferred_url=self.title or self.url, force=True)
                if page is None:
                    page = self.cdp.ensure(force=True)
                if page is not None:
                    try:
                        st = page.state(self._cfg())
                        # 窗口标题拿不到 URL: CDP 连上后用真实 URL 重定 Profile,
                        # 保证「切换站点后 profile 正确」, 不靠标题猜
                        real_url = st.get("url") or ""
                        if real_url:
                            self.url = real_url
                            self.title = st.get("title") or self.title
                            newp = self._profile_for(self.url, self.title)
                            if newp.get("id") != self.profile.get("id"):
                                self.profile = newp
                                self.zone = newp.get("default_zone") or "MAIN"
                        self._sync_dialog(st)
                        if st.get("editing") and st.get("editing_room"):
                            self.editing = True
                        res = page.zone_first(self._cfg(), self.zone)
                        if not res.get("moved"):
                            # 默认区没有候选（登录页/空页）：按 Profile 区域顺序找第一个非空区域
                            for z in self._candidate_zone_order():
                                if z == self.zone:
                                    continue
                                res = page.zone_first(self._cfg(), z)
                                if res.get("moved"):
                                    self.zone = z
                                    break
                        if res.get("moved"):
                            self.engine = "dom"
                            self._apply_moved(res)
                            self.zones = (page.scan(self._cfg()).get("zones") or {})
                            self.last_scan = time.time()
                            return self._status(f"遥控就绪: {self.current_name or self.zone}")
                        self.engine = "dom"
                        return self._status("已连接网页语义层，但当前页没有可导航控件")
                    except Exception as e:
                        self.cdp.last_error = str(e)
                        self.cdp.close()

            # 2) UIA 层
            self.engine = "uia"
            res = self.uia.enter(self.target, self.profile, self.zone)
            if not res:
                for z in self._candidate_zone_order():
                    if z == self.zone:
                        continue
                    res = self.uia.enter(self.target, self.profile, z)
                    if res:
                        self.zone = z
                        break
            if res:
                label, rect, actual_zone = res
                self.zone = actual_zone or self.zone
                self.current_name = label or ""
                self.current_rect = rect
                self.current_zone = self.zone
                self.current_key = f"uia:{self.zone}:{label}:{int((rect or [0])[0])}:{int((rect or [0, 0])[1])}"
                return self._status(f"遥控就绪(UIA): {label}")
            return self._status("遥控未就绪：网页语义层与 UIA 都没有可用控件", ok=False)

    def _candidate_zone_order(self):
        p = self.profile.get("zone_priority") or list(SP.ZONES)
        out = [z for z in p if z != "DIALOG"]
        for z in ("COMPOSER", "SEARCH", "MAIN", "TOPBAR", "SIDEBAR", "PLAYER"):
            if z not in out:
                out.append(z)
        return out

    # ------------------------------------------------------------------ actions
    def action(self, action: str, data: Optional[dict] = None) -> dict:
        data = data or {}
        with self.lock:
            try:
                if action == "status":
                    return self._status(self.last_msg or "状态")
                if action == "mode":
                    idx = MODES.index(self.mode) if self.mode in MODES else 0
                    self.mode = MODES[(idx + 1) % len(MODES)]
                    return self._status(f"模式: {MODE_NAMES[self.mode]}")
                if action == "zone_next":
                    return self._switch_zone(+1)
                if action == "zone_prev":
                    return self._switch_zone(-1)
                if action == "zone_set":
                    return self._set_zone(str(data.get("zone") or ""))
                if action == "focus_back":
                    return self._focus_back()
                if action in DIRS:
                    return self._move(action, force=bool(data.get("force")))
                if action == "ok":
                    return self._ok()
                if action == "back":
                    return self._back()
                if action == "rescan":
                    return self._rescan()
                return self._status(f"未知导航动作: {action}", ok=False)
            except Exception as e:
                return self._status(f"导航异常: {e}", ok=False)

    def _dom_active(self) -> bool:
        return self.engine == "dom" and self.cdp.page is not None and self.cdp.page.alive()

    def _dom_sync(self) -> Optional[dict]:
        if not self._dom_active():
            return None
        try:
            st = self.cdp.page.state(self._cfg())
        except Exception as e:
            self.cdp.last_error = str(e)
            self.cdp.close()
            return None
        if not st:
            return None
        changed = self._page_changed(st)
        restore = self._sync_dialog(st)
        # 空输入框不算"编辑态": 页面自动聚焦搜索框时, 若不区分, 方向键会被全部吞掉,
        # 用户按什么都没反应(必应首页实测)。只有在框里真有内容/选中时才让权给文本编辑。
        self.editing = bool(st.get("editing")) and bool(st.get("editing_room"))
        act = st.get("active") or {}
        # 只有当页面焦点确实是我们上次选中的那个元素时才回写 ——
        # 否则(比如页面自动聚焦了搜索框)会用"别人的焦点"覆盖掉我们的导航位置,
        # 于是 name 和 rect 分属两个不同元素, 焦点框画到空地上。
        if act.get("key") and act.get("key") == self.current_key:
            self.current_name = act.get("name") or self.current_name
            if act.get("rect") or act.get("screen"):
                self.current_rect = self._screen_rect(act) or self.current_rect
        if restore and restore.get("key"):
            self.current_key = restore.get("key")
            self.current_id = ""
        if changed or restore:
            try:
                res = self.cdp.page.restore(self._cfg(), self.current_key, self.zone) if self.current_key \
                    else self.cdp.page.zone_first(self._cfg(), self.zone)
                if res.get("moved"):
                    self._apply_moved(res)
            except Exception:
                pass
        return st

    def _move(self, direction: str, force: bool = False) -> dict:
        # 先同步页面状态（换页/弹窗/编辑态）
        if self.engine == "dom":
            st = self._dom_sync()
            if st is None:
                # DOM 层掉线：本次降级 UIA，不混用两套候选
                self.engine = "uia"
        if self.engine == "uia":
            r = self.uia.editing()
            self.editing = bool(r)
        # 输入框编辑态：方向键交还应用（free 模式与长按 force 例外）
        if self.editing and not force and self.mode != "free" and direction in DIRS:
            self.uia.press_direction(direction)
            return self._status(f"[文本区·方向键] {self.current_name or '输入框'}")

        zone = self.zone if self.mode != "free" else "*"
        if self.dialog:
            zone = "DIALOG"
        if self.engine == "dom" and self._dom_active():
            try:
                res = self.cdp.page.nav(self._cfg(), direction, zone, self.current_id, self.mode)
            except Exception as e:
                self.cdp.last_error = str(e)
                self.cdp.close()
                self.engine = "uia"
                res = None
            if res is not None:
                if res.get("moved"):
                    self._apply_moved(res)
                    return self._status(f"→ {self.current_name or self.zone}")
                reason = res.get("reason") or "zone-end"
                msg = {
                    "editing": f"[文本区·方向键] {self.current_name or '输入框'}",
                    "zone-empty": f"{self.zone} 区没有可导航控件",
                    "zone-end": f"{self.zone} 区该方向已到边缘",
                    "group-end": "输入区功能组已到头",
                    "stale": "元素已失效，已重新扫描",
                    "focus-failed": "目标无法聚焦",
                }.get(reason, f"该方向没有更多元素({reason})")
                return self._status(msg, moved=False)
        # UIA
        res = self.uia.nav(direction, self.profile, self.zone, force)
        if not res:
            return self._status(f"{self.zone} 区该方向已到边缘", moved=False)
        label, rect, moved, actual_zone = res
        if not moved:
            return self._status("该方向没有更多元素", moved=False)
        zone = actual_zone or self.zone
        self._apply_moved({
            "id": "", "key": f"uia:{zone}:{label}:{int((rect or [0])[0])}:{int((rect or [0, 0])[1])}",
            "name": label, "zone": zone, "screen": rect, "editing": self.uia.editing(),
            "from_uia": True, "moved": True,
        })
        return self._status(f"→ {label}")

    def _switch_zone(self, step: int) -> dict:
        order = [z for z in self._candidate_zone_order() if z != "DIALOG"]
        if self.engine == "dom" and self._dom_active():
            try:
                zs = self.cdp.page.scan(self._cfg()).get("zones") or {}
                self.zones = zs
                order = [z for z in order if zs.get(z)]
            except Exception:
                pass
        if not order:
            return self._status("没有可切换的区域")
        try:
            idx = order.index(self.zone)
        except ValueError:
            idx = -1
        now = time.time()
        for i in range(1, len(order) + 1):
            z = order[(idx + i * step) % len(order)]
            res = self._focus_zone(z)
            if res:
                # 切区保留 history: 「回上一焦点」要能跨区回去, 清空就等于没有回退键
                if self.current_key:
                    self.history.append((self.current_zone, self.current_key, self.current_name))
                    if len(self.history) > 30:
                        self.history = self.history[-30:]
                self.zone_stack.append((self.zone, self.current_key, self.current_name))
                self.zone = z
                self.current_zone = z
                self.current_name = res.get("name") or res.get("label") or ""
                self.current_rect = res.get("screen") or res.get("rect")
                self.current_key = res.get("key") or self.current_key
                self.current_id = res.get("id") or ""
                self.last_action = now
                return self._status(f"区域: {z}")
        return self._status(f"{self.zone} 区不可用，其他区域也没有控件")

    def _set_zone(self, zone: str) -> dict:
        zone = (zone or "").upper().strip()
        if zone not in SP.ZONES:
            return self._status(f"未知区域: {zone}", ok=False)
        res = self._focus_zone(zone)
        if not res:
            return self._status(f"{zone} 区没有可导航控件", ok=False)
        if self.current_key and self.current_zone != zone:
            self.history.append((self.current_zone, self.current_key, self.current_name))
            if len(self.history) > 30:
                self.history = self.history[-30:]
        self.zone_stack.append((self.zone, self.current_key, self.current_name))
        self.zone = zone
        self.current_zone = zone
        self.current_name = res.get("name") or res.get("label") or ""
        self.current_rect = res.get("screen") or res.get("rect")
        self.current_key = res.get("key") or self.current_key
        self.current_id = res.get("id") or ""
        return self._status(f"区域: {zone}")

    def _focus_zone(self, zone: str) -> Optional[dict]:
        if self.engine == "dom" and self._dom_active():
            try:
                res = self.cdp.page.zone_first(self._cfg(), zone)
                return res if res.get("moved") else None
            except Exception:
                return None
        res = self.uia.zone_first(self.target, self.profile, zone)
        if not res:
            return None
        label, rect = res[0], res[1]
        return {"name": label, "label": label, "rect": rect, "screen": rect, "key": "", "id": ""}

    def _focus_back(self) -> dict:
        while self.history:
            zone, key, name = self.history.pop()
            if key == self.current_key:
                continue
            res = None
            if self.engine == "dom" and self._dom_active():
                try:
                    res = self.cdp.page.restore(self._cfg(), key, zone)
                except Exception:
                    res = None
            if res and res.get("moved"):
                self._apply_moved(res)
                return self._status(f"回到: {self.current_name or name}")
            if self.engine == "uia":
                prev_zone = self.zone
                r = self.uia.zone_first(self.target, self.profile, zone)
                if r:
                    self.zone = zone
                    self.current_name = r[0]
                    self.current_rect = r[1]
                    self.current_zone = zone
                    self.current_key = key
                    return self._status(f"回到: {self.current_name}")
                self.zone = prev_zone
        return self._status("没有上一焦点")

    def _ok(self) -> dict:
        if self.editing:
            ok_mode = ((self.profile.get("composer") or {}).get("ok") or "enter")
            if ok_mode == "send" and self.engine == "dom" and self._dom_active():
                res = self.cdp.page.activate(self._cfg(), self.current_id)
                if res.get("ok"):
                    return self._status("已点击发送")
            self.uia.press_key("enter")
            return self._status("已回车确认")
        if self.engine == "dom" and self._dom_active():
            try:
                res = self.cdp.page.activate(self._cfg(), self.current_id)
                if res.get("ok"):
                    return self._status(f"已激活 {res.get('name') or ''}".strip())
            except Exception:
                pass
        if self.uia.activate():
            return self._status("已激活当前元素")
        self.uia.press_key("enter")
        return self._status("已回车确认")

    def _back(self) -> dict:
        if self.dialog:
            self.uia.press_key("esc")
            time.sleep(0.12)
            if self.engine == "dom" and self._dom_active():
                try:
                    st = self.cdp.page.state(self._cfg())
                    if not st.get("dialog"):
                        restore = self._sync_dialog(st)
                        if restore:
                            self._focus_zone(self.zone)
                            return self._status(f"弹窗已关闭，回到 {self.zone}")
                except Exception:
                    pass
            return self._status("已返回（弹窗）")
        self.uia.press_key("esc")
        return self._status("已返回")

    def _rescan(self) -> dict:
        if self.engine == "dom" and self._dom_active():
            try:
                st = self.cdp.page.state(self._cfg())
                self._page_changed(st)
                sc = self.cdp.page.scan(self._cfg(), self.zone)
                self.zones = sc.get("zones") or {}
                self.rejects = sc.get("rejects") or {}
                return self._status(f"已重扫: {len(sc.get('candidates') or [])} 个候选")
            except Exception as e:
                return self._status(f"重扫失败: {e}", ok=False)
        return self._status("UIA 层将在下次方向键重扫")

    def shutdown(self) -> None:
        try:
            self.cdp.close()
        except Exception:
            pass
        self.target = 0
        self.engine = "none"
        self.current_id = self.current_key = ""
        self.current_rect = None
        self.history = []
        self.dialog = False
