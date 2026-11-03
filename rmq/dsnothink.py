#!/usr/bin/env python3
"""dsnothink：127.0.0.1:7678 → DeepSeek 的 Anthropic 兼容端点，把每个 /messages 请求的 thinking 改成 disabled。

rmq 驾驶手机每一步只是「读屏 → 点哪」，不需要思考；DeepSeek Flash 默认开思考，而 claude -p 会自己带上思考参数，
MAX_THINKING_TOKENS=0 / alwaysThinkingEnabled=false 都关不掉（11-01 实测），只能在中间改请求。
密钥原样透传，这里不读不存。用法：python3 dsnothink.py [--port 7678] [--up api.deepseek.com] [--prefix /anthropic]
（复核走 GLM 时：--port 7679 --up open.bigmodel.cn --prefix /api/anthropic）
"""
import http.client, json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


UP, PREFIX, PORT = arg("--up", "api.deepseek.com"), arg("--prefix", "/anthropic"), int(arg("--port", "7678"))
HOP = {"host", "content-length", "connection", "transfer-encoding", "keep-alive", "proxy-connection"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok"); return
        self.fwd("GET")

    def do_POST(self):
        self.fwd("POST")

    def fwd(self, method):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        if method == "POST" and self.path.split("?")[0].endswith("/messages"):
            try:
                j = json.loads(body)
                j["thinking"] = {"type": "disabled"}
                body = json.dumps(j, ensure_ascii=False).encode()
            except ValueError:
                pass
        hdr = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        c = http.client.HTTPSConnection(UP, timeout=600)
        try:
            c.request(method, PREFIX + self.path, body=body if method == "POST" else None, headers=hdr)
            r = c.getresponse()
            self.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() not in HOP:
                    self.send_header(k, v)
            self.send_header("connection", "close")
            self.end_headers()
            while chunk := r.read1(65536):  # SSE 流式逐块转发
                self.wfile.write(chunk); self.wfile.flush()
        except Exception as e:  # noqa: BLE001
            try:
                self.send_error(502, f"dsnothink 上游出错：{e}")
            except Exception:
                pass
        finally:
            c.close()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
