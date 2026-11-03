// kvmphone 常驻桥：在 EGO 里接管 GLKVM 页面，对本机 127.0.0.1:7690 提供
//   GET  /health               状态
//   GET  /frame?q=0.85         手机区域 JPEG（响应头 X-Crop: L,R）
//   GET  /read                 抓帧 + OCR，返回 {L,R,W,H,items:[{t,x,y,w,h,c}],img}
//   POST /act  {ops:[...]}     在页面内按时序发 HID；坐标一律是手机裁图像素
// 用法：ego-browser nodejs -e "await import('file:///…/bridge.mjs')" </dev/null
// 由 kp 命令启动和调用，别直接跑。
const http = await import("node:http");
const fs = await import("node:fs/promises");
const { spawn } = await import("node:child_process");
const path = await import("node:path");

const DIR = process.env.KP_DIR || decodeURIComponent(new URL(".", import.meta.url).pathname).replace(/\/$/, "");
const RUN = "/tmp/kvmphone";
const PORT = 7690;
const SPACE = Number((await fs.readFile(`${DIR}/.space`, "utf8").catch(() => "")).trim());
await fs.mkdir(RUN, { recursive: true });
// 桥脚本跑在 EGO 的 Node 子进程里，杀掉 ego-browser 命令行进程脚本并不会停（11-01 实测旧桥的看门狗还每分钟重载页面）。
// 所以每个桥启动时登记自己的编号，发现登记的不是自己（有更新的桥起来了）就自己停手，不再碰页面
const ME = `${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
const OWNF = `${RUN}/bridge.owner`;
await fs.writeFile(OWNF, ME);
const isOwner = async () => (await fs.readFile(OWNF, "utf8").catch(() => "")).trim() === ME;

// .space 缺失或那个空间已被关掉时，新建一个并记下来（本机与 Mac mini 通用）
let task;
try { task = SPACE ? await taskSpace(SPACE) : null; } catch (e) { task = null; }
if (!task) { task = await taskSpace("RMQ1 手机桥"); await fs.writeFile(`${DIR}/.space`, String(task.spaceId) + "\n"); }
const page = task.page("p1");
let crop = { L: 0 };
// 指针当前在哪（手机裁图像素）；点击和拖动都会更新，下一次从这里出发
let pos = null;
// 记住 GLKVM 的登录会话：令牌存本机 600 文件，浏览器重启丢了 cookie 就补回；
// GLKVM 重启会清掉所有令牌，这时用用户自己经 glkvmpass 存的密码文件（600）重新登录，不再要人手登
const TOKF = `${DIR}/.glkvm_token`;
const PASSF = `${process.env.HOME}/.config/kvmphone/glkvm_pass`;
const atLogin = async () => /\/(login|init)/.test(String(await page.url()));
// 每次整页重载记一笔原因（排查页面频繁刷新用），文件 /tmp/kvmphone/reload.log
const rlog = (m) => fs.appendFile(`${RUN}/reload.log`, `${new Date().toISOString()} pid=${process.pid} ${m}\n`).catch(() => {});
const reload = async () => { rlog("reload " + (new Error().stack.split("\n")[2] || "").trim()); await page.goto("about:blank"); await page.goto("https://glkvm.local/#/"); await page.waitForTimeout(3000); };
const putToken = (t) => page.evaluate((t) => {
  localStorage.setItem("gl-kvm-token-key", t); localStorage.setItem("gl-kvm-token-keys", JSON.stringify({ glkvm: t }));
  document.cookie = `auth_token=${t}; path=/; secure; samesite=none`;
}, t);
let badPw = "";
async function connect() {
  const u0 = String(await page.url());
  if (!u0.includes("glkvm.local") || await atLogin()) {
    // 整页重载（只改 # 不会重载）；别处登录过时 cookie 共用，重载就能进
    await reload();
    const tok = (await fs.readFile(TOKF, "utf8").catch(() => "")).trim();
    if (await atLogin() && tok) { await putToken(tok); await reload(); }
    const pw = (await fs.readFile(PASSF, "utf8").catch(() => "")).replace(/\r?\n$/, "");
    if (await atLogin() && pw && pw !== badPw) {
      const t = await page.evaluate(async (pw) => {
        const f = new FormData(); f.append("user", "admin"); f.append("passwd", pw);
        const r = await fetch("/api/auth/login", { method: "POST", body: f });
        const j = await r.json().catch(() => ({}));
        return j?.result?.token || j?.token || "";
      }, pw);
      if (t) { await putToken(t); await reload(); console.log("GLKVM 已用存的密码自动重登"); }
      else badPw = pw;  // 密码不对就别反复试（GLKVM 失败多次会锁登录），等用户重新 glkvmpass
    }
    if (await atLogin()) throw new Error("GLKVM_LOGIN：GLKVM 没登录，存的会话失效且自动重登没成功（没存密码就在终端敲 glkvmpass 存一次）");
    await page.waitForSelector("video", { timeout: 20000 });
  }
  // 画面流可能还没出第一帧（videoWidth 为 0 时抓帧会报错），等它出来
  await page.waitForFunction(() => (document.querySelector("video")?.videoWidth || 0) > 0, undefined, { timeout: 30000 });
  const t = await page.evaluate(() => localStorage.getItem("gl-kvm-token-key") || (document.cookie.match(/(?:^|; )auth_token=([^;]+)/) || [])[1] || "");
  if (t) { await fs.writeFile(TOKF, t + "\n", { mode: 0o600 }); await fs.chmod(TOKF, 0o600); }
}

// 页面内：截获 /api/ws 套接字，提供 HID 原语与抓帧
const INJECT = () => {
  if (window.__kp) return "already";
  const o = WebSocket.prototype.send;
  WebSocket.prototype.send = function (d) {
    if (String(this.url).includes("/api/ws")) window.__kvmws = this;
    return o.call(this, d);
  };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let crop = { L: 0, R: 1919 };
  const abs = (cx, cy) => {
    // 两种映射（devices.json 的 mouse）：frame = 绝对坐标铺满整个 1920×1080 画面（11-01 上午以前）；
    // phone = 铺满手机自己的屏幕（11-01 下午实测变成这样，原因未明）。横向点偏就切另一种
    const fx = window.__kpMouse === "phone" ? cx / (crop.R - crop.L + 1) : (crop.L + cx) / 1920;
    return { x: Math.round(fx * 65535 - 32768), y: Math.round((cy / 1080) * 65535 - 32768) };
  };
  const KEY = (ch) => {
    if (/^[a-z]$/.test(ch)) return ["Key" + ch.toUpperCase(), false];
    if (/^[A-Z]$/.test(ch)) return ["Key" + ch, true];
    if (/^[0-9]$/.test(ch)) return ["Digit" + ch, false];
    // 美式键盘全部符号（11-01 之前缺 : ? = _ & 等，网址里的冒号、问号会被静默丢掉）
    const SYM = { " ": "Space", "\n": "Enter", "\t": "Tab", "-": "Minus", "=": "Equal", "[": "BracketLeft", "]": "BracketRight",
      "\\": "Backslash", ";": "Semicolon", "'": "Quote", "`": "Backquote", ",": "Comma", ".": "Period", "/": "Slash" };
    const SHIFT = { "_": "Minus", "+": "Equal", "{": "BracketLeft", "}": "BracketRight", "|": "Backslash", ":": "Semicolon",
      '"': "Quote", "~": "Backquote", "<": "Comma", ">": "Period", "?": "Slash",
      "!": "Digit1", "@": "Digit2", "#": "Digit3", "$": "Digit4", "%": "Digit5", "^": "Digit6", "&": "Digit7", "*": "Digit8", "(": "Digit9", ")": "Digit0" };
    return SYM[ch] ? [SYM[ch], false] : SHIFT[ch] ? [SHIFT[ch], true] : null;
  };
  // 本固件的 HID 只认二进制帧（JSON 事件会被忽略）：
  //   [1, state, ...键名ASCII]  [2, state, ...按钮名ASCII]  [3, x int16 BE, y int16 BE]
  const B = (type, state, name) => window.__kvmws.send(new Uint8Array([type, state ? 1 : 0, ...new TextEncoder().encode(name)]).buffer);
  const hid = {
    move: (cx, cy) => {
      const p = abs(cx, cy), b = new DataView(new ArrayBuffer(5));
      b.setUint8(0, 3); b.setInt16(1, p.x); b.setInt16(3, p.y);
      window.__kvmws.send(b.buffer);
    },
    btn: (state) => B(2, state, "left"),
    key: (key, state) => B(1, state, key),
  };
  // 像人手指 / 鼠标那样走：三次贝塞尔弧线（往随机一侧弯 4–12% 距离）+ 两头慢中间快的速度曲线，
  // 最后 1–2 步带一点点修正。步数和总时长跟原来的直线差不多，不拖慢操作。
  // iOS 会截断大跨度的绝对坐标跳变，所以单步仍控制在 30 左右。
  let cur = null;
  const rnd = (a, b) => a + Math.random() * (b - a);
  const speed = (t) => 0.5 * t + 0.5 * (0.5 - 0.5 * Math.cos(Math.PI * t));  // 线性与正弦各半，峰速约 1.3 倍
  const bez = (p0, p1, p2, p3, t) => {
    const u = 1 - t;
    return u * u * u * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t * t * t * p3;
  };
  const curvePts = (x0, y0, x1, y1, n, bendMax = 0.12, prof = speed) => {
    const dx = x1 - x0, dy = y1 - y0, d = Math.hypot(dx, dy) || 1;
    const px = -dy / d, py = dx / d, side = Math.random() < 0.5 ? -1 : 1;
    const b1 = d * rnd(0.04, bendMax) * side, b2 = b1 * rnd(0.3, 0.9);
    const c1x = x0 + dx * rnd(0.2, 0.4) + px * b1, c1y = y0 + dy * rnd(0.2, 0.4) + py * b1;
    const c2x = x0 + dx * rnd(0.6, 0.8) + px * b2, c2y = y0 + dy * rnd(0.6, 0.8) + py * b2;
    const out = [];
    for (let i = 1; i <= n; i++) {
      const t = prof(i / n);
      out.push([bez(x0, c1x, c2x, x1, t), bez(y0, c1y, c2y, y1, t)]);
    }
    return out;
  };
  const glide = async (x, y) => {
    if (!cur) { hid.move(x, y); cur = { x, y }; await sleep(30); }
    const d = Math.hypot(x - cur.x, y - cur.y);
    if (d < 4) { hid.move(x, y); cur = { x, y }; return; }
    const n = Math.max(3, Math.ceil(d / 20));
    for (const [px, py] of curvePts(cur.x, cur.y, x, y, n)) { hid.move(px, py); await sleep(10); }
    cur = { x, y };
  };
  // 像人移鼠标：三次贝塞尔弧线（往随机一侧弯）+ 最小急动度速度曲线（起步、收尾慢，中间快），
  // 远距离 1/4 概率先冲过头几像素再拉回。时长按 Fitts 定律随距离增长，再 ±15% 随机，封顶 300 ms，
  // 和原来固定的 0.18 s 差不多，不拖慢。
  // 在页面里直接发 HID、页面自己掐时间：EGO 的 page.mouse.move 每隔一次要卡约 1 s（11-01 实测），一条轨迹会拖到 10–20 s。
  // 最小急动度中段速度约是平均的 1.9 倍，步数按这个算，保证单步不超过 22 像素，iOS 不会截断。
  const minJerk = (t) => t * t * t * (10 - 15 * t + 6 * t * t);
  function humanPath(x0, y0, x1, y1) {
    const dx = x1 - x0, dy = y1 - y0, d = Math.hypot(dx, dy) || 1, px = -dy / d, py = dx / d;
    const T = Math.min(300, Math.max(90, (70 + 42 * Math.log2(1 + d / 25)) * rnd(0.85, 1.15)));
    const over = d > 60 && Math.random() < 0.25 ? rnd(3, 7) : 0;
    const ex = x1 + (dx / d) * over, ey = y1 + (dy / d) * over;
    const side = Math.random() < 0.5 ? -1 : 1, b1 = d * rnd(0.03, 0.14) * side, b2 = b1 * rnd(0.2, 0.9);
    const c1 = [x0 + dx * rnd(0.2, 0.4) + px * b1, y0 + dy * rnd(0.2, 0.4) + py * b1];
    const c2 = [x0 + dx * rnd(0.6, 0.8) + px * b2, y0 + dy * rnd(0.6, 0.8) + py * b2];
    const bz = (a, b, c, e, t) => { const u = 1 - t; return u * u * u * a + 3 * u * u * t * b + 3 * u * t * t * c + t * t * t * e; };
    const n = Math.max(4, Math.ceil(T / 16), Math.ceil((d * 1.9) / 22)), pts = [];
    for (let i = 1; i <= n; i++) {
      const s = minJerk(i / n), j = i < n ? 0.5 : 0;  // 途中带一点手抖，最后一点不抖
      pts.push([bz(x0, c1[0], c2[0], ex, s) + rnd(-j, j), bz(y0, c1[1], c2[1], ey, s) + rnd(-j, j), (T * i) / n]);
    }
    if (over) { const k = Math.round(rnd(2, 4)), t1 = rnd(35, 70); for (let i = 1; i <= k; i++) pts.push([ex + (x1 - ex) * minJerk(i / k), ey + (y1 - ey) * minJerk(i / k), T + (t1 * i) / k]); }
    return pts;
  }
  const humanGlide = async (x, y) => {
    if (!cur) { const s0 = { x: x + rnd(-90, 90), y: y + rnd(60, 160) }; await glide(s0.x, s0.y); }
    const t0 = performance.now();
    for (const [px, py, due] of humanPath(cur.x, cur.y, x, y)) {
      const w = t0 + due - performance.now(); if (w > 1) await sleep(w);
      hid.move(px, py);
    }
    cur = { x, y };
  };
  // 小缩略图（灰度 96×208）：比两帧差多少，用来判画面静止 / 读屏缓存是否还有效
  const TW = 96, TH = 208, tc = document.createElement("canvas");
  tc.width = TW; tc.height = TH;
  const tg = tc.getContext("2d", { willReadFrequently: true });
  const thumb = () => {
    const v = document.querySelector("video");
    tg.drawImage(v, crop.L, 0, crop.R - crop.L + 1, v.videoHeight, 0, 0, TW, TH);
    const d = tg.getImageData(0, 0, TW, TH).data, g = new Uint8Array(TW * TH);
    for (let i = 0; i < g.length; i++) g[i] = (d[i * 4] * 3 + d[i * 4 + 1] * 6 + d[i * 4 + 2]) / 10;
    return g;
  };
  // 变化像素数（灰度差 > 18 算变；H.264 静止画面的压缩噪声在这之下）
  const ndiff = (a, b) => { let n = 0; for (let i = 0; i < a.length; i++) if (Math.abs(a[i] - b[i]) > 18) n++; return n; };
  let readThumb = null;
  window.__kp = {
    setCrop(c) { crop = c; },
    setCur(x, y) { cur = { x, y }; },
    markRead() { readThumb = thumb(); },
    // 上次读屏之后画面没变（变化像素 ≤ 3）就返回 true，读屏可直接用缓存
    unchanged() { return !!readThumb && ndiff(readThumb, thumb()) <= 3; },
    // 等画面静止：先等 min ms，之后每 40 ms 比一次，连续 quiet ms 变化 < 1.2% 就返回；超过 max 返回 stable=false
    async settle(min = 150, quiet = 200, max = 1500) {
      const t0 = performance.now(); await sleep(min);
      let prev = thumb(), since = null;
      const trace = [];  // 每次比较的变化像素数，排障用
      while (true) {
        await sleep(40);
        const c = thumb(), n = ndiff(prev, c), now = performance.now(); prev = c; trace.push(n);
        if (n < c.length * 0.012) { since ??= now; if (now - since >= quiet) return { stable: true, ms: Math.round(now - t0), trace }; }
        else since = null;
        if (now - t0 > max) return { stable: false, ms: Math.round(now - t0), trace };
      }
    },
    frame(q) {
      // 只画中线一行找手机区域、再直接从视频裁出手机那一块编码，不先画整帧 1920×1080 再拷一遍
      const v = document.querySelector("video"), VW = v.videoWidth, VH = v.videoHeight;
      const cv = (window.__kpCv ??= { row: document.createElement("canvas"), out: document.createElement("canvas") });
      if (cv.row.width !== VW) { cv.row.width = VW; cv.row.height = 1; cv.rg = cv.row.getContext("2d", { willReadFrequently: true }); }
      cv.rg.drawImage(v, 0, (VH * 0.5) | 0, VW, 1, 0, 0, VW, 1);
      const row = cv.rg.getImageData(0, 0, VW, 1).data;
      let L = -1, R = -1;
      for (let x = 0; x < VW; x++) {
        const i = x * 4, r = row[i], gg = row[i + 1], b = row[i + 2];
        const flat = Math.abs(r - gg) < 6 && Math.abs(gg - b) < 6 && (r < 20 || (r > 150 && r < 200));
        if (!flat) { if (L < 0) L = x; R = x; }
      }
      // 中线那一行找不到手机边（模糊背景、白底页面等）：沿用上一次找到的边界，没有才退回整幅
      if (L < 0 || R - L < 200) {
        const c0 = window.__kpCrop0;  // 设备表里记的默认边界（深色页面中线全黑时用）
        if (crop.R - crop.L < VW - 100) ({ L, R } = crop); else if (c0) [L, R] = c0; else { L = 0; R = VW - 1; }
      }
      crop = { L, R };
      const w = R - L + 1, o = cv.out;
      if (o.width !== w || o.height !== VH) { o.width = w; o.height = VH; }
      o.getContext("2d").drawImage(v, L, 0, w, VH, 0, 0, w, VH);
      return { L, R, W: w, H: VH, data: o.toDataURL("image/jpeg", q).split(",")[1] };
    },
    async run(ops) {
      if (!window.__kvmws || window.__kvmws.readyState !== 1) throw new Error("kvm ws not ready");
      for (const op of ops) {
        switch (op.op) {
          case "tap":
            // 停稳再按，按压时长也随机
            await humanGlide(op.x, op.y); await sleep(op.pre ?? rnd(55, 100));
            hid.btn(true); await sleep(op.hold ?? rnd(60, 115)); hid.btn(false);
            break;
          case "drag": {
            // iOS：按下后要立刻有位移，否则会被识别成长按
            const n = op.steps ?? 20, ms = op.ms ?? 260;
            // 手指划动是一道浅弧、起手快收尾慢，不是一条直线。
            // op.stop：拖到位先停住再松手（起步仍有位移防长按），iOS 不带惯性，画面松手即停，等稳定快得多
            const prof = op.stop ? (t) => 0.08 * t + 0.92 * t * t * t * (10 - 15 * t + 6 * t * t) : (t) => 0.6 * t + 0.4 * Math.sin((t * Math.PI) / 2);
            await glide(op.x1, op.y1); await sleep(rnd(40, 70)); hid.btn(true);
            const t0 = performance.now();
            let i = 0;
            for (const [px, py] of curvePts(op.x1, op.y1, op.x2, op.y2, n, 0.06, prof)) {
              const w = t0 + (ms * ++i) / n - performance.now(); if (w > 0) await sleep(w);
              hid.move(px, py);
            }
            // 停住期间每 16 ms 报一次同一位置，让 iOS 算出的松手速度是 0
            if (op.stop) for (const end = performance.now() + op.stop; performance.now() < end;) { hid.move(op.x2, op.y2); await sleep(16); }
            cur = { x: op.x2, y: op.y2 };
            await sleep(op.settle ?? 30); hid.btn(false);
            break;
          }
          case "key": {
            const mods = op.mods || [];
            for (const m of mods) hid.key(m, true);
            hid.key(op.key, true); await sleep(40); hid.key(op.key, false);
            for (const m of mods.reverse()) hid.key(m, false);
            await sleep(40);
            break;
          }
          case "type":
            for (const ch of op.text) {
              const k = KEY(ch); if (!k) continue;
              if (k[1]) hid.key("ShiftLeft", true);
              // 节奏像人：按键时长与间隔每次随机，平均和原来一样
              hid.key(k[0], true); await sleep(22 + Math.random() * 18); hid.key(k[0], false);
              if (k[1]) hid.key("ShiftLeft", false);
              await sleep((op.gap ?? 45) * (0.55 + Math.random() * 0.9));
            }
            break;
          case "wait":
            await sleep(op.ms);
            break;
          case "cur":
            cur = { x: op.x, y: op.y };
            break;
        }
      }
      return cur;
    },
  };
  return "installed";
};
let __booted = false;
const KPVER = "15";
// 鼠标映射：桥跑在 EGO 的 Node 里，环境变量不一定传得进来，直接读设备表里 glkvm 那台的 mouse
const GDEV = await fs.readFile(`${DIR}/devices.json`, "utf8").then((t) => Object.values(JSON.parse(t).devices).find((d) => d.kind === "glkvm") || {}).catch(() => ({}));
const MOUSE = GDEV.mouse || "frame";
// 登录 + 注入 + 等 HID 套接字；GLKVM 重启或掉线后由看门狗和每次 /read、/act 前自动重跑
async function ensure() {
  if (!(await isOwner())) throw new Error("STALE_BRIDGE：已有更新的桥接管页面，这个旧桥不再操作");
  // 页面里的注入带版本号：桥升级后旧页面里残留的老 __kp 会被换掉
  const ok = await page.evaluate((v) => window.__kpVer === v && window.__kvmws?.readyState === 1, KPVER).catch(() => false);
  if (ok && !(await atLogin())) return;
  rlog("ensure: 注入或套接字失效 url=" + (await page.url().catch(() => "?")));
  // GLKVM 前端被别的网页客户端挤掉时会自己刷新页面（11-01 实测一分钟好几次）。这时页面还在 GLKVM 上，
  // 就地重新注入即可，不必再整页重载（那要 5 秒）；就地不行再走整页重载 + 登录
  const here = __booted && String(await page.url().catch(() => "")).includes("glkvm.local") && !(await atLogin().catch(() => true));
  for (const full of here ? [false, true] : [__booted]) {
    try {
      if (full) await reload();
      await connect();
      await inject();
      return;
    } catch (e) { if (full || !here) throw e; console.log("就地重注入失败，整页重载", String(e && e.message || e).slice(0, 120)); }
  }
}
async function inject() {
  await page.evaluate(() => { delete window.__kp; });
  console.log("inject", await page.evaluate(INJECT));
  await page.evaluate(([v, k, c0]) => { window.__kpVer = v; window.__kpMouse = k; window.__kpCrop0 = c0; }, [KPVER, MOUSE, GDEV.crop || null]);
  // 套接字要等前端发一次消息才截得到：在画面上把指针挪到它现在的位置，前端就会发一条移动
  const g = await page.evaluate(() => { const v = document.querySelector("video"); const b = v.getBoundingClientRect(); return { x: b.x, y: b.y, s: v.videoWidth / b.width }; });
  const at = pos || [250, 540];
  await page.mouse.move(g.x + (at[0] + crop.L) / g.s, g.y + at[1] / g.s);
  pos = at;
  await page.waitForFunction(() => !!window.__kvmws && window.__kvmws.readyState === 1, undefined, { timeout: 20000 });
}
await ensure();
__booted = true;

