# -*- coding: utf-8 -*-
"""最小 Chromium CDP 客户端（无扩展、无 Playwright 依赖）。

- 通过 http://127.0.0.1:9222/json/list 发现页面 target；
- 每条 page target 一条 WebSocket，Runtime.evaluate 注入 nav_helper.js；
- 自动选择 `document.hasFocus()` 为 true 的页面（多窗口/多标签时不会控错页）；
- 连接断开/页面刷新/执行上下文销毁自动重连并重新注入 helper。

浏览器需要以调试端口启动：
  msedge.exe --remote-debugging-port=9222 --user-data-dir=... 
或 chrome.exe 同上。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import websocket  # websocket-client
    HAS_WS = True
except Exception:  # pragma: no cover
    HAS_WS = False

BASE = Path(__file__).resolve().parent
HELPER_FILE = BASE / "nav_helper.js"
_HELPER_SRC: Optional[str] = None
_HELPER_LOCK = threading.Lock()


def helper_source() -> str:
    global _HELPER_SRC
    if _HELPER_SRC is None:
        with _HELPER_LOCK:
            if _HELPER_SRC is None:
                _HELPER_SRC = HELPER_FILE.read_text(encoding="utf-8")
    return _HELPER_SRC


class CDPError(RuntimeError):
    pass


class CDPPage:
    def __init__(self, ws_url: str, target_id: str, url: str = "") -> None:
        self.ws_url = ws_url
        self.target_id = target_id
        self.url = url
        self._ws = None
        self._id = 0
        self._lock = threading.RLock()
        self._helper_ready = False
        self._last_used = 0.0

    # --------------------------------------------------------------- transport
    def connect(self, timeout: float = 2.5) -> None:
        if not HAS_WS:
            raise CDPError("未安装 websocket-client (pip install websocket-client)")
        self.close()
        self._ws = websocket.create_connection(
            self.ws_url, timeout=timeout, suppress_origin=True,
            enable_multithread=False,
        )
        self._helper_ready = False

    def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        self._helper_ready = False

    def alive(self) -> bool:
        return self._ws is not None and getattr(self._ws, "connected", False)

    def send(self, method: str, params: Optional[dict] = None, timeout: float = 3.0) -> dict:
        with self._lock:
            if not self.alive():
                self.connect()
            self._id += 1
            mid = self._id
            payload = {"id": mid, "method": method, "params": params or {}}
            try:
                self._ws.send(json.dumps(payload, ensure_ascii=False))
                self._ws.settimeout(timeout)
                deadline = time.time() + timeout
                while time.time() < deadline:
                    raw = self._ws.recv()
                    if not raw:
                        break
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if msg.get("id") != mid:
                        continue  # event
                    if "error" in msg:
                        raise CDPError(str(msg["error"].get("message") or msg["error"]))
                    return msg.get("result") or {}
                raise CDPError(f"CDP 超时: {method}")
            except websocket.WebSocketTimeoutException as e:
                raise CDPError(f"CDP 超时: {method} ({e})")
            except (websocket.WebSocketConnectionClosedException, OSError) as e:
                self.close()
                raise CDPError(f"CDP 连接断开: {e}")

    def evaluate(self, expression: str, timeout: float = 3.0) -> Any:
        res = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": False,
            "userGesture": True,
            "timeout": int(timeout * 1000),
        }, timeout=timeout)
        if res.get("exceptionDetails"):
            det = res["exceptionDetails"]
            text = (det.get("exception") or {}).get("description") or det.get("text") or "JS 异常"
            raise CDPError(str(text)[:500])
        return (res.get("result") or {}).get("value")

    # --------------------------------------------------------------- helper
    def ensure_helper(self) -> None:
        # 不短路上「已注入」标志: 页面刷新/跳转后执行上下文换了, 标志还在但
        # window.__KZQ_NAV 已经没了。一次 typeof 探测很便宜, 每次实测才可靠。
        probe = ("typeof window.__KZQ_NAV === 'object' && "
                 "typeof window.__KZQ_NAV.version === 'number' && "
                 "window.__KZQ_NAV.version >= 3")
        try:
            if self.evaluate(probe):
                self._helper_ready = True
                return
        except CDPError:
            # 执行上下文销毁（页面刚刷新）：重连一次
            self.close()
            self.connect()
        src = helper_source()
        self.evaluate(src, timeout=5.0)
        if not self.evaluate(probe):
            raise CDPError("nav_helper.js 注入失败")
        self._helper_ready = True

    def call(self, fn: str, *args: Any, timeout: float = 4.0) -> Any:
        self.ensure_helper()
        payload = json.dumps([a for a in args], ensure_ascii=False)
        expr = f"window.__KZQ_NAV.{fn}.apply(window.__KZQ_NAV, {payload})"
        try:
            return self.evaluate(expr, timeout=timeout)
        except CDPError as e:
            # 页面刚导航：重连 + 重注入 + 重试一次
            if "Execution context" in str(e) or "Cannot find context" in str(e) or "断" in str(e):
                self.close()
                self.connect()
                self.ensure_helper()
                return self.evaluate(expr, timeout=timeout)
            raise

    # --------------------------------------------------------------- high level
    def scan(self, cfg: dict, zone: Optional[str] = None) -> dict:
        return self.call("scan", cfg, {"zone": zone}) or {}

    def state(self, cfg: dict) -> dict:
        return self.call("state", cfg) or {}

    def nav(self, cfg: dict, direction: str, zone: str, current_id: str, mode: str) -> dict:
        return self.call("nav", cfg, direction, zone, current_id or "", mode) or {}

    def zone_first(self, cfg: dict, zone: str) -> dict:
        return self.call("zoneFirst", cfg, zone) or {}

    def restore(self, cfg: dict, key: str, zone: str) -> dict:
        return self.call("restore", cfg, key, zone) or {}

    def activate(self, cfg: dict, nav_id: str) -> dict:
        return self.call("activate", cfg, nav_id or "") or {}

    def zoom_rect(self, rect: dict) -> dict:
        """把页面内视口矩形换算成屏幕物理像素（近似值，最终以 UIA 焦点矩形为准）。"""
        return rect or {}


class CDPManager:
    """发现 127.0.0.1:<port> 上的浏览器，并维护当前焦点页面会话。"""

    def __init__(self, port: Optional[int] = None) -> None:
        self.port = int(port or os.environ.get("KZQ_CDP_PORT") or 9222)
        self.page: Optional[CDPPage] = None
        self._lock = threading.RLock()
        self.last_error = ""

    # --------------------------------------------------------------- discovery
    def version(self, timeout: float = 0.6) -> Optional[dict]:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/version", timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            self.last_error = str(e)
            return None

    def available(self) -> bool:
        return HAS_WS and self.version() is not None

    def targets(self, timeout: float = 0.8) -> List[dict]:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/list", timeout=timeout) as r:
                items = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            self.last_error = str(e)
            return []
        out = []
        for t in items if isinstance(items, list) else []:
            if t.get("type") != "page":
                continue
            u = t.get("url") or ""
            if u.startswith("devtools://") or u.startswith("chrome-extension://"):
                continue
            out.append(t)
        return out

    @staticmethod
    def _url_key(u: str) -> str:
        u = (u or "").lower()
        for pref in ("https://", "http://"):
            if u.startswith(pref):
                u = u[len(pref):]
        return u.split("#")[0].rstrip("/")

    def _pick_focused(self, preferred_url: str = "") -> Optional[dict]:
        items = self.targets()
        if not items:
            return None
        # 1) 地址栏 URL 匹配（避免多窗口时控错浏览器）
        if preferred_url:
            pk = self._url_key(preferred_url)
            cand = [t for t in items if self._url_key(t.get("url") or "") == pk]
            if len(cand) == 1:
                return cand[0]
            if cand:
                items = cand + [t for t in items if t not in cand]
            else:
                # 传进来的可能是窗口标题(不是 URL): 调试端口下常同时开着好几个
                # 浏览器窗口, 必须按标题挑对标签页 —— 挑错了就会"方向键控制的是
                # 另一个页面", 表现出来是选区永远为空。
                key = preferred_url.strip().lower()
                frag = key[:24]
                tcand = [t for t in items if frag and frag in (t.get("title") or "").lower()]
                if tcand:
                    items = tcand
        # 2) document.hasFocus()
        for t in items:
            page = CDPPage(t.get("webSocketDebuggerUrl") or "", t.get("id") or "", t.get("url") or "")
            if not page.ws_url:
                continue
            try:
                page.connect(timeout=1.2)
                focused = page.evaluate("document.hasFocus()", timeout=1.0)
                if focused is True:
                    page._last_used = time.time()
                    return t
            except Exception:
                pass
            finally:
                page.close()
        return items[0]

    def ensure(self, preferred_url: str = "", force: bool = False) -> Optional[CDPPage]:
        with self._lock:
            if not force and self.page is not None and self.page.alive():
                # 已连接但页面焦点可能变了：只在 URL 明显不匹配时重新发现
                if not preferred_url or self._url_key(preferred_url) in self._url_key(self.page.url):
                    self.page._last_used = time.time()
                    return self.page
            t = self._pick_focused(preferred_url)
            if not t:
                self.last_error = "没有可用的 page target"
                return None
            ws = t.get("webSocketDebuggerUrl") or ""
            if not ws:
                self.last_error = "target 缺少 webSocketDebuggerUrl"
                return None
            self.page = CDPPage(ws, t.get("id") or "", t.get("url") or "")
            try:
                self.page.connect()
                self.page.ensure_helper()
            except Exception as e:
                self.last_error = f"CDP 连接失败: {e}"
                self.page.close()
                self.page = None
                return None
            return self.page

    def open_url(self, url: str, timeout: float = 8.0) -> Optional[CDPPage]:
        """新建标签页打开 url 并连上它(测试/调试用)。
        用 /json/new?<url> 建标签: 老版本 Chrome 要 PUT, 新版本 PUT/GET 都行,
        所以先 PUT 再退回 GET。"""
        target = None
        for method in ("PUT", "GET"):
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{self.port}/json/new?{urllib.parse.quote(url, safe='')}",
                    method=method)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    target = json.loads(r.read().decode("utf-8", "replace"))
                break
            except Exception as e:
                self.last_error = str(e)
                continue
        if not target:
            page = self.ensure(force=True)
            if page is None:
                return None
            page.send("Page.navigate", {"url": url})
            return page
        ws = target.get("webSocketDebuggerUrl") or ""
        if not ws:
            self.last_error = "new target 缺少 webSocketDebuggerUrl"
            return None
        self.close()
        page = CDPPage(ws, target.get("id") or "", target.get("url") or url)
        page.connect()
        self.page = page
        time.sleep(1.2)
        page.ensure_helper()
        return page

    def close(self) -> None:
        with self._lock:
            if self.page:
                self.page.close()
            self.page = None

    def status(self) -> dict:
        v = self.version()
        return {
            "ok": v is not None and HAS_WS,
            "port": self.port,
            "browser": (v or {}).get("Browser", ""),
            "protocol": (v or {}).get("Protocol-Version", ""),
            "error": self.last_error,
        }
