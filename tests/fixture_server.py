# -*- coding: utf-8 -*-
"""本地 fixture 静态服务器: 给语义导航自动化测试提供稳定页面。

用法:
    python tests/fixture_server.py [port]        # 默认 9751

页面在 tests/fixtures/ 下, 走 http://127.0.0.1:<port>/<name>.html
"""
import os
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "fixtures")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):
        pass


def serve(port=9751):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


if __name__ == "__main__":
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 9751
    print(f"fixture server: http://127.0.0.1:{p}/main.html")
    srv = serve(p)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
