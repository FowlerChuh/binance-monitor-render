"""Render entrypoint: 健康检查 HTTP 服务 + 监控主循环。

Render 的 Free Web Service 要求进程绑定 $PORT 并对健康检查返回 200，
否则会被判定为部署失败。监控逻辑本身是常驻循环，所以这里用一个后台线程
跑 HTTP 服务，主线程继续跑 monitor.main()。
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import monitor


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib naming)
        path = self.path.split("?", 1)[0]
        if path in ("/", "/health", "/healthz"):
            body = json.dumps(
                {
                    "ok": True,
                    "service": "binance-copy-monitor",
                    "portfolio": monitor.PORTFOLIO_ID,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, fmt, *args):
        # 保活请求每分钟一次，不写日志，避免刷屏
        pass


def start_health_server() -> ThreadingHTTPServer:
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="health")
    thread.start()
    return server


if __name__ == "__main__":
    start_health_server()
    monitor.main()
