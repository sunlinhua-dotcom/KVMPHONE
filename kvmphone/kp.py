#!/usr/bin/env python3
"""kp —— 经 GLKVM 操控 iPhone 的命令行（客户端，背后是 bridge.mjs 常驻桥）。

坐标一律是「手机裁图像素」：宽约 500、高 1080，左上原点，和 kp read 输出一致。
每个动作默认在之后等一下再 OCR 一次并打印，省掉单独的读屏回合（-q 关掉）。

  kp [--dev 设备] 命令…            选手机（也可设 KP_DEV）；不写用设备表 default。kp devs 列设备
  kp start | stop | health
  kp who | lock [秒] [--note 说明] [--force] | unlock [--force]   看 / 占 / 放这台手机的租约
  kp read [-g 关键词]              OCR 当前屏，每行「中心x,中心y  文字」
  kp look [-g 关键词]              读屏 + JEV 判屏（屏类型 → 下一步；结算页自动附价格）
  kp shot                          只抓帧，打印图片路径（要看图时用）
  kp tap 文字 [-n 第几个] [-e]     按文字点（-e 精确匹配）；或 kp tap X Y
  kp type pinyin                   打字（ASCII，经 iOS 输入法；带中文会自动改走 paste）
  kp paste 中文                    打中文：经 iPhone 拼音输入法敲拼音、点候选词，打完切回英文（WDA 后端直接打字）
  kp open 小红书 双妹              直达 App 的搜索结果页（KVM 后端经 Safari 敲 URL scheme，表在 schemes.json；不给词就直达首页）
  kp key Enter|Backspace|Space ... 按键，可写 Meta+KeyA 这类组合
  kp up [N] / kp down [N]          上滑 / 下滑 N 次（看下面 / 看上面的内容）
  kp back                          从左边缘右滑返回
  kp price                        读结算页 / 购物车栏的价格结构（只读）
  kp judge 模板                    把当前屏 OCR 文本交给 JEV 做闭集判断
  kp app [名字] / kp apps          看当前（或指定）App 的说明 / 列出有说明的 App（knowledge/apps/）
  kp handoff "原因" [--wait 秒]    登录、验证码、滑块等交给用户：截图推 decide 等用户处理完回话（默认等 900 秒）
                                   退出码 0 处理好了（先核实再继续）/ 10 用户自己做完了 / 11 跳过 / 12 取消任务 / 13 用户写了话（照办）/ 124 没人来
  kp learn "App：事实"             记一条这个 App 的事实（只记现象，不记规定）

每个动作做完都会自动跑 JEV phone_screen，首行打印「[JEV xxms] 屏=… → 下一步」。
借鉴 Mika 的附加能力在 kpx.py：第一次进某个 App 自动附上说明；风控严的 App（pace: careful）和发送 / 打招呼这类
对外动作自动放慢（KP_PACE=off 关）；验证 / 限流 / 登录页给出 [风控] [登录] 提示；验证码、卡号、证件号打码；
rmq 任务里 [用户插话] [用户叫停] 由 kp 转达。
多调用方：rmq 任务、Claude 直控、OpenClaw 共用一台手机时按租约排他。动作命令（tap/type/paste/key/up/down/back）
先拿租约，空闲 120 秒自动过期；别人占着就退出码 75 并说是谁。调用方身份取 KP_OWNER（rmq 自动设 rmq:<任务id>，
OpenClaw 请设 KP_OWNER=openclaw），不设时 Claude 会话里是 claude、其余是 manual。读屏不拿租约。
护栏：提交订单 / 去支付 / 开通月付 / 付费购买等付钱类按钮，kp 一律拒点（退出码 3）；付钱交给用户确认。
"""
import json, os, re, subprocess, sys, time, urllib.error, urllib.request, unicodedata

BASE = os.environ.get("KP_BASE", "http://127.0.0.1:7690")  # 设了 KP_BASE 就直连那个桥，不查设备表、不转发、不拿租约
DIR = os.path.dirname(os.path.realpath(__file__))
DEVF = os.environ.get("KP_DEVICES") or os.path.join(DIR, "devices.json")
DEV, DEVCFG = "rmq1", {}  # main() 按 --dev / KP_DEV / 设备表 default 定下来；DEVCFG 为空 = 不自动起桥
LOG = "/tmp/kvmphone/bridge.log"
ACTS = {"tap", "type", "paste", "key", "up", "down", "back", "open", "handoff"}
sys.path.insert(0, DIR)
import kpx
TTL = 120


def call(path, data=None, timeout=30, auto=True):
    req = urllib.request.Request(BASE + path, data=None if data is None else json.dumps(data).encode(),
                                 headers={"content-type": "application/json"}, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        # 桥没在跑（被关 / 机器重启）就自己拉起再试一次；桥里会用存的令牌或密码自动登 GLKVM
        if not auto or not DEVCFG or not isinstance(getattr(e, "reason", None), ConnectionRefusedError):
            raise
        start()
        return call(path, data, timeout, auto=False)


def alive():
    try:
        return call("/health", timeout=3, auto=False).get("ok")
    except Exception:
        return False


def start():
    if alive():
        print("bridge 已在运行"); return
    try: call("/quit", timeout=3, auto=False); time.sleep(1)  # 端口上还挂着失效的旧桥（空间被关等）就先让它退
    except Exception: pass
    os.makedirs("/tmp/kvmphone", exist_ok=True)
    log = open(LOG, "w")
    argv = [a.replace("{DIR}", DIR) for a in DEVCFG["start"]] if DEVCFG.get("start") else \
        ["ego-browser", "nodejs", "-e", f"await import('file://{DIR}/bridge.mjs')"]
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                     env={**os.environ, "KP_DIR": DIR, "KP_OCR": os.path.join(DIR, "ocr")})
    for _ in range(60):
        time.sleep(0.5)
        if alive():
            print("bridge 已就绪"); return
    print("bridge 启动失败，日志：" + LOG); sys.exit(1)