// 常驻 OCR 子进程
const ocr = spawn(`${DIR}/ocr`, [], { stdio: ["pipe", "pipe", "inherit"] });
let ocrBuf = "", ocrQ = [];
ocr.stdout.on("data", (d) => {
  ocrBuf += d;
  let i;
  while ((i = ocrBuf.indexOf("\n")) >= 0) {
    const line = ocrBuf.slice(0, i); ocrBuf = ocrBuf.slice(i + 1);
    const cb = ocrQ.shift(); if (cb) cb(line);
  }
});
const runOcr = (file) => new Promise((res) => { ocrQ.push((l) => res(JSON.parse(l))); ocr.stdin.write(file + "\n"); });

// 点击走网页界面（GLKVM 前端自己的移动 / 节流逻辑在 iOS 上最可靠），其余直发
const rnd = (a, b) => a + Math.random() * (b - a);
async function runOps(ops) {
  // 指针真实位置在页面里（cur）；页面自刷新后注入是新的，用 Node 记的 pos 补回去
  const c = await page.evaluate((o) => window.__kp.run(o), pos ? [{ op: "cur", x: pos[0], y: pos[1] }, ...ops] : ops);
  if (c) pos = [c.x, c.y];
}

let seq = 0, lastRead = null;
async function grab(q = 0.85) {
  const f = await page.evaluate((q) => window.__kp.frame(q), q);
  const file = path.join(RUN, `f${String(++seq % 50).padStart(2, "0")}.jpg`);
  await fs.writeFile(file, Buffer.from(f.data, "base64"));
  await fs.copyFile(file, path.join(RUN, "last.jpg"));
  crop = { L: f.L, R: f.R };
  return { L: f.L, R: f.R, W: f.W, H: f.H, img: file };
}

