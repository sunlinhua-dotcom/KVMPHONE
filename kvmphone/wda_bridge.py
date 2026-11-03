#!/usr/bin/env python3
"""kp 的 WebDriverAgent 后端：iPhone 用数据线插在本机（Mac mini），WDA 跑在手机上，经 usbmux 转到 127.0.0.1:8100。

接口和 bridge.mjs / ikvm_bridge 一样：GET /health  GET /frame?q=  GET /read（截图 + 苹果 OCR）  POST /act {"ops":[…]}  GET /quit
坐标是截图像素（长边缩到 1080），桥里换算成 WDA 的点（point）。

和另外两个后端的区别：
  - 没有鼠标指针，点按是手指触摸；划动走贝塞尔浅弧、起手快收尾慢（像人手），时长不变。
  - 中文 / 任意文字直接输入（op type），不用剪贴板、不用切拼音。
  - op launch 按包名开 App；系统权限弹窗（允许 / Allow）由 WDA 自动点允许，别的弹窗不碰。

手机端先拉起 WDA：~/wda/start.sh（pymobiledevice3 developer dvt xcuitest + usbmux forward 8100）。
  python3 wda_bridge.py [--port 7692] [--wda http://127.0.0.1:8100]
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import random
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIR = os.path.dirname(os.path.realpath(__file__))
OCR_BIN = os.environ.get("KP_OCR") or os.path.join(DIR, "ocr")
RUN = "/tmp/kvmphone/wda"
# 只自动点「允许」类按钮；付款确认之类的弹窗不在这里面，交给 kp 的护栏
ALLOW = ["允许", "Allow", "允许一次", "Allow Once", "使用App时允许", "使用 App 时允许", "Allow While Using App",
         "仅在使用期间允许", "允许访问", "允许完全访问", "Allow Full Access", "允许访问所有照片", "Allow Access to All Photos"]
APPS = {  # kp open 的常用名 → 包名；也可以直接给包名
    "淘宝": "com.taobao.taobao4iphone", "taobao": "com.taobao.taobao4iphone",
    "京东": "com.360buy.jdmobile", "jd": "com.360buy.jdmobile",
    "微信": "com.tencent.xin", "wechat": "com.tencent.xin",
    "美团": "com.meituan.imeituan", "meituan": "com.meituan.imeituan",
    "饿了么": "me.ele.ios.eleme", "拼多多": "com.xunmeng.pinduoduo", "支付宝": "com.alipay.iphoneclient",
    "高德": "com.autonavi.amap", "抖音": "com.ss.iphone.ugc.Aweme", "小红书": "com.xingin.discover",
    "safari": "com.apple.mobilesafari", "设置": "com.apple.Preferences", "settings": "com.apple.Preferences",
}


def human_path(x0, y0, x1, y1, n, bend=0.06):
    """手指划动：三次贝塞尔浅弧（往随机一侧弯）+ 起手快、松手时还在动的速度曲线。"""
    dx, dy = x1 - x0, y1 - y0
    d = math.hypot(dx, dy) or 1.0
    px, py, side = -dy / d, dx / d, random.choice((-1, 1))
    b1 = d * random.uniform(0.02, bend) * side
    b2 = b1 * random.uniform(0.3, 0.9)
    a1, a2 = random.uniform(0.2, 0.4), random.uniform(0.6, 0.8)
    c1 = (x0 + dx * a1 + px * b1, y0 + dy * a1 + py * b1)
    c2 = (x0 + dx * a2 + px * b2, y0 + dy * a2 + py * b2)
    out = []
    for i in range(1, n + 1):
        u = i / n
        t = 0.6 * u + 0.4 * math.sin(u * math.pi / 2)
        v = 1 - t
        k0, k1, k2, k3 = v * v * v, 3 * v * v * t, 3 * v * t * t, t * t * t
        out.append((k0 * x0 + k1 * c1[0] + k2 * c2[0] + k3 * x1, k0 * y0 + k1 * c1[1] + k2 * c2[1] + k3 * y1))
    return out


class WDA:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.sid = None
        self.pt = (414.0, 736.0)  # 手机屏幕的点尺寸，开会话后读真实值
        self.img = (608, 1080)  # 最近一张截图的像素尺寸
        self.seq = 0
        self.lock = threading.Lock()
        os.makedirs(RUN, exist_ok=True)
        self.ocr = subprocess.Popen([OCR_BIN], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) \
            if os.path.exists(OCR_BIN) else None

    def req(self, method: str, path: str, body=None, timeout=60):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors="replace")
            raise RuntimeError(f"WDA {method} {path} -> {e.code}: {txt[:300]}")

    def session(self) -> str:
        if self.sid:
            return self.sid
        caps = {"capabilities": {"alwaysMatch": {
            "defaultAlertAction": "accept",
            "acceptAlertButtonSelector": "**/XCUIElementTypeButton[`label IN {%s}`]" % ",".join(f"'{s}'" for s in ALLOW),
        }}}
        r = self.req("POST", "/session", caps)
        self.sid = r.get("sessionId") or r["value"]["sessionId"]
        # 不等 App 空闲 / 动画冷却：否则每个动作白等 1–2 秒
        self.req("POST", f"/session/{self.sid}/appium/settings",
                 {"settings": {"waitForIdleTimeout": 0, "animationCoolOffTimeout": 0}})
        sz = self.req("GET", f"/session/{self.sid}/window/size")["value"]
        self.pt = (float(sz["width"]), float(sz["height"]))
        return self.sid

    def s(self, method: str, path: str, body=None, timeout=60):
        """带会话的请求；WDA 重启后会话失效就重开一次。"""
        try:
            return self.req(method, f"/session/{self.session()}{path}", body, timeout)
        except RuntimeError as e:
            if "invalid session" not in str(e).lower() and "-> 404" not in str(e):
                raise
            self.sid = None
            return self.req(method, f"/session/{self.session()}{path}", body, timeout)

    def to_pt(self, x: float, y: float):
        return x * self.pt[0] / self.img[0], y * self.pt[1] / self.img[1]

    def frame(self, q: float = 0.85) -> dict:
        self.session()
        png = base64.b64decode(self.req("GET", "/screenshot", timeout=20)["value"])
        self.seq += 1
        src = os.path.join(RUN, "shot.png")
        f = os.path.join(RUN, f"f{self.seq % 50:02d}.jpg")
        with open(src, "wb") as fp:
            fp.write(png)
        subprocess.run(["sips", "-Z", "1080", "-s", "format", "jpeg", "-s", "formatOptions", str(int(q * 100)),
                        src, "--out", f], capture_output=True, check=True)
        out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", f], capture_output=True, text=True).stdout
        w = int(out.split("pixelWidth:")[1].split()[0])
        h = int(out.split("pixelHeight:")[1].split()[0])
        self.img = (w, h)
        subprocess.run(["cp", f, os.path.join(RUN, "last.jpg")])
        return {"L": 0, "R": w, "W": w, "H": h, "img": f}

    def read(self) -> dict:
        g = self.frame(0.9)
        if self.ocr:
            self.ocr.stdin.write(g["img"] + "\n")
            self.ocr.stdin.flush()
            g["items"] = json.loads(self.ocr.stdout.readline())
        else:
            g["items"] = []
        return g

    def touch(self, actions: list) -> None:
        self.s("POST", "/actions", {"actions": [{"type": "pointer", "id": "finger1",
                                                 "parameters": {"pointerType": "touch"}, "actions": actions}]})

    def act(self, ops: list) -> None:
        for op in ops:
            k = op["op"]
            if k == "tap":
                x, y = self.to_pt(op["x"], op["y"])
                hold = int(op.get("hold") or random.uniform(55, 110))
                self.touch([{"type": "pointerMove", "duration": 0, "x": x, "y": y},
                            {"type": "pointerDown", "button": 0},
                            {"type": "pause", "duration": hold},
                            {"type": "pointerUp", "button": 0}])
            elif k == "drag":
                x1, y1 = self.to_pt(op["x1"], op["y1"])
                x2, y2 = self.to_pt(op["x2"], op["y2"])
                # WDA 每段约 0.12 秒开销，段数封顶 6：贝塞尔弧线照样在，划一次约 1.6 秒
                n, ms = min(int(op.get("steps") or 6), 6), int(op.get("ms") or 260)
                acts = [{"type": "pointerMove", "duration": 0, "x": x1, "y": y1}, {"type": "pointerDown", "button": 0}]
                acts += [{"type": "pointerMove", "duration": max(1, ms // n), "x": px, "y": py}
                         for px, py in human_path(x1, y1, x2, y2, n)]
                acts.append({"type": "pointerUp", "button": 0})
                self.touch(acts)
            elif k == "type":
                self.s("POST", "/wda/keys", {"value": list(op["text"])})
            elif k == "key":
                mods, key = op.get("mods") or [], op["key"]
                if key == "KeyH" and any(m.startswith("Meta") for m in mods):
                    self.req("POST", "/wda/homescreen")
                elif key in ("Enter", "NumpadEnter"):
                    self.s("POST", "/wda/keys", {"value": ["\n"]})
                elif key == "Backspace":
                    self.s("POST", "/wda/keys", {"value": ["\b"]})
                elif key == "KeyV" and any(m.startswith("Meta") for m in mods):
                    raise RuntimeError("WDA 后端不走剪贴板：直接用 kp type / kp paste 打字")
                else:
                    raise RuntimeError(f"WDA 后端不支持按键 {'+'.join(mods + [key])}")
            elif k == "launch":
                app = op["app"]
                self.s("POST", "/wda/apps/launch", {"bundleId": APPS.get(app.lower(), APPS.get(app, app))})
            elif k == "wait":
                time.sleep(op["ms"] / 1000)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wda", default=os.environ.get("WDA_URL", "http://127.0.0.1:8100"))
    ap.add_argument("--port", type=int, default=7692)
    a = ap.parse_args()
    w = WDA(a.wda)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, code: int, obj: dict) -> None:
            b = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def handle_req(self) -> None:
            path, _, qs = self.path.partition("?")
            try:
                with w.lock:  # 和 bridge.mjs 一样串行
                    if path == "/health":
                        try:
                            st = w.req("GET", "/status", timeout=5)
                        except Exception as e:  # noqa: BLE001
                            return self.send(200, {"ok": False, "backend": "wda", "error": f"WDA 没在跑：{e}"})
                        return self.send(200, {"ok": bool(st.get("value", {}).get("ready", True)), "backend": "wda",
                                               "ios": st.get("value", {}).get("os", {}).get("version")})
                    if path == "/frame":
                        q = float(dict(p.split("=") for p in qs.split("&") if "=" in p).get("q", 0.85))
                        return self.send(200, w.frame(q))
                    if path == "/read":
                        return self.send(200, w.read())
                    if path == "/act" and self.command == "POST":
                        n = int(self.headers.get("content-length", 0))
                        t0 = time.time()
                        w.act(json.loads(self.rfile.read(n) or b"{}").get("ops", []))
                        return self.send(200, {"ok": True, "ms": int((time.time() - t0) * 1000)})
                    if path == "/quit":
                        self.send(200, {"ok": True})
                        threading.Thread(target=srv.shutdown).start()
                        return
                self.send(404, {"error": "not found"})
            except Exception as e:  # noqa: BLE001
                self.send(500, {"error": str(e)})

        do_GET = do_POST = handle_req

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print(f"wda kp bridge on 127.0.0.1:{a.port} -> {a.wda}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