def norm(s):
    return unicodedata.normalize("NFKC", s).replace(" ", "").lower()


def show(items, grep=None):
    # rmq 任务里把这一屏读到的文字记进 kp.jsonl：复核要靠它核对价格、店名的出处（11-01 前只记命令，查对了也被判「无出处」）
    klog(screen=" | ".join(it["t"] for it in items)[:4000])
    for it in items:
        if grep and norm(grep) not in norm(it["t"]):
            continue
        print(f'{it["x"] + it["w"] // 2},{it["y"] + it["h"] // 2}\t{kpx.redact(it["t"])}')


def read(grep=None, show_=True):
    r = call("/read")
    if show_:
        show(r["items"], grep)
    return r


def settle(min_wait=0.15, timeout=6.0):
    """动作后等画面稳定。先由桥在页面里比缩略图（每 40 ms 一次，0.25 s 不动就算稳），只 OCR 一次；
    画面一直在动（轮播、视频）或字太少（还在加载）才退回老办法：连续两次 OCR 文本一致。"""
    t0 = time.time()
    r = call(f"/read?settle={int(min_wait * 1000)}")
    if r.get("stable") and len(r["items"]) >= 3:
        return r
    sig = lambda r: {it["t"] for it in r["items"][2:]}  # 去掉状态栏时间 / 电量
    last = sig(r)
    while time.time() - t0 < timeout:
        time.sleep(0.2)
        r = call("/read")
        # OCR 有抖动、页面有轮播，按文字集合重合度 ≥ 85% 判稳定，不要求逐字相同
        s = sig(r)
        if len(s & last) >= 0.85 * max(len(s | last), 1) and len(r["items"]) >= 3:
            return r
        last = s
    return r


# 本机的 JEV 先试；mini 上的连不上上游（typesafe 在国内不通，backend=none），再试 Mac 的 rmq relay 反向映射过来的 7681（11-01）
JEV_URLS = [os.environ["JEVJUDGE_URL"]] if os.environ.get("JEVJUDGE_URL") else ["http://127.0.0.1:7677", "http://127.0.0.1:7681"]


HAN = re.compile(r"[\u3400-\u9fff]+")