const body = (req) => new Promise((res) => { let b = ""; req.on("data", (d) => (b += d)); req.on("end", () => res(b ? JSON.parse(b) : {})); });
const send = (res, code, obj) => { res.writeHead(code, { "content-type": "application/json; charset=utf-8" }); res.end(JSON.stringify(obj)); };

let busy = Promise.resolve();
const serial = (fn) => (busy = busy.then(fn, fn));

const server = http.createServer(async (req, res) => {
  const u = new URL(req.url, "http://x");
  try {
    if (u.pathname === "/health") return send(res, 200, { ok: true, space: SPACE, ws: await page.evaluate(() => window.__kvmws?.readyState) });
    if (u.pathname === "/frame") {
      const r = await serial(() => grab(Number(u.searchParams.get("q") || 0.85)));
      return send(res, 200, r);
    }
    if (u.pathname === "/read") {
      // ?settle=最少等的毫秒：先在页面里比缩略图等画面静止（0.2 s 不动），再只 OCR 一次
      // ?cache=1：上次读屏后画面没变（15 秒内）就直接返回上次结果，省一次 OCR
      const st = u.searchParams.get("settle"), useCache = u.searchParams.get("cache") === "1";
      const once = async () => {
        await ensure();
        if (useCache && lastRead && Date.now() - lastRead.at < 15000 && await page.evaluate(() => window.__kp.unchanged())) return { ...lastRead.r, cached: true };
        const s = st !== null ? await page.evaluate(([m]) => window.__kp.settle(m, 250, 2500), [Number(st) || 150]) : null;
        // 缩略图要和读屏那一帧同时截：OCR 要 0.4 s，期间画面变了的话缓存会以为没变、返回旧结果
        const g = await grab(0.9); await page.evaluate(() => window.__kp.markRead());
        g.items = await runOcr(g.img);
        if (s) { g.stable = s.stable; g.settle_ms = s.ms; g.trace = s.trace; }
        lastRead = { at: Date.now(), r: g };
        return g;
      };
      const r = await serial(() => once().catch((e) => /navigated|closed|context|not ready|chrome-error/i.test(String(e && e.message || e)) ? once() : Promise.reject(e)));
      return send(res, 200, r);
    }
    if (u.pathname === "/act" && req.method === "POST") {
      const b = await body(req);
      const t0 = Date.now();
      await serial(async () => { await ensure(); await runOps(b.ops); });
      return send(res, 200, { ok: true, ms: Date.now() - t0 });
    }
    // 退出要整个进程退：只关 HTTP 的话看门狗还在，会按旧版本号把新桥的页面整页重载（10-31 实测 mini 上残留 6 个桥互相刷页面）
    if (u.pathname === "/quit") { send(res, 200, { ok: true }); clearInterval(watchdog); server.close(); ocr.kill(); setTimeout(() => process.exit(0), 300); return; }
    send(res, 404, { error: "not found" });
  } catch (e) {
    console.log("error", req.url, String(e && e.message || e).slice(0, 300));
    send(res, 500, { error: String(e && e.message || e) });
  }
});
// 先抓一帧，让页面内坐标映射有手机区域
await grab();
await new Promise((r) => server.listen(PORT, "127.0.0.1", r));
// 看门狗：每 60 秒查一次，GLKVM 重启 / 掉登录后自己重登，不等下一条命令
const watchdog = setInterval(async () => {
  if (!(await isOwner())) { clearInterval(watchdog); server.close(); ocr.kill(); console.log("有更新的桥接管，旧桥退出"); return; }
  serial(() => ensure().catch((e) => console.log("watchdog", String(e && e.message || e))));
}, 60000);
console.log(`kvmphone bridge ready on ${PORT}, space ${SPACE}`);
await new Promise((r) => server.on("close", r));
console.log("bridge closed");