def zh_type(text, quiet=False, grep=None):
    """中文经 iPhone 的拼音输入法打：ASCII 段英文模式直接敲；汉字段切到拼音、敲拼音、在候选栏点中目标词，
    候选只盖住一部分就点最长的前缀、剩下的接着点；实在没有就按空格上屏第一个并提示核对。打完切回英文。
    输入法状态记在 /tmp/kvmphone/<设备>.ime；记错了（敲完没出候选）自动删掉重来。"""
    sys.path.insert(0, os.path.join(DIR, "vendor"))  # pypinyin 随 kp 放在 vendor/（mini 连不上 PyPI）
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        sys.exit("kp：缺 pypinyin（python3 -m pip install --user pypinyin）")
    stf = f"/tmp/kvmphone/{DEV}.ime"
    st = open(stf).read().strip() if os.path.exists(stf) else "?"
    key = lambda k, mods=(): call("/act", {"ops": [{"op": "key", "key": k, "mods": list(mods)}]})
    def now():  # 读屏上键盘：空格键写「拼 / 简体拼音」= 拼音，写「English」或只看得到 123 键 = 英文；看不到键盘返回 None
        its = [it for it in call("/read")["items"] if it["y"] > 850]
        k123 = next((it for it in its if it["t"].strip() == "123"), None)
        ts = [it["t"].strip() for it in its]
        if any(t in ("拼", "拼音") or "简体" in t for t in ts): return "zh", k123
        return ("en" if k123 or any(t.startswith("English") for t in ts) else None), k123
    def toggle(k123=None):
        # 11-01 实测 Ctrl+Space 时灵时不灵（吞键），点屏上键盘左下角的地球键稳定来回切；地球键在 123 键正下方偏左
        k123 = k123 or now()[1]
        if k123:
            call("/act", {"ops": [{"op": "tap", "x": max(20, k123["x"] + k123["w"] // 2 - 14), "y": k123["y"] + k123["h"] // 2 + 73}]})
            time.sleep(0.6)
        else:
            key("Space", ["ControlLeft"]); time.sleep(0.8)
    def ensure(want):  # 切完读空格键确认，最多试 3 次
        nonlocal st
        for _ in range(3):
            n, k = now()
            if n is None:
                if st != want: toggle()
                break
            if n == want: break
            toggle(k)
        st = want
    misses = []
    for seg in re.findall(r"[\u3400-\u9fff]+|[^\u3400-\u9fff]+", text):
        if not HAN.fullmatch(seg):
            if not seg.isascii():
                sys.exit(f"kp：「{seg}」既不是汉字也不是 ASCII，打不出来（全角标点请换成半角）")
            # 空格、数字在拼音模式下没有候选、直接上屏，不必来回切；带字母或符号的段才切英文
            if re.search(r"[A-Za-z]|[^\w\s]", seg): ensure("en")
            call("/act", {"ops": [{"op": "type", "text": seg}]}); continue
        py = "".join(lazy_pinyin(seg))
        before = {it["t"] for it in call("/read?cache=1")["items"]}
        ensure("zh")
        call("/act", {"ops": [{"op": "type", "text": py}]})
        new = lambda: [it for it in call("/read?settle=150")["items"] if it["t"] not in before and HAN.search(it["t"])]
        cands = new()
        if not cands:  # 敲出来的是字母不是候选：输入法其实在英文模式，删掉切过去重敲
            call("/act", {"ops": [{"op": "key", "key": "Backspace", "mods": []}] * len(py)})
            toggle(); call("/act", {"ops": [{"op": "type", "text": py}]}); cands = new()
        st = "zh"
        left = seg
        for _ in range(6):
            best, bk = None, 0
            for it in cands:
                k = 0
                while k < min(len(left), len(it["t"])) and left[k] == it["t"][k]: k += 1
                if k > bk or (k == bk and best and len(it["t"]) < len(best["t"])): best, bk = it, k
            if not best: break
            n = len(best["t"])  # OCR 把几个候选连成一条时，按字数比例点前 bk 个字的中间
            x = best["x"] + (best["w"] // 2 if bk == n else round(best["w"] * bk / 2 / n))
            call("/act", {"ops": [{"op": "tap", "x": x, "y": best["y"] + best["h"] // 2}]})
            left = left[bk:]
            if not left: break
            time.sleep(0.3); cands = new()
        if left:
            key("Space"); misses.append(seg)
    if st != "en": ensure("en")
    open(stf, "w").write(st)
    r = settle()
    if misses:
        print(f"[输入] 候选栏里没找到「{'」「'.join(misses)}」，已按空格上屏第一个候选，先核对输入框里的字")
    if not quiet:
        screen_hint(r["items"]); show(r["items"], grep)


def jev(template, items):
    """把 OCR 文本交给 JEV 做闭集判断，返回 (主答案, 派生动作, gate)。JEV 不在线或超时返回 None。
    直连常驻服务的 HTTP 接口（mini 上 jevjudge 命令不在 PATH，之前一直是「不可用」），2 秒没回就跳过。"""
    state = "\n".join(it["t"] for it in items)[:4000]
    for url in JEV_URLS:
        try:
            req = urllib.request.Request(url + "/judge", data=json.dumps({"template": template, "state": state}).encode(),
                                         headers={"content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=3.5) as r:  # 热的时候 0.6–1.5 s
                j = json.loads(r.read())
        except Exception:
            continue
        if j.get("answers"):  # 后端不通时服务回 backend=none、answers 为空，换下一个
            break
    else:
        return None
    ans = {k: (v.get("choice") or v.get("noul")) for k, v in (j.get("answers") or {}).items()}
    return ans, j.get("derived") or {}, j.get("gate"), j.get("latency_ms")


def klog(**kw):
    """测试记账：设了 KP_LOG 就把每条命令记一行 JSON（步数、JEV 耗时、护栏拦截）。"""
    if os.environ.get("KP_LOG"):
        with open(os.environ["KP_LOG"], "a") as f:
            f.write(kpx.redact(json.dumps({"ts": round(time.time(), 2), **kw}, ensure_ascii=False)) + "\n")


def screen_hint(items):
    """JEV phone_screen：一次约 0.6 s 判断当前屏类型，给出下一步。结算页顺手把价格结构也打出来。"""
    j = jev("phone_screen", items)
    if not j:
        print("[JEV] 不可用"); return None
    scr, nxt, ms = j[0].get("screen"), j[1].get("next"), j[3]
    print(f"[JEV {ms}ms] 屏={scr} → {nxt}")
    klog(jev_screen=scr, jev_ms=ms)
    if scr == "checkout":
        print("[价格] " + price_of(items))
    elif scr == "payment":
        print("[护栏] 这是支付页，立刻点左上角返回，不要做任何别的操作")
    return scr


def dbg(msg, t0):
    """KP_DEBUG=1 时把各段耗时打到 stderr，排查哪一步慢。"""
    if os.environ.get("KP_DEBUG"):
        print(f"[耗时] {msg} {time.time() - t0:.2f}s", file=sys.stderr)
    return time.time()


def act(ops, after=900, quiet=False, grep=None, target=None):
    """target 是这一下点的按钮文字（节奏按它认发送 / 打招呼这类对外动作）。"""
    t = time.time()
    kpx.before(lambda: call("/read?cache=1")["items"], target)
    call("/act", {"ops": ops})
    t = dbg("动作", t)
    if quiet and not os.environ.get("KP_SETTLE"):
        kpx.after(None, True, target); return
    r = settle()
    t = dbg(f"等稳定+读屏 stable={r.get('stable')} settle_ms={r.get('settle_ms')}", t)
    kpx.after(r["items"], True, target)
    if quiet:
        return
    screen_hint(r["items"])
    dbg("JEV", t)
    show(r["items"], grep)


# 护栏：只拦付钱类按钮（下单、付款、购买、开通月付 / 金融服务）；发送、分享、填信息等照常放行
import re
FORBID = re.compile(r"提交订单|去支付|立即支付|确认支付|确认付款|立即付款|付款|免密支付|极速支付|立即购买|立即下单|确认下单"
                    r"|立即开通|去开通|开通|月付"
                    r"|(?<!免拼)(?<!单独)(?<!拼单)(?<!拼团)购买"
                    # 繁体与英文界面（Mika 的付款识别也认这些）；按钮上带金额的「支付 ¥12」「Pay S$8.50」同样拦
                    r"|提交訂單|確認支付|確認付款|付款|立即購買|立即下單|確認下單|開通|(?<!免拼)(?<!單獨)購買"
                    r"|(?i:\b(pay now|place (your )?order|buy now|confirm (and )?pay(ment)?|complete (purchase|payment)|slide to pay"
                    r"|subscribe now)\b)"
                    r"|(?i:(支付|pay)\s*(S\$|HK\$|US\$|RM|\$|¥|￥|€|£)\s*\d)")
# 屏上出现金融 / 开通类字样时，连「确认使用」「同意」这类泛按钮也不点（美团月付诱导弹窗就是这样）
FIN_CTX = re.compile(r"月付|免密|开通|信用|分期|借款")
FIN_BTN = re.compile(r"确认使用|确认|同意|立即领取|领取并")


# 下单确认 / 收银台页（11-01 Gemini 4 在京东秒送结算页用坐标点击付了 ¥41.9：落点附近没认出禁点字样）。
# 这类页面上一律不许坐标点击，只许点左上角返回；按文字点也不许点「确定 / 确认 / 同意」这类泛按钮
CHECKOUT = re.compile(r"提交订单|确认订单|去支付|立即支付|确认支付|确认付款|收银台|京东支付|免密支付|支付方式|付款方式|应付金额|需付款|待支付"
                      r"|提交訂單|確認訂單|(?i:place order|checkout)")
GENERIC = re.compile(r"^(确定|确认|好的|同意|继续|完成|OK|Confirm|Continue)$", re.I)


def on_checkout(items):
    return next((it["t"] for it in items if CHECKOUT.search(it["t"])), None)


def forbidden(t, items):
    if FORBID.search(t):
        return True
    return bool(FIN_BTN.search(t)) and any(FIN_CTX.search(it["t"]) for it in items)


def deny(t):
    print(f"拒绝：「{t}」是下单 / 支付 / 开通 / 付费类按钮，付钱要用户自己确认，kp 不点")
    klog(guard=t); sys.exit(3)


def guard(items, x, y, coord=False):
    """点击前检查：落点所在或最近的文字是禁点按钮就拒绝。返回落点上的文字（节奏用）。
    下单确认 / 收银台页：坐标点击只放行左上角返回，按文字点也不放行泛「确定 / 确认」。"""
    page = on_checkout(items)
    if page and not (x < 90 and y < 160):
        near0 = [it["t"] for it in items if it["x"] - 30 <= x <= it["x"] + it["w"] + 30 and it["y"] - 25 <= y <= it["y"] + it["h"] + 25]
        if coord or any(GENERIC.match(t.strip()) for t in near0):
            print(f"拒绝：这是下单确认 / 支付页（屏上有「{page}」），只许读价格（kp price / kp read）和点左上角返回（kp back）")
            klog(guard=f"结算页:{page}"); sys.exit(3)
    near = [it for it in items if it["x"] - 30 <= x <= it["x"] + it["w"] + 30 and it["y"] - 25 <= y <= it["y"] + it["h"] + 25]
    for it in near:
        if forbidden(it["t"], items):
            deny(it["t"])
    near.sort(key=lambda it: abs(it["x"] + it["w"] / 2 - x) + abs(it["y"] + it["h"] / 2 - y))
    return near[0]["t"] if near else None


def find(items, text, nth=0, exact=False):
    k = norm(text)
    hits = [it for it in items if (norm(it["t"]) == k if exact else k in norm(it["t"]))]
    if not hits and not exact:
        return None
    # 精确相等优先，再按从上到下
    hits.sort(key=lambda it: (norm(it["t"]) != k, it["y"], it["x"]))
    return hits[nth] if nth < len(hits) else None


def load_devs():
    try:
        return json.load(open(DEVF))
    except Exception:
        return {"default": "rmq1", "hosts": {}, "devices": {"rmq1": {"kind": "glkvm", "host": "", "port": 7690}}}


def is_local(host, devs):
    """桥在本机就直接连；在别的机器（host 是 ssh 别名）就整条命令转过去。一台 KVM 只能有一个桥
    （多个 GLKVM 网页客户端会互相挤掉），所以各调用方都经桥所在那台机器。"""
    if not host or os.environ.get("KP_LOCAL"):
        return True
    import socket
    return socket.gethostname().split(".")[0] in devs.get("hosts", {}).get(host, [host])


def owner():
    return os.environ.get("KP_OWNER") or ("claude" if os.environ.get("CLAUDECODE") else "manual")


def lease(need, ttl=TTL, force=False, note=""):
    """一台手机同一时刻只归一个调用方。need=True 时拿 / 续租约（只延长不缩短），别人占着就退出码 75；
    need=False 只在别人占着时提示一句。租约文件在桥所在机器的 /tmp/kvmphone/<设备>.lease。"""
    import fcntl
    os.makedirs("/tmp/kvmphone", exist_ok=True)
    p = f"/tmp/kvmphone/{DEV}.lease"
    with open(p + ".lk", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            cur = json.load(open(p))
        except Exception:
            cur = None
        now, me = time.time(), owner()
        live = bool(cur and cur["until"] > now)
        mine = live and cur["owner"] == me
        if live and not mine and not force:
            msg = f"{DEV} 正被 {cur['owner']} 占用（{cur.get('note') or '无说明'}，最长还剩 {int(cur['until'] - now)} 秒）"
            if need:
                klog(busy=cur["owner"])
                sys.stderr.write(msg + f"；等它用完，或 kp --dev {DEV} lock --force 抢占\n"); sys.exit(75)
            sys.stderr.write("注意：" + msg + "\n"); return
        if need:
            json.dump({"owner": me, "until": max(now + ttl, cur["until"] if mine else 0),
                       "note": note or (cur.get("note", "") if mine else ""), "since": cur["since"] if mine else now},
                      open(p, "w"), ensure_ascii=False)


def lease_cmd(cmd, args):
    p = f"/tmp/kvmphone/{DEV}.lease"
    force = "--force" in args
    args = [a for a in args if a != "--force"]
    note = ""
    if "--note" in args:
        i = args.index("--note"); note = args[i + 1]; del args[i:i + 2]
    try:
        cur = json.load(open(p))
    except Exception:
        cur = None
    if cmd == "lock":
        lease(True, int(args[0]) if args else 600, force, note)
        print(f"{DEV} 已归 {owner()}"); return
    if cmd == "unlock":
        if cur and cur["owner"] != owner() and cur["until"] > time.time() and not force:
            sys.stderr.write(f"{DEV} 的租约是 {cur['owner']} 的，不是你的（{owner()}）；确要清掉加 --force\n"); sys.exit(75)
        if os.path.exists(p): os.remove(p)
        print(f"{DEV} 已释放"); return
    if not cur or cur["until"] <= time.time():
        print(f"{DEV} 空闲"); return
    print(f"{DEV} 归 {cur['owner']}（{cur.get('note') or '无说明'}），已用 {int(time.time() - cur['since'])} 秒，"
          f"最长还剩 {int(cur['until'] - time.time())} 秒")


def devs_cmd(devs):
    for k, d in devs["devices"].items():
        mark = "*" if k == devs.get("default") else " "
        state = "未接好" if d.get("enabled") is False else f"{d.get('host') or '本机'}:{d['port']}"
        print(f"{mark} {k:<6} {d.get('kind', ''):<6} {state:<16} {d.get('name', '')}")


def forward(host, argv):
    import shlex
    env = " ".join(f"{k}={shlex.quote(os.environ[k])}" for k in ("KP_DEBUG", "KP_SETTLE") if os.environ.get(k))
    env += f" KP_OWNER={shlex.quote(owner())} KP_LOCAL=1"  # 身份带过去；KP_LOCAL 防两边设备表不一致时来回转
    cmd = f"export PATH=$HOME/.local/bin:/opt/homebrew/bin:$PATH {env}; kp " + " ".join(shlex.quote(a) for a in argv)
    if argv and argv[0] == "shot":
        # 截图在远端，拉回本机再给路径，方便直接看图
        p = subprocess.run(["ssh", host, cmd], capture_output=True, text=True)
        rp = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""
        if p.returncode or not rp:
            sys.stderr.write(p.stderr); sys.exit(p.returncode or 1)
        os.makedirs("/tmp/kvmphone", exist_ok=True)
        lp = f"/tmp/kvmphone/remote_{os.path.basename(rp)}"
        subprocess.run(["scp", "-q", f"{host}:{rp}", lp], check=True)
        print(lp); return
    sys.exit(subprocess.run(["ssh", host, cmd]).returncode)


def ime_en(tries=3):
    """确保输入法在英文（输入框已聚焦时用）。敲一个 a：候选栏冒出「啊 阿…」这种短汉字就是拼音，删掉、点地球键切换再验。
    11-03：空格键上的「拼音」OCR 常读不到，zh_type 里按键盘文字判的办法会把拼音误判成英文，
    结果 URL 里的「=」被拼音输入法当成候选翻页键吞掉，所以这里改用敲字探测。"""
    key = lambda k, mods=(): call("/act", {"ops": [{"op": "key", "key": k, "mods": list(mods)}]})
    for _ in range(tries):
        before = {(it["t"], it["y"] // 20) for it in call("/read")["items"]}
        call("/act", {"ops": [{"op": "type", "text": "a"}]})
        its = call("/read?settle=150")["items"]
        # 两下：拼音下第一下清掉拼音串；英文下 Safari 会自动补全，第一下只删掉补全的尾巴，第二下才删掉 a
        call("/act", {"ops": [{"op": "key", "key": "Backspace", "mods": []}] * 2})
        # 候选栏在「123」键上方约 200–330 像素（11-03 实测 123 在 y≈958、候选在 y≈707、字母第一排 y≈762）；
        # 不能拿单个字母定位第一排：候选栏左边会显示拼音串本身（就是那个 a）。看不到屏上键盘时退回屏幕下半部
        k123 = next((it for it in its if it["t"].strip() == "123" and it["y"] > 600), None)
        lo, hi = (k123["y"] - 330, k123["y"] - 200) if k123 else (600, 2000)
        cands = [it for it in its if HAN.fullmatch(it["t"].strip()) and len(it["t"].strip()) <= 4
                 and lo <= it["y"] <= hi and (it["t"], it["y"] // 20) not in before]
        if os.environ.get("KP_DEBUG"):
            print(f"[ime_en] 123={k123 and (k123['x'], k123['y'])} 区间={lo}-{hi} 候选={[c['t'] for c in cands]}", file=sys.stderr)
        if len(cands) < 2:
            with open(f"/tmp/kvmphone/{DEV}.ime", "w") as f:
                f.write("en")
            return True
        if k123:  # 地球键在 123 键正下方偏左（同 zh_type 的 toggle）
            call("/act", {"ops": [{"op": "tap", "x": max(20, k123["x"] + k123["w"] // 2 - 14), "y": k123["y"] + k123["h"] // 2 + 73}]})
        else:
            key("Space", ["ControlLeft"])
        time.sleep(0.7)
    return False


def open_url(args, quiet=False, grep=None):
    """kp open <App> [关键词]：在 Safari 地址栏敲 App 的 URL scheme，直达 App 首页或搜索结果页（11-03）。
    关键词按 URL 编码敲成 ASCII，不走拼音输入法；原来点图标→点搜索框→打中文→点搜索要 5–10 步，这里一步。"""
    import urllib.parse
    if not args:
        sys.exit("用法：kp open <App> [关键词]，例：kp open 小红书 双妹；kp open 淘宝")
    tab = {k: v for k, v in json.load(open(os.path.join(DIR, "schemes.json"))).items() if not k.startswith("_")}
    name = args[0]
    ent = tab.get(name) or next((v for k, v in tab.items() if name in v.get("alias", []) or name.lower() in v.get("alias", [])), None)
    if not ent:
        sys.exit(f"kp open：没有「{name}」的直达链接，照常点主屏图标；有直达的：{'、'.join(tab)}")
    q = " ".join(args[1:]).strip()
    if q and not ent.get("search"):
        sys.exit(f"kp open：{name} 没有搜索直达，先 kp open {name} 再在 App 里搜")
    url = ent["search"].replace("{q}", urllib.parse.quote(q, safe="")) if q else ent["home"]
    url = url.replace('"', "%22").replace("{", "%7B").replace("}", "%7D")  # 京东那条带 JSON
    kpx.before(lambda: call("/read?cache=1")["items"], None)
    key = lambda k, mods=(): call("/act", {"ops": [{"op": "key", "key": k, "mods": list(mods)}]})
    # 回主屏 → 点 Dock 里的 Safari → ⌘L 选中地址栏 → 敲网址回车 → 点「打开」
    # （11-03 试过 Spotlight 打 safari：搜索框常落在拼音输入法里，回车会打开别的 App，所以改点图标；Dock 位置写在 schemes.json 的 _safari）
    sx, sy = json.load(open(os.path.join(DIR, "schemes.json"))).get("_safari", [193, 997])
    # ⌘H 后主屏有动画，太快点 Safari 会被吞，后面的按键就落在主屏上（敲字会弹 Spotlight、回车乱开 App）：
    # 点完 ⌘L 确认屏上键盘出来了（地址栏有焦点）才往下走，没出来就再来一次
    for i in range(3):
        key("KeyH", ["MetaLeft"]); time.sleep(1.2)
        call("/act", {"ops": [{"op": "tap", "x": sx, "y": sy}]}); time.sleep(1.5)
        key("KeyL", ["MetaLeft"]); time.sleep(0.6)
        if any(it["t"].strip() == "123" and it["y"] > 600 for it in call("/read")["items"]):
            break
    else:
        r = settle()
        kpx.after(r["items"], True, None)
        print("[open] 打不开 Safari 地址栏（点了三次 Dock 里的 Safari），照常点图标进")
        screen_hint(r["items"]); show(r["items"], grep); return
    # ⌘L 只聚焦：上回没提交的残字不会被选中，接着敲会拼到后面（11-03 实测），先 ⌘A 退格清空
    key("KeyA", ["MetaLeft"]); key("Backspace"); time.sleep(0.2)
    if not ime_en():
        print("[open] 输入法切不到英文，网址里的「=」可能被吞，照常点图标进更稳")
    call("/act", {"ops": [{"op": "type", "text": url}]}); key("Enter")
    btn = None
    for _ in range(12):  # 「在“小红书”中打开？」弹窗一般 0.5–2 秒出来
        time.sleep(0.35)
        its = call("/read")["items"]
        # OCR 常把弹窗两个按钮读成一条「Cancel Open」/「取消 打开」，按词在整条里的位置算横坐标
        for it in its:
            t = it["t"].strip()
            tok = next((w for w in ("打开", "Open", "開啟") if t == w or t.endswith(" " + w) or t.endswith(w) and len(t) <= 8), None)
            if tok:
                i = t.rfind(tok)
                btn = {**it, "cx": it["x"] + round(it["w"] * (i + len(tok) / 2) / len(t))}
                break
        if btn:
            break
    if not btn:
        r = settle()
        kpx.after(r["items"], True, None)
        print(f"[open] 没等到「打开」弹窗：可能没装 {name}、链接已失效，或 Safari 直接跳了。网址 {url}")
        screen_hint(r["items"]); show(r["items"], grep); return
    call("/act", {"ops": [{"op": "tap", "x": btn["cx"], "y": btn["y"] + btn["h"] // 2}]})
    time.sleep(1.2)
    r = settle(0.3)
    # 京东等 App 一启动就读剪贴板，弹系统的「允许粘贴」框挡住后面所有点击（11-03 实测）；按隐私从严点「不允许」
    deny = next((it for it in r["items"] if it["t"].strip() in ("Don't Allow Paste", "不允许粘贴")), None)
    if deny:
        call("/act", {"ops": [{"op": "tap", "x": deny["x"] + deny["w"] // 2, "y": deny["y"] + deny["h"] // 2}]})
        time.sleep(0.6); r = settle(0.3)
    kpx.after(r["items"], True, None)
    print(f"[open] 已经 {name} 直达{'「' + q + '」搜索结果' if q else '首页'}；不对就照常点图标进")
    if quiet:
        return
    screen_hint(r["items"]); show(r["items"], grep)


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__); return
    global BASE, DEV, DEVCFG, LOG
    dev = os.environ.get("KP_DEV")
    if "--dev" in argv:
        i = argv.index("--dev"); dev = argv[i + 1]; del argv[i:i + 2]
    if "KP_BASE" not in os.environ:
        devs = load_devs()
        if argv[0] == "devs":
            return devs_cmd(devs)
        dev = dev or devs.get("default", "rmq1")
        d = devs["devices"].get(dev)
        if not d:
            sys.exit(f"设备表里没有 {dev}（{DEVF}）；kp devs 看有哪些")
        if d.get("enabled") is False:
            sys.exit(f"{dev}（{d.get('name', '')}）还没接好")
        if not is_local(d.get("host"), devs):
            return forward(d["host"], ["--dev", dev] + argv)
        DEV, DEVCFG, BASE = dev, d, f"http://127.0.0.1:{d['port']}"
        os.environ.setdefault("KP_DEVNAME", d.get("name", ""))  # 接管卡标题用
        if dev != "rmq1":
            LOG = f"/tmp/kvmphone/bridge-{dev}.log"
    else:
        DEV = dev or "port" + BASE.rsplit(":", 1)[-1]  # 直连桥时节奏状态按端口分开
    quiet = "-q" in argv
    argv = [a for a in argv if a != "-q"]
    grep = None
    if "-g" in argv:
        i = argv.index("-g"); grep = argv[i + 1]; del argv[i:i + 2]
    after = 900
    if "-w" in argv:
        i = argv.index("-w"); after = int(argv[i + 1]); del argv[i:i + 2]
    cmd, args = argv[0], argv[1:]
    klog(cmd=cmd, args=args)
    kpx.init(DEV, owner())
    kpx.steer(cmd in ACTS)

    if cmd == "apps":
        return kpx.apps_cmd()
    if cmd == "learn":
        n = kpx.learn(" ".join(args))
        print(f"已记进 {n} 的说明" if n else "没记：要写成「App 名：事实」，App 名要在 kp apps 里"); return
    if cmd == "start":
        return start()
    if cmd == "stop":
        try: call("/quit", timeout=3, auto=False)
        except Exception: pass
        time.sleep(0.5)
        if DEVCFG.get("kind", "glkvm") == "glkvm":
            # 老版本桥 /quit 后进程不退，顺手把本目录残留的桥进程都清掉
            subprocess.run(["pkill", "-f", f"file://{DIR}/bridge.mjs"], capture_output=True)
        print("bridge 已停"); return
    if cmd == "health":
        print(call("/health")); return
    if cmd in ("who", "lock", "unlock"):
        if "KP_BASE" in os.environ: sys.exit("设了 KP_BASE 时没有设备租约")
        return lease_cmd(cmd, args)
    if "KP_BASE" not in os.environ:
        lease(cmd in ACTS)
    if not alive():
        start()

    if cmd == "read":
        read(grep); return
    if cmd == "settle":
        r = settle(0.1)
        print(len(r["items"]), "条文字，已稳定"); return
    if cmd == "shot":
        img = call("/frame")["img"]
        if os.environ.get("KP_LOG"):  # /tmp/kvmphone 只留最近 50 张、会被覆盖：任务里的截图另存一份进任务目录当证据
            import shutil
            d = os.path.join(os.path.dirname(os.environ["KP_LOG"]), "shots"); os.makedirs(d, exist_ok=True)
            img = shutil.copy(img, os.path.join(d, time.strftime("%H%M%S") + f"-{os.getpid() % 1000}.jpg"))
        klog(shot=img); print(img); return
    if cmd == "tap":
        if len(args) >= 2 and args[0].lstrip("-").isdigit() and args[1].lstrip("-").isdigit():
            import random
            x = int(args[0]) + round(max(-3, min(3, random.gauss(0, 1.5))))
            y = int(args[1]) + round(max(-3, min(3, random.gauss(0, 1.5))))
            # 不用缓存、等画面稳住再判：11-01 那笔误付就是连着三下坐标点击，第三下落在刚弹出来的「立即支付」上，护栏还拿着上一屏在判
            target = guard(call("/read?settle=200")["items"], x, y, coord=True)
        else:
            nth = 0; exact = "-e" in args
            args = [a for a in args if a != "-e"]
            if "-n" in args:
                i = args.index("-n"); nth = int(args[i + 1]); del args[i:i + 2]
            t0 = time.time()
            r = call("/read?settle=200")  # 点之前按当前稳定的屏找字（不用缓存，见上面坐标点击的说明）
            dbg(f"找字读屏 cached={r.get('cached')}", t0)
            if forbidden(args[0], r["items"]):
                deny(args[0])
            it = find(r["items"], args[0], nth, exact)
            if not it:
                print(f"没找到「{args[0]}」，当前屏：")
                show(r["items"])
                sys.exit(2)
            # 人点按钮不会每次落在同一像素：在文字框中间一块里随机偏一点
            import random
            x = it["x"] + it["w"] // 2 + round(max(-it["w"] / 4, min(it["w"] / 4, random.gauss(0, 3))))
            y = it["y"] + it["h"] // 2 + round(max(-it["h"] / 4, min(it["h"] / 4, random.gauss(0, 2))))
            guard(r["items"], x, y)
            target = it["t"]
            print(f"点 {x},{y}「{kpx.redact(it['t'])}」")
        return act([{"op": "tap", "x": x, "y": y}], after, quiet, grep, target)
    if cmd in ("type", "paste") and DEVCFG.get("kind") == "wda":  # WDA 能直接打任意文字，不走剪贴板
        return act([{"op": "type", "text": " ".join(args)}], after, quiet, grep)
    if cmd == "open":
        if DEVCFG.get("kind") == "wda":
            return act([{"op": "launch", "app": " ".join(args)}], after, quiet, grep)
        return open_url(args, quiet, grep)
    if cmd in ("type", "paste"):  # 英文也走这里：先确认输入法在英文，不然字母会落进拼音候选（11-01 狂打字母）
        # 11-01 起不再走通用剪贴板：mini 没登录用户的 Apple ID，⌘V 粘出来的是用户 MacBook 剪贴板里的东西
        return zh_type(" ".join(args), quiet, grep)
    if cmd == "key":
        ops = []
        for k in args:
            parts = k.split("+")
            mods = [{"Meta": "MetaLeft", "Ctrl": "ControlLeft", "Shift": "ShiftLeft", "Alt": "AltLeft"}.get(p, p) for p in parts[:-1]]
            ops.append({"op": "key", "key": parts[-1], "mods": mods})
        return act(ops, after, quiet, grep)
    if cmd in ("up", "down"):
        import random
        n = int(args[0]) if args else 1
        W, U = 500, random.uniform
        ops = []
        for _ in range(n):
            # 每次划的位置、长度、斜度、快慢都不一样；拖到位停一下再松手（不甩），画面松手就停，
            # 一次约翻 60% 屏，不用等惯性滚完
            x1 = W * U(0.62, 0.86); y1 = U(880, 930); L = U(600, 680)
            x2 = x1 + U(-22, 12); y2 = y1 - L
            if cmd == "down":
                y1, y2 = 1150 - y1, 1150 - y2
            ms = U(560, 660)  # 快于约 0.5 s 会触发惯性滚动，要等 2–3 s 才停
            ops += [{"op": "drag", "x1": round(x1), "y1": round(y1), "x2": round(x2), "y2": round(y2), "ms": round(ms), "steps": round(ms / 12),
                     "stop": round(U(60, 100))},
                    {"op": "wait", "ms": round(U(80, 160))}]
        return act(ops, after, quiet, grep)
    if cmd == "back":
        import random
        y = random.uniform(470, 620)
        return act([{"op": "drag", "x1": 3, "y1": round(y), "x2": round(random.uniform(350, 410)), "y2": round(y + random.uniform(-25, 25)),
                     "ms": round(random.uniform(220, 280)), "steps": 18}], after, quiet, grep)
    if cmd == "price":
        print(price_of(read(show_=False)["items"])); return
    if cmd == "look":
        # 读屏 + JEV 判屏，一步拿到「这是什么屏、下一步干嘛、屏上有什么」
        r = read(grep)
        kpx.after(r["items"], acted=False)
        screen_hint(r["items"]); return
    if cmd == "app":
        return kpx.app_cmd(args, call("/read?cache=1")["items"])
    if cmd == "handoff":
        wait = 900
        if "--wait" in args:
            i = args.index("--wait"); wait = int(args[i + 1]); del args[i:i + 2]
        sys.exit(kpx.handoff(" ".join(args) or "需要你在手机上处理一下", lambda: call("/frame")["img"],
                             lambda ttl: lease(True, ttl), wait))
    if cmd == "judge":
        r = read(show_=False)
        j = jev(args[0], r["items"])
        print(json.dumps({"answers": j[0], "derived": j[1], "gate": j[2], "ms": j[3]}, ensure_ascii=False) if j else "JEV 不可用"); return
    print(__doc__); sys.exit(64)


def price_of(items):
        # 解析结算页 / 店铺购物车栏，输出结构化价格（只读）
        texts = [it["t"] for it in items]
        joined = "\n".join(texts)
        money = lambda pat: (m.group(1) if (m := re.search(pat, joined)) else None)

        def prev_money(label):
            # 结算页里金额常在标签上一行；「¥6¥0.5」这种划线价取最后一个
            for i, t in enumerate(texts):
                if t.startswith(label):
                    for j in (i - 1, i + 1):
                        if 0 <= j < len(texts) and (ms := re.findall(r"[¥￥]\s*([\d.]+)", texts[j])) and not re.search(r"[\u4e00-\u9fff]", texts[j]):
                            return ms[-1]
            return None
        out = {
            "店铺": next((t.replace("闪购", "").strip() for t in texts if re.search(r"（.*店）", t)), None),
            "打包费": prev_money("打包费"),
            "配送费": prev_money("配送费"),
            "已优惠": money(r"已优惠\s*[¥￥]\s*([\d.]+)"),
            "实付": money(r"立即支付\s*[¥￥]\s*([\d.]+)"),
            "未选红包": money(r"最高\s*([\d.]+)\s*元可用"),
            "起送差额": money(r"差\s*[¥￥]\s*([\d.]+)\s*起送"),
            "购物车": money(r"(?m)^1?\s*[¥￥]([\d.]+)\s*(?:[券劵]后|优惠明细)"),
            "券后约": money(r"[券劵]后约\s*[¥￥]\s*([\d.]+)"),
        }
        return json.dumps({k: v for k, v in out.items() if v}, ensure_ascii=False)


if __name__ == "__main__":
    main(sys.argv[1:])
