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
  kp tap 文字 [-n 第几个] [-e] [-f]  按文字点（-e 精确匹配，-f 放行闲鱼我想要 / 关注这类社交按钮）；或 kp tap X Y
  kp type pinyin                   打字（ASCII，经 iOS 输入法；带中文会自动改走 paste）
  kp paste 中文                    打中文：经 iPhone 拼音输入法敲拼音、点候选词，打完切回英文（WDA 后端直接打字）
  kp paste --fresh 中文            搜索框专用：先清空，打完核对框里的字，不对清空重打（最多 3 次，仍不对退出码 3）
  kp open 小红书 双妹              直达 App 的搜索结果页（KVM 后端经 Safari 敲 URL scheme，表在 schemes.json；不给词就直达首页）
  kp key Enter|Backspace|Space ... 按键，可写 Meta+KeyA 这类组合
  kp up [N] / kp down [N]          上滑 / 下滑 N 次（看下面 / 看上面的内容）
  kp back                          从左边缘右滑返回
  kp drag X1 Y1 X2 Y2 [毫秒]       任意拖动（如从右上角下拉打开控制中心、拖音量条）
  kp scan [--pages N≤6] [--grep 正则] [--stop-when 文字]   列表扫描：自动上滑读几屏，只输出新增的行，价格行另存 candidates.jsonl
  kp scroll-until 文字 [--max N≤8]  上滑直到目标文字出现或到底
  kp date 开始 [结束] [--no-ok]     日历选日期：翻月、点日期格、回读「取（当地）」那行核对，对了点「确定」（日期写 11-06 或 2026-11-06）
  kp place 地名                    地点搜索：点搜索框、打地名、等候选、点最匹配的一条、回读核对（待真机验证，失败会退回手动）
  动作后和 kp look 默认只显示相对上一屏变化的行；要全量加 --full（KP_DIFF=0 整体关掉）
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
import contextlib, io, json, os, re, subprocess, sys, time, urllib.error, urllib.request, unicodedata

BASE = os.environ.get("KP_BASE", "http://127.0.0.1:7690")  # 设了 KP_BASE 就直连那个桥，不查设备表、不转发、不拿租约
DIR = os.path.dirname(os.path.realpath(__file__))
DEVF = os.environ.get("KP_DEVICES") or os.path.join(DIR, "devices.json")
DEV, DEVCFG = "rmq1", {}  # main() 按 --dev / KP_DEV / 设备表 default 定下来；DEVCFG 为空 = 不自动起桥
LOG = "/tmp/kvmphone/bridge.log"
ACTS = {"tap", "type", "paste", "key", "up", "down", "back", "drag", "open", "handoff", "scan", "scroll-until", "date", "place"}
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


LOG_SEP = "===== kp start"   # 每次起桥在桥日志里写的分隔行开头；rmq 判原因只看最后一个分隔之后的部分
LOG_MAX = 2 * 1024 * 1024    # 桥日志超过 2 MB 就挪成 .1（只留一份旧的），再从新文件接着写


def open_log():
    """11-12 起桥日志追加、不再清空：11-12 15:32 那次失败的现场被重启清掉了，查不到原因。"""
    os.makedirs("/tmp/kvmphone", exist_ok=True)
    try:
        if os.path.getsize(LOG) > LOG_MAX:
            os.replace(LOG, LOG + ".1")   # 改名不影响还开着旧文件的进程
    except OSError:
        pass
    log = open(LOG, "a")
    log.write(f"\n{LOG_SEP} {time.strftime('%Y-%m-%d %H:%M:%S')} {owner()} pid={os.getpid()} =====\n"); log.flush()
    return log


def start():
    if alive():
        print("bridge 已在运行"); return
    try: call("/quit", timeout=3, auto=False); time.sleep(1)  # 端口上还挂着失效的旧桥（空间被关等）就先让它退
    except Exception: pass
    log = open_log()
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


def read(grep=None, show_=True, remember=True):
    t0 = time.time()
    r = call("/read")
    # K0：读屏耗时分段（只加记账字段）。dur_ocr/dur_frame 由桥回报，旧桥没有就缺省
    klog(read_ms=round((time.time() - t0) * 1000), dur_ocr=r.get("ocr_ms"), dur_frame=r.get("frame_ms"), cached=r.get("cached"))
    if show_:
        show(r["items"], grep)
    k1_save(last_texts=k2_texts(r["items"]))  # K2：下一条 key 等没有点前读屏的命令，拿它当「点之前那一屏」
    if remember and not grep:
        diff_remember(r["items"])  # K3：kp read 是全量输出，下一次动作的差分以它为底
    elif remember and grep:
        k1_save(diff_ts=0)  # 带 -g 只印了匹配的行，模型没见过整屏：下一次动作必须给全量
    return r


# K3：动作后 / kp look 的默认输出只打印相对上一屏新增或变化的行。底子是「上一次完整给模型看过的那一屏」，
# 只存行的短哈希（文字 + 纵向位置分桶），不存明文。位置也进哈希：滑动后同一行换了位置，坐标变了，必须重新打印。
DIFF_TTL = 90   # 秒：上一屏超过这么久，模型多半已经忘了它长什么样（或换了会话），一律给全量
DIFF_MIN_SAME = 3  # 未变的行少于这个数，说明基本换了一屏，直接给全量
STATUS_Y = 60   # y 小于它的是状态栏（时间 / 电量 / 信号）
FULL_OUT = False  # kp look --full：本条命令保留全量


def diff_key(it):
    return k2_hash(norm(it["t"]) + "@" + str(it["y"] // 16))


def diff_remember(items, ts=None):
    k1_save(diff_keys=[diff_key(it) for it in items][:400], diff_ts=round(time.time() if ts is None else ts))


def show_screen(items, grep=None, full=False):
    """默认差分输出。klog 仍记全量（复核要靠它）。全量的情形：KP_DIFF=0、--full、带 -g、没有可比的上一屏、
    上一屏太久以前、换了整屏、当前是结算 / 支付页（付款相关的屏永远给全量）。"""
    st = k1_load()
    prev, ts = st.get("diff_keys"), st.get("diff_ts") or 0
    use = (not full and not grep and os.environ.get("KP_DIFF", "1") != "0" and prev
           and time.time() - ts <= DIFF_TTL and not on_checkout(items)
           and st.get("jev_screen") not in ("checkout", "payment") and not st.get("checkout"))
    if use:
        ps = set(prev)
        body = [it for it in items if it["y"] >= STATUS_Y]
        same = [it for it in body if diff_key(it) in ps]
        use = len(same) >= DIFF_MIN_SAME
    if not use:
        show(items, grep)
        if grep:
            k1_save(diff_ts=0)  # 只印了匹配的行，不能当差分底子，下一次给全量
        else:
            diff_remember(items)
        return
    klog(screen=" | ".join(it["t"] for it in items)[:4000])
    new = [it for it in body if diff_key(it) not in ps]
    print("[差分] 只显示相对上一屏新增 / 变化的行；要全量用 kp look --full")
    for it in new:
        print(f'{it["x"] + it["w"] // 2},{it["y"] + it["h"] // 2}\t{kpx.redact(it["t"])}')
    if not new:
        print("（没有新增或变化的行）")
    print(f"[同屏 {len(same)} 项未变，状态栏已省；未变的行位置没动，坐标沿用上一屏的输出]")
    diff_remember(items)


LAST_SETTLE = {}  # K0：最近一次 settle 的结果（桥是否判稳、是否退回文字比对、桥回报的 OCR / 截帧耗时）


def settle(min_wait=0.15, timeout=6.0):
    """动作后等画面稳定。先由桥在页面里比缩略图（每 40 ms 一次，0.25 s 不动就算稳），只 OCR 一次；
    画面一直在动（轮播、视频）或字太少（还在加载）才退回老办法：连续两次 OCR 文本一致。"""
    t0 = time.time()
    r = call(f"/read?settle={int(min_wait * 1000)}")
    LAST_SETTLE.update(stable=bool(r.get("stable")), fallback=False, ocr_ms=r.get("ocr_ms"), frame_ms=r.get("frame_ms"), bridge_settle_ms=r.get("settle_ms"))
    if r.get("stable") and len(r["items"]) >= 3:
        return r
    sig = lambda r: {it["t"] for it in r["items"][2:]}  # 去掉状态栏时间 / 电量
    last = sig(r)
    LAST_SETTLE.update(fallback=True, stable=False)  # K0：退回文字比对后，是否稳定以文字比对的结果为准（桥的判稳另存在 bridge_settle_ms 旁）
    while time.time() - t0 < timeout:
        time.sleep(0.2)
        r = call("/read")
        LAST_SETTLE.update(ocr_ms=r.get("ocr_ms"), frame_ms=r.get("frame_ms"))
        # OCR 有抖动、页面有轮播，按文字集合重合度 ≥ 85% 判稳定，不要求逐字相同
        s = sig(r)
        if len(s & last) >= 0.85 * max(len(s | last), 1) and len(r["items"]) >= 3:
            LAST_SETTLE["stable"] = True
            return r
        last = s
    return r


# 本机的 JEV 先试；mini 上的连不上上游（typesafe 在国内不通，backend=none），再试 Mac 的 rmq relay 反向映射过来的 7681（11-01）
JEV_URLS = [os.environ["JEVJUDGE_URL"]] if os.environ.get("JEVJUDGE_URL") else ["http://127.0.0.1:7677", "http://127.0.0.1:7681"]


HAN = re.compile(r"[\u3400-\u9fff]+")


def _find_sub(items, word):
    """候选里找整词 word（可能被 OCR 和别的候选粘在一起）。优先单独成条的，再找粘连串里的；返回要点的 (x, y)。"""
    if not word:
        return None
    for exact in (True, False):
        for it in items:
            t = it["t"]
            i = t.find(word)
            if i < 0 or (exact and t != word):
                continue
            L = max(1, len(t))
            return round(it["x"] + it["w"] * (i + len(word) / 2) / L), it["y"] + it["h"] // 2
    return None


def field_has(text):
    """输入后核对：屏幕上方（y<220，搜索框所在）有一行去掉空格后等于 / 以 text 结尾。"""
    want = re.sub(r"\s+", "", text)
    for it in call("/read?settle=150")["items"]:
        if it["y"] < 220 and re.sub(r"\s+", "", it["t"]).endswith(want):
            return True
    return False


def zh_type_fresh(text, quiet=False, grep=None, tries=3):
    """kp paste --fresh：搜索框专用。清空 → 输入 → 核对框里的字，不对就清空重来，最多 3 次（11-18）。"""
    for n in range(1, tries + 1):
        call("/act", {"ops": [{"op": "key", "key": "KeyA", "mods": ["MetaLeft"]}, {"op": "key", "key": "Backspace", "mods": []}]})
        time.sleep(0.4)
        zh_type(text, quiet=True)
        if field_has(text):
            if n > 1:
                print(f"[输入] 第 {n} 次打对了")
            r = settle()
            if not quiet:
                screen_hint(r["items"]); show(r["items"], grep)
            return
        print(f"[输入] 第 {n} 次框里不是「{text}」，清空重打")
    print(f"[输入] {tries} 次都没打对「{text}」，先读屏看框里是什么")
    sys.exit(3)


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
        if any(t.startswith("English") or t.lower() == "space" for t in ts): return "en", k123
        if not k123: return None, None
        # 11-05：空格键上的「拼」「English (US)」深色底小字 OCR 都常读不到，以前只要看到 123 就判英文，
        # 拼音里打英文网址全变乱码（微信发 Tims 链接实测）。读不到标签就试探：打一个 a，候选栏冒出汉字 = 拼音，再删掉
        before = {it["t"] for it in call("/read?cache=1")["items"]}
        call("/act", {"ops": [{"op": "type", "text": "a"}]})
        its = call("/read?settle=150")["items"]
        time.sleep(0.25); key("Backspace"); time.sleep(0.35)  # 删得太快 iOS 会吞（11-05 试探留下一个 a）
        # 只看候选栏（123 键上方 200–330 像素）里的纯汉字词；闲鱼这类 App 敲字母会冒出「Q 笔记本…」联想行，以前被误当候选（11-18）
        lo, hi = k123["y"] - 330, k123["y"] - 200
        han = any(lo <= it["y"] <= hi and HAN.fullmatch(it["t"].replace(" ", "")) and it["t"] not in before for it in its)
        return ("zh" if han else "en"), k123
    def toggle(k123=None):
        # 11-18 实测：KVM 是外接硬件键盘，硬件键盘有自己的输入语言。点屏上地球键只切软键盘，HID 一敲字母就弹回英文，
        # 拼音敲不出来（闲鱼框里留下 bijiben）。必须用 Ctrl+Space 切硬件键盘语言；偶尔吞键，ensure() 会读屏核对后重试
        key("Space", ["ControlLeft"]); time.sleep(0.9)
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
        for ch in py:  # 拼音逐个字母敲、每个之间停一下：整串一次敲 HID 偶尔吞键（11-18 zhengjiuzhe 少了 g 变「真就这」）
            call("/act", {"ops": [{"op": "type", "text": ch}]}); time.sleep(0.04)
        geo = {}
        def new():
            # 只认键盘上方候选栏里的字（「123」键上方 200–330 像素）。11-18 闲鱼搜索框：英文模式敲 bijiben，
            # App 自己的联想词「5090笔记本…」冒出来被当成候选，误判成拼音模式，结果框里留下 bijiben
            its = call("/read?settle=150")["items"]
            k123 = next((it for it in its if it["t"].strip() == "123" and it["y"] > 600), None)
            # 候选栏中心实测在 123 键上方约 251 像素（11-03 958→707、11-18 949→698）；放宽到 ±30。
            # 以前取 200–330 太宽，闲鱼联想行（123 上方约 288）混进来，「Q 台式钥匙」被当候选点了
            lo, hi = (k123["y"] - 282, k123["y"] - 220) if k123 else (680, 730)
            geo.update(k123=k123, bar=(lo + hi) // 2)
            out = []
            for it in its:
                t = it["t"]
                if t in before or not (lo <= it["y"] <= hi) or not HAN.fullmatch(t.replace(" ", "")):
                    continue
                # OCR 常把一排候选连成一条「态势 台式 泰式」（11-18 台式、拯救者都栽在这），按空格拆开、按字位估每个词的横坐标
                L = max(1, len(t))
                for m in re.finditer(r"\S+", t):
                    out.append(dict(it, t=m.group(), x=round(it["x"] + it["w"] * m.start() / L), w=max(8, round(it["w"] * len(m.group()) / L))))
            return out
        cands = new()
        if os.environ.get("KP_DEBUG"):
            print(f"[zh_type] seg={seg} st={st} 首次候选={[c['t'] for c in cands]}", file=sys.stderr)
        for _try in range(3):  # 敲出来的是字母不是候选：输入法其实在英文模式，删掉、Ctrl+Space（偶尔吞键）、重敲，最多 3 次
            if cands: break
            time.sleep(0.4); cands = new()  # 候选栏有时慢半拍
            if cands: break
            call("/act", {"ops": [{"op": "key", "key": "Backspace", "mods": []}] * (len(py) + 1)})
            time.sleep(0.3); toggle()
            for ch in py:
                call("/act", {"ops": [{"op": "type", "text": ch}]}); time.sleep(0.04)
            cands = new()
        if not cands:  # 三次都没切到拼音：删掉拼音串，别按空格（英文模式下空格会把 xianka 自动改成 Xianxia.）
            call("/act", {"ops": [{"op": "key", "key": "Backspace", "mods": []}] * (len(py) + 1)})
            misses.append(seg); st = "en"; continue
            if os.environ.get("KP_DEBUG"):
                print(f"[zh_type] 切换后候选={[c['t'] for c in cands]} 屏={[ (i['t'],i['y']) for i in call('/read?cache=1')['items'] if i['y']>600][:25]}", file=sys.stderr)
        st = "zh"
        left = seg
        sub = _find_sub(cands, left)
        if sub:  # OCR 把几个候选粘成一条（「态势台式」），目标整词在里面：点它那几个字的中间（11-18）
            call("/act", {"ops": [{"op": "tap", "x": sub[0], "y": sub[1]}]}); left = ""
        for _ in range(6 if left else 0):
            best, bk = None, 0
            for it in cands:
                k = 0
                while k < min(len(left), len(it["t"])) and left[k] == it["t"][k]: k += 1
                if k > bk or (k == bk and best and len(it["t"]) < len(best["t"])): best, bk = it, k
            if not best: break
            if left == seg and bk < len(left) and bk < len(best["t"]) and geo.get("k123"):
                break  # 只沾一部分（「主板」碰上「主办」只对 1 个字）：先去展开的候选里找整词，别点半截（11-18）
            n = len(best["t"])  # OCR 把几个候选连成一条时，按字数比例点前 bk 个字的中间
            x = best["x"] + (best["w"] // 2 if bk == n else round(best["w"] * bk / 2 / n))
            call("/act", {"ops": [{"op": "tap", "x": x, "y": best["y"] + best["h"] // 2}]})
            left = left[bk:]
            if not left: break
            time.sleep(0.3); cands = new()
        if left and left == seg and geo.get("k123"):
            # 目标词不在第一排（候选栏只放得下 5 个左右，11-18「主板」「拯救者」都排在后面）：点候选栏右端的 ∨ 展开全部候选再找
            k = geo["k123"]
            call("/act", {"ops": [{"op": "tap", "x": 486, "y": geo["bar"]}]}); time.sleep(0.7)
            its = call("/read?settle=200")["items"]
            grid = []
            for it in its:
                t = it["t"]
                if not (geo["bar"] - 30 <= it["y"] <= k["y"] + 90) or not HAN.fullmatch(t.replace(" ", "")):
                    continue
                L = max(1, len(t))
                for m in re.finditer(r"\S+", t):
                    grid.append(dict(it, t=m.group(), x=round(it["x"] + it["w"] * m.start() / L), w=max(8, round(it["w"] * len(m.group()) / L))))
            hit = next((g for g in grid if g["t"] == seg), None)
            sub = None if hit else _find_sub(grid, seg)
            if sub:
                call("/act", {"ops": [{"op": "tap", "x": sub[0], "y": sub[1]}]}); time.sleep(0.4); left = ""
            if os.environ.get("KP_DEBUG"):
                print(f"[zh_type] 展开候选={[g['t'] for g in grid][:30]} 命中={hit and hit['t']}", file=sys.stderr)
            if sub:
                pass
            elif hit:
                n = len(hit["t"]); bk = n
                x = hit["x"] + (hit["w"] // 2 if bk == n else round(hit["w"] * bk / 2 / n))
                call("/act", {"ops": [{"op": "tap", "x": x, "y": hit["y"] + hit["h"] // 2}]}); time.sleep(0.4)
                left = ""
            else:
                call("/act", {"ops": [{"op": "tap", "x": 486, "y": geo["bar"]}]}); time.sleep(0.5)  # 收起
        if left:
            key("Space"); misses.append(seg)
    if st != "en": ensure("en")
    open(stf, "w").write(st)
    r = settle()
    if misses:
        print(f"[输入] 候选栏里没找到「{'」「'.join(misses)}」，已按空格上屏第一个候选，先核对输入框里的字")
    if not quiet:
        screen_hint(r["items"]); show(r["items"], grep)


LAST_JEV = {}  # K0：最近一次 jev() 实际答的是哪个地址、耗时多久


def jev(template, items):
    """把 OCR 文本交给 JEV 做闭集判断，返回 (主答案, 派生动作, gate)。JEV 不在线或超时返回 None。
    直连常驻服务的 HTTP 接口（mini 上 jevjudge 命令不在 PATH，之前一直是「不可用」），2 秒没回就跳过。"""
    state = "\n".join(it["t"] for it in items)[:4000]
    LAST_JEV.clear()
    for url in JEV_URLS:
        try:
            req = urllib.request.Request(url + "/judge", data=json.dumps({"template": template, "state": state}).encode(),
                                         headers={"content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=3.5) as r:  # 热的时候 0.6–1.5 s
                j = json.loads(r.read())
        except Exception:
            continue
        if j.get("answers"):  # 后端不通时服务回 backend=none、answers 为空，换下一个
            LAST_JEV["url"] = url
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


# K1：点前不重读同一屏。kp 每条命令是独立进程，「上一次读屏」的缓存放在桥里；这里只记两个只能 kp 知道的事实：
# 上一屏是不是结算 / 支付页（OCR 命中或 JEV 判的），有任何一条成立，下一次点击就一律完整重读，不用缓存。
K1_FRESH = 5  # 秒：上次读屏在这么久之内、画面严格没变、且当时是「等稳了」的，才沿用（桥那边上限 8）


def k1_path():
    d = os.path.dirname(os.environ["KP_LOG"]) if os.environ.get("KP_LOG") else "/tmp/kvmphone"
    return os.path.join(d, f"k1_state_{DEV}.json")


def k1_load():
    try:
        with open(k1_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def k1_save(**kw):
    try:
        st = k1_load(); st.update(kw)
        os.makedirs(os.path.dirname(k1_path()), exist_ok=True)
        with open(k1_path(), "w") as f:
            json.dump(st, f)
    except Exception:
        pass  # 状态写不进去只会让下次多读一屏，不影响护栏


def k1_must_full():
    """true = 这次点击必须完整重读：设了 KP_NOAUTO、上一屏 OCR 命中结算页、或 JEV 上一次判成 checkout / payment。"""
    if os.environ.get("KP_NOAUTO"):
        return True
    st = k1_load()
    return bool(st.get("checkout")) or st.get("jev_screen") in ("checkout", "payment")


def k1_read(text=None, x=None, y=None, nth=0, exact=False):
    """点击前读屏。能沿用桥里缓存就沿用（不到 K1_FRESH 秒、当时判稳、画面严格没变），否则完整 settle 重读。
    结算 / 付款类一律完整重读：on_checkout 命中、JEV 上次判 checkout / payment、目标文字或落点附近命中 forbidden()。
    返回 (r, expect)；expect 是要交给 /act 的屏指纹（没沿用缓存时为 None，行为和以前完全一样）。"""
    full = lambda: call("/read?settle=200")
    if k1_must_full() or (text is not None and forbidden(text, [])):
        r = full()
    else:
        r = call(f"/read?settle=200&fresh={K1_FRESH}")
        if r.get("fresh_hit") and r.get("thumb"):
            items = r["items"]
            risky = bool(on_checkout(items))
            if not risky and text is not None:
                it = find(items, text, nth, exact)
                risky = bool(it and forbidden(it["t"], items))
            if not risky and x is not None:
                risky = any(forbidden(it["t"], items) for it in items
                            if it["x"] - 30 <= x <= it["x"] + it["w"] + 30 and it["y"] - 25 <= y <= it["y"] + it["h"] + 25)
            if risky:
                r = full()
            else:
                print(f"[省读屏] 沿用 {round(r.get('age_ms', 0) / 1000, 1)} 秒前的读屏（画面没变），点之前不重读；护栏照常用这屏判")
                klog(k1_hit=True, k1_age_ms=r.get("age_ms"))
                return r, {"thumb": r["thumb"]}
    k1_save(checkout=bool(on_checkout(r["items"])))
    return r, None


# K2：无变化 / 循环提示。状态放在 K1 同一个状态文件里，只存文字的短哈希，不存屏幕原文。
K2_SIM = 0.97      # 前后文字集合相似度达到这个值，才当作画面没变（0.9 太松：20 项的屏变 1 项就误报）
K2_MIN_ITEMS = 5   # 文字太少的小屏不判无变化
K2_NOOP_MAX = 3    # 同一处连续这么多次无变化之后，下一次点击直接拒绝
K2_NEAR = 24       # 两次点击相距不超过这么多像素，算「同一处」
K2 = {"cmd": ""}   # 当前命令的指纹（main 里设置），循环检测用
_VOLATILE = re.compile(r"^\d{1,2}[:：]\d{2}$|^\d{1,3}%$")  # 状态栏时间 / 电量，会自己变，不算内容


def k2_hash(s):
    import hashlib
    return hashlib.md5(s.encode("utf-8", "ignore")).hexdigest()[:8]


def k2_texts(items):
    return sorted({k2_hash(norm(it["t"])) for it in items if it["t"].strip() and not _VOLATILE.match(it["t"].strip())})[:200]


def k2_sim(a, b):
    a, b = set(a or []), set(b or [])
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def k2_cmd(cmd, args):
    """命令指纹：坐标点击按 30 像素格子归一（落点本来就有随机偏移），其余用原参数。"""
    if cmd == "tap" and len(args) >= 2 and args[0].lstrip("-").isdigit() and args[1].lstrip("-").isdigit():
        return k2_hash(f"tap {int(args[0]) // 30} {int(args[1]) // 30}")
    return k2_hash(" ".join([cmd] + [a for a in args if a != "-f"]))


def k2_repeat_guard(items, x, y, coord, force=False):
    """同一处连续 3 次无变化、当前屏仍然和那时一样：第 4 次直接拒绝（退出码 3），-f 放行。付款护栏不受它影响，先于它执行。"""
    if os.environ.get("KP_NOAUTO") or force:
        return
    st = k1_load()
    nx = st.get("noop_xy")
    if not nx or nx[2] < K2_NOOP_MAX:
        return
    pay = bool(on_checkout(items)) or st.get("jev_screen") in ("checkout", "payment")
    if pay and x < 90 and y < 160:
        return  # 结算 / 支付页唯一允许的出口（左上角返回）不受重复点拒绝影响
    if abs(nx[0] - x) > K2_NEAR or abs(nx[1] - y) > K2_NEAR:
        return
    if k2_sim(k2_texts(items), st.get("noop_texts")) < K2_SIM:
        return
    advice = "换个文字点击（kp tap 文字）" if coord else "换个目标，或 kp handoff"
    if pay:
        advice = "这是结算 / 支付页，别再点别的文字：用 kp back（边缘右滑）退出，或 kp handoff"
    print(f"拒绝：同一处已连续 {nx[2]} 次点了没变化，再点也不会有用；{advice}；确需再点用 -f")
    klog(guard="重复无变化点击"); sys.exit(3)


def k2_xianyu_hint(items, x, y):
    """闲鱼搜索列表上点商品卡左侧头像 / 店名区，多半会进卖家页。只提示，不拒、不自动返回（任务可能真要进卖家页）。"""
    if os.environ.get("KP_NOAUTO"):
        return
    if x < 170 and y >= 160 and k1_load().get("jev_screen") == "search_list" and on_xianyu(items):
        print("[提示] 这一下落在商品卡左侧头像 / 店名区，会进卖家页；要看商品详情，改点商品标题")


def k2_after(items, stable, before_texts, ops, jev_prev, jev_now, track):
    """动作后判断「没起作用」和「在原地打转」。返回要打印的行；同时更新状态文件。"""
    st = k1_load()
    after_t = k2_texts(items)
    out = []
    xy = None
    if len(ops) == 1 and ops[0].get("op") == "tap":
        xy = (ops[0]["x"], ops[0]["y"])
    upd = {"last_texts": after_t}
    jev_same = jev_prev is None or jev_now is None or jev_prev == jev_now
    noop = bool(track and stable and before_texts is not None and len(before_texts) >= K2_MIN_ITEMS and len(after_t) >= K2_MIN_ITEMS
                and k2_sim(before_texts, after_t) >= K2_SIM and jev_same)
    if noop:
        n = int(st.get("noop_n", 0)) + 1
        upd["noop_n"] = n
        out.append(f"[无变化] 这一下没起作用（连续第 {n} 次）")
        if xy:
            pv = st.get("noop_xy")
            cnt = pv[2] + 1 if pv and abs(pv[0] - xy[0]) <= K2_NEAR and abs(pv[1] - xy[1]) <= K2_NEAR else 1
            upd["noop_xy"], upd["noop_texts"] = [xy[0], xy[1], cnt], after_t
            if cnt >= K2_NOOP_MAX:
                out.append(f"同一处已连续 {cnt} 次没变化，再点会被拒绝：换个文字点击或别的目标，或 kp handoff")
    elif track and (not stable or before_texts is None):
        pass  # 没法判断（还在加载、或没有点前的屏）：计数保持原样，不加也不清
    else:
        upd.update(noop_n=0, noop_xy=None, noop_texts=None)
    lp = st.get("loop") or {}
    same = lp.get("cmd") == K2["cmd"] and lp.get("jev") == jev_now and len(after_t) >= 3 and k2_sim(lp.get("texts"), after_t) >= K2_SIM
    n = int(lp.get("n", 0)) + 1 if same else 1
    upd["loop"] = {"cmd": K2["cmd"], "jev": jev_now, "texts": after_t, "n": n}
    if n >= 3:
        out.append(f"[循环] 同一条命令在同一屏已连续第 {n} 次，换个做法，或 kp handoff")
    k1_save(**upd)
    return out


def screen_hint(items):
    """JEV phone_screen：一次约 0.6 s 判断当前屏类型，给出下一步。结算页顺手把价格结构也打出来。"""
    t_jev = time.time()
    j = jev("phone_screen", items)
    dur_jev = round((time.time() - t_jev) * 1000)
    if not j:
        klog(jev_screen=None, dur_jev=dur_jev, jev_url=None)  # K0：JEV 没答也记一行，统计才算得出不可用比例
        k1_save(jev_screen=None)
        print("[JEV] 不可用"); return None
    scr, nxt, ms = j[0].get("screen"), j[1].get("next"), j[3]
    if scr == "other" and "look" in str(nxt):
        nxt = "已读屏，无需再 look"  # JEV 模板对 other 回的是 look，但这一屏已经读过了
    print(f"[JEV {ms}ms] 屏={scr} → {nxt}")
    klog(jev_screen=scr, jev_ms=ms, dur_jev=dur_jev, jev_url=LAST_JEV.get("url"))
    k1_save(jev_screen=scr)
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


def act(ops, after=900, quiet=False, grep=None, target=None, expect=None, restart=None, before=None, track=False, ret=False):
    """target 是这一下点的按钮文字（节奏按它认发送 / 打招呼这类对外动作）。
    expect（K1）：护栏判定所用那次读屏的指纹，交给桥在按下之前核对；桥回 stale 就没点，调 restart() 完整重读、
    重新过 guard、拿到新的 (ops, target) 再点一次（只重试这一次）。
    before（K2）：点之前那一屏的 items，用来判断「这一下没起作用」；track=True 的动作（tap / key）才判，其余只更新状态。
    ret（K3）：scan / scroll-until 用。不打印这一屏，只把 (读屏结果, JEV 屏类型, JEV 回显文字) 还给调用方，由它决定怎么输出。"""
    t = T0 = time.time()
    pre = {"read": 0.0}

    def pre_read():  # K0：单独量点之前读屏花的时间（kpx.before 里的节奏停留不算在内）
        t1 = time.time()
        try:
            return call("/read?cache=1")["items"]
        finally:
            pre["read"] += time.time() - t1
    kpx.before(pre_read, target)
    dur_pre = time.time() - t
    t = time.time()
    t_act = time.time()
    resp = call("/act", {"ops": ops, "expect": expect} if expect else {"ops": ops})
    if expect and resp.get("stale"):
        print("[屏已变] 点之前画面变了，这一下没点；重新读屏、重新过护栏后再点一次")
        klog(k1_stale=True)
        before = False  # 点之前的屏已经作废，这一下没法和它比
        if restart:
            ops, target = restart()
        t_act = time.time()
        call("/act", {"ops": ops})
    dur_act = time.time() - t_act
    t = dbg("动作", t)
    ms = lambda v: round(v * 1000)
    base = dict(dur_pre_read=ms(pre["read"]), dur_pace=ms(max(dur_pre - pre["read"], 0)), dur_act=ms(dur_act))
    if quiet and not os.environ.get("KP_SETTLE"):
        kpx.after(None, True, target)
        k1_save(last_texts=None, noop_xy=None, noop_texts=None, loop=None)  # 没读屏，不知道现在是哪一屏，旧的无变化 / 循环记录作废
        klog(act_done=True, dur_total=ms(time.time() - T0), **base); return
    LAST_SETTLE.clear()
    t_set = time.time()
    r = settle()
    dur_settle = time.time() - t_set
    t = dbg(f"等稳定+读屏 stable={r.get('stable')} settle_ms={r.get('settle_ms')}", t)
    kpx.after(r["items"], True, target)
    k1_save(checkout=bool(on_checkout(r["items"])))
    if ret:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            jev_now = screen_hint(r["items"])
        klog(act_done=True, dur_total=ms(time.time() - T0), dur_settle=ms(dur_settle), dur_ocr=LAST_SETTLE.get("ocr_ms"),
             settle_stable=LAST_SETTLE.get("stable"), **base)
        return r, jev_now, buf.getvalue()
    if quiet:
        k1_save(last_texts=k2_texts(r["items"]), noop_xy=None, noop_texts=None, loop=None)
        klog(act_done=True, dur_total=ms(time.time() - T0), dur_settle=ms(dur_settle), dur_ocr=LAST_SETTLE.get("ocr_ms"),
             settle_stable=LAST_SETTLE.get("stable"), settle_fallback=LAST_SETTLE.get("fallback"), **base)
        return
    jev_prev = k1_load().get("jev_screen")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):  # 先拿到 JEV 的屏类型，再把「无变化 / 循环」放在最前面输出
        jev_now = screen_hint(r["items"])
    dbg("JEV", t)
    if not os.environ.get("KP_NOAUTO"):
        bt = None if before is False else (k2_texts(before) if before is not None else (k1_load().get("last_texts") if track else None))
        for line in k2_after(r["items"], bool(r.get("stable")), bt, ops, jev_prev, jev_now, track):
            print(line)
    sys.stdout.write(buf.getvalue())
    show_screen(r["items"], grep, FULL_OUT)
    # K0：整步分段耗时（dur_jev / jev_url 在上面 screen_hint 那行 jev_screen 记录里）
    klog(act_done=True, dur_total=ms(time.time() - T0), dur_settle=ms(dur_settle), dur_ocr=LAST_SETTLE.get("ocr_ms"),
         dur_frame=LAST_SETTLE.get("frame_ms"), settle_stable=LAST_SETTLE.get("stable"), settle_fallback=LAST_SETTLE.get("fallback"),
         bridge_settle_ms=LAST_SETTLE.get("bridge_settle_ms"), jev_url=LAST_JEV.get("url"), **base)


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


# 闲鱼社交触发护栏：这三个按钮会通知卖家或直接下单，任务没要求就不点（付款类拦截不受影响，仍然优先）
XY_DENY = {"我想要", "关注", "立即购买"}
XY_HINT = {"聊一聊", "私信", "发消息"}
XY_SCREEN = re.compile(r"闲鱼|验货宝")


def on_xianyu(items):
    """判断当前屏是不是闲鱼：屏上有「闲鱼 / 验货宝」字样，或「我想要」和「聊一聊」同屏（商品详情页底栏）。"""
    texts = [it["t"].strip() for it in items]
    if any(XY_SCREEN.search(t) for t in texts):
        return True
    return "我想要" in texts and "聊一聊" in texts


def xianyu_social(items, near, force=False):
    """near 是落点附近的文字条目。命中「我想要 / 关注 / 立即购买」默认拒绝（退出码 3），-f 放行；
    命中「聊一聊 / 私信 / 发消息」不拒，只多打一行提示。"""
    if not on_xianyu(items):
        return
    for it in near:
        t = it["t"].strip()
        if t in XY_DENY and not force:
            print(f"拒绝：「{t}」在闲鱼里会通知卖家或下单，任务没要求就别点；确需点用 -f（kp tap {t} -f）")
            klog(guard=f"闲鱼社交:{t}"); sys.exit(3)
        if t in XY_HINT:
            print(f"[提示] 「{t}」会给卖家发消息，任务没要求就别点")


def guard(items, x, y, coord=False, force=False):
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
    xianyu_social(items, near, force)  # 付款类拦截先过，上面一条都没变
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


FWD_ENV = ("KP_DEBUG", "KP_SETTLE", "KP_JOB_DIR", "KP_JOB_ID", "KP_LOG", "KP_T0", "KP_BUDGET", "KP_NOTIFY", "KP_DEVNAME",
           "KP_DIFF", "KP_NOAUTO")


def forward(host, argv):
    import shlex
    # 任务变量也带过去（11-10：Mac 上的 CC 驾驶偶尔绕过垫片直接用本机 kp，只带身份时 mini 上没有 kp.jsonl、预算提醒、插话和任务目录）
    env = " ".join(f"{k}={shlex.quote(os.environ[k])}" for k in FWD_ENV if os.environ.get(k))
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
        key("Space", ["ControlLeft"])  # 硬件键盘语言只认 Ctrl+Space（地球键只切软键盘，11-18）
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
    # 闲鱼的 scheme 只能打开搜索页，关键词带不进去（11-06 试了 keyword / q / searchKey / query 都不认）：
    # 表里写了 search_box（搜索框占位字）的，打开后点框、敲词、回车。只敲 ASCII，中文让驾驶自己 kp paste
    if q and ent.get("search_box"):
        box = None
        for _ in range(8):
            box = next((it for it in r["items"] if ent["search_box"] in it["t"]), None)
            if box:
                break
            time.sleep(0.5); r = settle(0.3)
        if box and q.isascii():
            call("/act", {"ops": [{"op": "tap", "x": box["x"] + box["w"] // 2, "y": box["y"] + box["h"] // 2}]})
            time.sleep(0.6); ime_en()
            call("/act", {"ops": [{"op": "type", "text": q}]}); key("Enter")
            time.sleep(2.5); r = settle(0.3)
        else:
            q = ""
            print(f"[open] {name} 只能直达搜索页：{'没找到搜索框' if not box else '关键词带中文，点搜索框后用 kp paste 自己打'}")
    kpx.after(r["items"], True, None)
    print(f"[open] 已经 {name} 直达{'「' + q + '」搜索结果' if q else '首页'}；不对就照常点图标进")
    if quiet:
        return
    screen_hint(r["items"]); show(r["items"], grep)


SWIPE_BATCH = 8  # kp up / down 每批最多滑几次；更大的 N 自动分批（一批发太长桥会超时报 500）


def swipe_ops(cmd, W, U, span=None):
    """一次上滑 / 下滑的动作序列。每次划的位置、长度、斜度、快慢都不一样；拖到位停一下再松手（不甩），
    画面松手就停，一次约翻 60% 屏，不用等惯性滚完。
    span=(起点y下限, 上限, 长度下限, 上限)：日历这类只占半屏的区域用，起点放在区域里、划得短一点，避免划到下面的时间滚轮。"""
    x1 = W * U(0.62, 0.86)
    y1 = U(880, 930) if span is None else U(span[0], span[1])
    L = U(600, 680) if span is None else U(span[2], span[3])
    x2 = x1 + U(-22, 12); y2 = y1 - L
    if cmd == "down":
        y1, y2 = 1150 - y1, 1150 - y2
    ms = U(560, 660)  # 快于约 0.5 s 会触发惯性滚动，要等 2–3 s 才停
    return [{"op": "drag", "x1": round(x1), "y1": round(y1), "x2": round(x2), "y2": round(y2), "ms": round(ms), "steps": round(ms / 12),
             "stop": round(U(60, 100))},
            {"op": "wait", "ms": round(U(80, 160))}]


# ───────────── K3：列表扫描宏 ─────────────
SCAN_MAX_PAGES = 6
SCROLL_MAX = 8
PRICE_RE = re.compile(r"[¥￥]\s*(\d+(?:\.\d+)?)")
COND_RE = re.compile(r"几乎全新|全新|未拆封|9[0-9]新|[5-9]成新|轻微使用痕迹|明显使用痕迹|有瑕疵|二手")
WANT_RE = re.compile(r"(\d+)\s*人(?:想要|收藏|关注)")
SHOP_RE = re.compile(r"店$|旗舰|专营|专卖|官方|直营|商城")


def scan_lines(items):
    """去掉状态栏和会自己变的时间 / 电量，剩下是这一屏真正的内容行。"""
    return [it for it in items if it["y"] >= STATUS_Y and it["t"].strip() and not _VOLATILE.match(it["t"].strip())]


def scan_candidates(items, page):
    """价格行 → 候选结构（价格 / 标题 / 店名 / 成色 / 想要人数；读不出的留空）。价格原样取屏上的数字，不改。
    同一张卡片的字在纵向上挨在一起，以价格行为中心取上下 130 像素内的行当邻居。"""
    rows = scan_lines(items)
    out = []
    for it in rows:
        if on_checkout([it]) or "已优惠" in it["t"]:
            continue
        ms = PRICE_RE.findall(it["t"])
        if not ms:
            continue
        cy = it["y"] + it["h"] // 2
        near = [o for o in rows if o is not it and abs(o["y"] + o["h"] // 2 - cy) <= 130]
        pool = [it] + near
        title = ""
        for o in sorted(near, key=lambda o: -len(o["t"])):
            t = o["t"].strip()
            if PRICE_RE.search(t) or WANT_RE.search(t) or SHOP_RE.search(t) or len(HAN.findall(t)) == 0:
                continue
            if sum(len(h) for h in HAN.findall(t)) >= 4:
                title = t; break
        shop = next((o["t"].strip() for o in pool if SHOP_RE.search(o["t"]) and not PRICE_RE.search(o["t"])), "")
        cond = next((m.group(0) for o in pool if (m := COND_RE.search(o["t"]))), "")
        want = next((m.group(1) for o in pool if (m := WANT_RE.search(o["t"]))), "")
        out.append({"page": page, "price": ms[-1], "title": kpx.redact(title), "shop": kpx.redact(shop), "condition": cond,
                    "want": want, "raw": kpx.redact(it["t"])})
    return out


def scan_write_candidates(rows):
    if not rows:
        return None
    d = os.path.dirname(os.environ["KP_LOG"]) if os.environ.get("KP_LOG") else "/tmp/kvmphone"
    try:
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "candidates.jsonl")
        with open(path, "a") as f:
            for r in rows:
                f.write(json.dumps({"ts": round(time.time(), 2), **r}, ensure_ascii=False) + "\n")
        return path
    except Exception:
        return None


def scan_abort(items, jev_scr, hint_text=""):
    """遇结算 / 支付页立即中止：打印这一屏、退出码 3（和付款护栏同一个口径）。"""
    why = on_checkout(items)
    if not why and jev_scr not in ("checkout", "payment"):
        return
    if hint_text:
        sys.stdout.write(hint_text)
    print(f"[中止] 翻到了结算 / 支付页（{kpx.redact(why) if why else 'JEV 判为 ' + str(jev_scr)}），不再自动翻；当前屏：")
    show(items)
    klog(guard="列表宏遇结算页")
    sys.exit(3)


def scan_first(after):
    """宏的起点：读当前屏并过一遍结算页检查（OCR + JEV）。"""
    r = settle(0.1)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        scr = screen_hint(r["items"])
    scan_abort(r["items"], scr, buf.getvalue())
    return r


def scan_step(after):
    """上滑一次（一次约翻 60% 屏）；节奏护栏、租约在 act / kpx 里照常走。"""
    import random
    r, scr, hint = act(swipe_ops("up", 500, random.uniform), after, False, None, ret=True)
    scan_abort(r["items"], scr, hint)
    return r


def scan_overlap(prev_pos, items):
    """相邻两屏里「文字相同、纵向位置挪动过」的行数。固定不动的顶栏 / 标签栏不算重叠证据。"""
    n = 0
    for it in scan_lines(items):
        k = norm(it["t"])
        if len(k) >= 2 and k in prev_pos and abs(prev_pos[k] - it["y"]) > 100:
            n += 1
    return n


def scan_shift(prev_pos, rows):
    """相邻两页的滚动位移：非价格行里「同文字、位移超过 100 像素」的位移按 30 像素分桶取最多的一桶的中位数；估不出返回 None。"""
    ds = [prev_pos[norm(it["t"])] - it["y"] for it in rows
          if len(norm(it["t"])) >= 2 and norm(it["t"]) in prev_pos and not PRICE_RE.search(it["t"])
          and abs(prev_pos[norm(it["t"])] - it["y"]) > 100]
    if not ds:
        return None
    from collections import Counter
    b = Counter(d // 30 for d in ds).most_common(1)[0][0]
    g = sorted(d for d in ds if d // 30 == b)
    return g[len(g) // 2]


def scan_new_rows(rows, prev_pos, fixed_pos):
    """本页相对前页新增的行。只认真正的滚动重叠：同文字且相邻页纵向位移超过 100 像素（有位移估计时还要和它吻合），
    或固定不动的栏（同文字同位置，非价格）。带 ¥ 的行不按纯文字去重：不同商品的同价行必须印出来，
    只有位移和这一页的滚动位移吻合（确是同一行被滚上来）才算重复。"""
    if rows and prev_pos and all(norm(it["t"]) in prev_pos and abs(prev_pos[norm(it["t"])] - it["y"]) <= 8 for it in rows):
        return []  # 整页和前一页逐行同位置：没滚动（到底了），不是新内容
    shift = scan_shift(prev_pos, rows)
    new = []
    for it in rows:
        k = norm(it["t"])
        price = bool(PRICE_RE.search(it["t"]))
        dup = False
        if k in prev_pos:
            d = prev_pos[k] - it["y"]
            if price:
                dup = shift is not None and abs(d - shift) <= 40
            else:
                dup = abs(d) > 100 and (shift is None or abs(d - shift) <= 60)
        if not dup and not price:
            # 固定栏：同文字同位置在至少 2 个前页出现，或在顶部筛选栏区（y<260）出现过；
            # 只在前一页同位置出现一次不算（滚动量刚好等于卡片间距时，不同商品的「包邮」会同位置）
            cnt = sum(c for y, c in fixed_pos.get(k, {}).items() if abs(y - it["y"]) <= 8)
            if cnt >= 2 or (cnt >= 1 and it["y"] < 260):
                dup = True
        if not dup:
            new.append(it)
    return new


def scan_cmd(args, after):
    if os.environ.get("KP_NOAUTO"):
        print("KP_NOAUTO=1，列表宏已关闭；用 kp up / kp read 手动翻"); sys.exit(2)
    pages, grep, stop = 4, None, None
    a = list(args)
    try:
        for flag in ("--pages", "--grep", "--stop-when"):
            if flag in a:
                i = a.index(flag)
                if i + 1 >= len(a):
                    raise ValueError(flag)
                v = a[i + 1]; del a[i:i + 2]
                if flag == "--pages":
                    pages = int(v)
                elif flag == "--grep":
                    grep = re.compile(v, re.I)
                else:
                    stop = v
        if a or pages < 1:
            raise ValueError("多余参数" if a else "--pages")
    except (ValueError, re.error):
        print("用法：kp scan [--pages N（1 到 6，默认 4）] [--grep 正则] [--stop-when 文字]"); sys.exit(2)
    pages = min(pages, SCAN_MAX_PAGES)
    r = scan_first(after)
    out, cands, cand_seen = [], [], set()
    fixed_pos = {}  # 文字 -> {纵向位置: 出现的页数}，用来认固定不动的栏
    prev_pos, hit, notes, scrolls = {}, None, [], 0
    for p in range(pages + 1):
        if p:
            r = scan_step(after); scrolls += 1
        rows = scan_lines(r["items"])
        klog(screen=" | ".join(it["t"] for it in r["items"])[:4000], scan_page=p)
        new = scan_new_rows(rows, prev_pos, fixed_pos)
        if p and rows:
            if not new:
                notes.append(f"第 {p + 1} 页没有新内容，已到底，停止"); out.append((p, [])); break
            if scan_overlap(prev_pos, r["items"]) == 0:
                notes.append(f"[可能漏行] 第 {p} 页和第 {p + 1} 页之间没找到挪动过位置的重复行，上滑幅度可能过大，中间或有行没读到")
        for c in scan_candidates(r["items"], p + 1):
            key = (c["price"], c["title"])
            if key not in cand_seen:
                cand_seen.add(key); cands.append(c)
        for it in rows:
            d_ = fixed_pos.setdefault(norm(it["t"]), {}); d_[it["y"]] = d_.get(it["y"], 0) + 1
        prev_pos = {norm(it["t"]): it["y"] for it in rows}
        out.append((p, new))
        if stop and any(norm(stop) in norm(it["t"]) for it in rows):
            hit = p + 1; break
    path = scan_write_candidates(cands)
    last = out[-1][0]
    print(f"[scan] 自动上滑 {scrolls} 次，读了 {len(out)} 屏，共 {sum(len(n) for _, n in out)} 行新内容；"
          f"只有最后一屏（第 {last + 1} 页）的坐标现在仍有效，更早页的行只有文字")
    for p, new in out:
        shown = [it for it in new if not grep or grep.search(it["t"])]
        print(f"--- 第 {p + 1} 页，新增 {len(new)} 行" + (f"，匹配 {len(shown)} 行" if grep else "") + " ---")
        for it in shown:
            if p == last:
                print(f'{it["x"] + it["w"] // 2},{it["y"] + it["h"] // 2}\t{kpx.redact(it["t"])}')
            else:
                print(f'  {kpx.redact(it["t"])}')
    for n in notes:
        print(n)
    if hit:
        print(f"[停] 第 {hit} 页出现了「{stop}」，不再往下翻")
    if cands:
        print(f"[候选] 读出 {len(cands)} 条带价格的行，已写入 {path or '（写入失败）'}")
    k1_save(noop_n=0, noop_xy=None, noop_texts=None, loop=None, last_texts=k2_texts(r["items"]), diff_ts=0)  # 模型没看过完整的最后一屏：下一次动作给全量


def scroll_until_cmd(args, after):
    if os.environ.get("KP_NOAUTO"):
        print("KP_NOAUTO=1，列表宏已关闭；用 kp up / kp read 手动翻"); sys.exit(2)
    a, mx = list(args), 6
    if "--max" in a:
        i = a.index("--max")
        if i + 1 >= len(a) or not a[i + 1].isdigit() or int(a[i + 1]) < 1:
            print("用法：kp scroll-until 文字 [--max N（1 到 8，默认 6）]"); sys.exit(2)
        mx = min(int(a[i + 1]), SCROLL_MAX); del a[i:i + 2]
    target = " ".join(a).strip()
    if not target:
        print("用法：kp scroll-until 文字 [--max N（1 到 8，默认 6）]"); sys.exit(2)
    r = scan_first(after)
    n = 0
    while True:
        hit = next((it for it in r["items"] if it["y"] >= STATUS_Y and norm(target) in norm(it["t"])), None)
        if hit:
            print(f"[找到] 上滑 {n} 次后「{target}」出现在 {hit['x'] + hit['w'] // 2},{hit['y'] + hit['h'] // 2}；当前屏：")
            show(r["items"]); diff_remember(r["items"]); return
        if n >= mx:
            print(f"[没找到] 上滑 {n} 次仍没有「{target}」，已达上限；当前屏：")
            show(r["items"]); diff_remember(r["items"]); return
        prev = {norm(it["t"]) for it in scan_lines(r["items"])}
        r = scan_step(after); n += 1
        if prev and {norm(it["t"]) for it in scan_lines(r["items"])} <= prev:
            hit = next((it for it in r["items"] if norm(target) in norm(it["t"])), None)
            if hit:
                continue
            print(f"[没找到] 上滑 {n} 次后已到底，没有「{target}」；当前屏：")
            show(r["items"]); diff_remember(r["items"]); return


# ───────────── K4：日期 / 地点宏 ─────────────
# 依据任务 15ff（携程租车、租租车）的真实屏文字：日历是竖向连续滚动，同屏可见两个月（月份标题「2026年11月」，下面是日期格，
# 选中后格子上出现「取车」「还车」，底部一行「取（当地）：11-05 13:00 … 还（当地）：11-08 13:00（共3天）」，再下面是「确定」）；
# 地点页是「搜索框 + 候选列表」，选中候选后回到表单，表单上写着地名。屏文字里日期数字的顺序是乱的，所以一律按坐标和月份标题定位。
# 待真机验证：两个 App 之外的日历（单月翻页、带箭头的）不一定适用，失败时会退回手动并打印当前屏。
import calendar as _cal
import datetime as _dt
import difflib as _dl

DATE_MAX_FLIPS = 12
MONTH_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月")
DAY_RE = re.compile(r"^\d{1,2}$")
RB_PICK = re.compile(r"取\s*[（(]?\s*当地\s*[）)]?\s*[：:]\s*(?:(\d{4})\s*[-年/]\s*)?(\d{1,2})\s*[-月/]\s*(\d{1,2})")
RB_DROP = re.compile(r"还\s*[（(]?\s*当地\s*[）)]?\s*[：:]\s*(?:(\d{4})\s*[-年/]\s*)?(\d{1,2})\s*[-月/]\s*(\d{1,2})")
DATE_ANCHOR = re.compile(r"当地取车时间|当地还车时间|取\s*[（(]\s*当地|还\s*[（(]\s*当地|^确定$")
PLACE_BOX = re.compile(r"请输入|机场\s*/\s*(火)?车站|搜索地点|^[Qq]\s*\S")
PLACE_HOLDER = re.compile(r"请输入|机场\s*/\s*(火)?车站|搜索地点")
PLACE_SKIP = re.compile(r"^(地标|机场|火车站|景点|酒店|购物中心|博物馆|剧院|历史搜索|取消|更改城市.*|取还车地点|选择.{0,4}地点)$|正在加载|^(123|space|return|go|search|English)$", re.I)
INF = 10 ** 6


def macro_off(name):
    if os.environ.get("KP_NOAUTO"):
        print(f"KP_NOAUTO=1，{name} 宏已关闭；用 kp tap / kp paste 手动做"); sys.exit(2)


def macro_end(items):
    """宏收尾：给下一条动作的差分输出做底，状态里的无变化 / 循环记录作废。"""
    k1_save(noop_n=0, noop_xy=None, noop_texts=None, loop=None, last_texts=k2_texts(items), diff_ts=0)


def macro_fail(tag, n, step, why, items):
    print(f"[{tag}] 第 {n} 步失败（{step}）：{why}；已退回手动，当前屏：")
    show(items)
    macro_end(items)
    klog(macro_fail=f"{tag}:{n}:{step}")
    sys.exit(1)


def macro_tap(items, x, y, after, target=None, thumb=None):
    """宏里的每一次点击：先过 guard()（坐标口径，结算页只放行左上角返回），点完等稳、读屏，遇结算 / 支付页立即中止。
    thumb 是上一步读屏的指纹（调用方传 r.get("thumb")）：交给桥在按下前核对，画面变了桥不点、回 stale，
    这里重读、重新过 guard、再点一次；重读后若是结算 / 支付页，guard 与 scan_abort 照常中止。没有 thumb（旧桥）时保持原样。"""
    import random
    x += round(max(-2, min(2, random.gauss(0, 1.0)))); y += round(max(-2, min(2, random.gauss(0, 1.0))))
    t = guard(items, x, y, coord=True)
    expect = {"thumb": thumb} if thumb else None
    old_texts = k2_texts(items)

    def restart():
        r2 = call("/read?settle=200")
        k1_save(checkout=bool(on_checkout(r2["items"])))
        scan_abort(r2["items"], None)
        t2 = guard(r2["items"], x, y, coord=True)
        if k2_sim(old_texts, k2_texts(r2["items"])) < 0.8:  # 换了一屏：原来算好的坐标不再可信，不盲点
            print("[宏中止] 点之前画面已经换了一屏，原定落点作废，没有点；当前屏：")
            show(r2["items"]); macro_end(r2["items"]); klog(macro_fail="stale换屏"); sys.exit(1)
        return [{"op": "tap", "x": x, "y": y}], target or t
    r, scr, hint = act([{"op": "tap", "x": x, "y": y}], after, False, None, target or t, expect, restart if expect else None, before=items, ret=True)
    scan_abort(r["items"], scr, hint)
    return r


def date_parse(s):
    m = re.match(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?$", s)
    if m:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))), True
    m = re.match(r"^(\d{1,2})[-/.月](\d{1,2})日?$", s)
    if m:
        return (int(m.group(1)), int(m.group(2))), False
    raise ValueError(s)


def date_labels(items):
    """屏上的月份标题，从上到下：[(y, (年, 月), item)]。同一个月只留最靠上的一条。"""
    out, seen = [], set()
    for it in sorted(items, key=lambda i: (i["y"], i["x"])):
        if it["y"] < STATUS_Y:
            continue
        m = MONTH_RE.search(it["t"])
        if m and 1 <= int(m.group(2)) <= 12 and (int(m.group(1)), int(m.group(2))) not in seen:
            ym = (int(m.group(1)), int(m.group(2))); seen.add(ym)
            out.append((it["y"], ym, it))
    return out


def date_anchor(items, above):
    """日历区的下边界：下面的时间滚轮 / 「取（当地）」行 / 「确定」里最靠上的那条。"""
    ys = [it["y"] for it in items if it["y"] > above and DATE_ANCHOR.search(it["t"].strip())]
    return min(ys) if ys else INF


def date_cells(items):
    """按月份标题把数字格分到各月：标题到下一个标题（或下边界）之间的纯数字，且不超过当月天数。返回 (标题列表, {月: [(日, 中心x, 中心y)]})。"""
    labs = date_labels(items)
    out = {}
    for i, (ly, ym, lit) in enumerate(labs):
        top = lit["y"] + lit["h"] - 5
        bot = labs[i + 1][0] if i + 1 < len(labs) else date_anchor(items, ly)
        dim = _cal.monthrange(*ym)[1]
        cs = []
        for it in sorted(items, key=lambda i: (i["y"], i["x"])):
            t = it["t"].strip()
            cy = it["y"] + it["h"] // 2
            if DAY_RE.match(t) and 1 <= int(t) <= dim and top < cy < bot:
                cs.append((int(t), it["x"] + it["w"] // 2, cy))
        out[ym] = cs
    return labs, out


def _median(v):
    v = sorted(v); n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def _line_fit(pts):
    """pts 是 [(序号, 值)]，至少两个不同序号；返回 f(序号)。多于两点用最小二乘。"""
    n = len(pts); sx = sum(p[0] for p in pts); sy = sum(p[1] for p in pts)
    sxx = sum(p[0] ** 2 for p in pts); sxy = sum(p[0] * p[1] for p in pts)
    den = n * sxx - sx ** 2
    if den == 0:
        return None
    a = (n * sxy - sx * sy) / den
    b = (sy - a * sx) / n
    return lambda k: a * k + b


def date_infer(ym, cells, day):
    """OCR 漏读了目标那一格时，用同月已认出的格子推算位置：日历第一列是周日，由当月 1 号是星期几算出每个日期的行列，
    列的 x 取同列已认出格子的中位数，行的 y 取同行的中位数，缺的列 / 行按已知的等距外推。认出的格子太少（不到两列或两行）就不推。"""
    wd0 = (_cal.weekday(ym[0], ym[1], 1) + 1) % 7
    pos = lambda d: ((wd0 + d - 1) % 7, (wd0 + d - 1) // 7)
    xs, ys = {}, {}
    for d, cx, cy in cells:
        c, r = pos(d)
        xs.setdefault(c, []).append(cx); ys.setdefault(r, []).append(cy)
    if len(xs) < 2 or len(ys) < 2:
        return None
    fx = _line_fit([(c, _median(v)) for c, v in xs.items()])
    fy = _line_fit([(r, _median(v)) for r, v in ys.items()])
    if not fx or not fy:
        return None
    c, r = pos(day)
    x = _median(xs[c]) if c in xs else fx(c)
    y = _median(ys[r]) if r in ys else fy(r)
    return round(x), round(y)


def date_find(items, tgt):
    """在当前屏找目标日期。返回 ("hit", x, y, 怎么找到的) / ("scroll", "up"|"down", 原因) / ("fail", 原因)。"""
    labs, cells = date_cells(items)
    if not labs:
        return ("fail", "屏上没认出「XXXX年X月」月份标题，这不像日历页")
    ym = (tgt.year, tgt.month)
    months = [l[1] for l in labs]
    if ym not in months:
        return ("scroll", "up" if ym > max(months) else "down", f"屏上是 {months[0][0]}年{months[0][1]}月"
                + (f"到{months[-1][0]}年{months[-1][1]}月" if len(months) > 1 else "") + f"，目标 {ym[0]}年{ym[1]}月不在屏上")
    cs = cells.get(ym, [])
    hit = next((c for c in cs if c[0] == tgt.day), None)
    if hit:
        return ("hit", hit[1], hit[2], "按识别到的数字格")
    guess = date_infer(ym, cs, tgt.day)
    if guess:
        i = months.index(ym)
        lit = labs[i][2]
        lo = lit["y"] + lit["h"]
        hi = labs[i + 1][0] if i + 1 < len(labs) else date_anchor(items, labs[i][0])
        if lo < guess[1] < hi - 10:
            return ("hit", guess[0], guess[1], "OCR 漏读了这一格，按同月其他格子推算位置（回读会核对）")
    return ("scroll", "up", f"{ym[0]}年{ym[1]}月在屏上但这一格还没滚进来")


def date_readback(items):
    """读底部「取（当地）：MM-DD … 还（当地）：MM-DD」一行（携程写 11-05，租租车写 11月05日）。返回 (取, 还, 还车未选)；认不出的是 None。"""
    pick = drop = None
    unset = False
    for it in items:
        t = it["t"]
        m = RB_PICK.search(t)
        if m and pick is None:
            pick = (int(m.group(2)), int(m.group(3)))
        m = RB_DROP.search(t)
        if m and drop is None:
            drop = (int(m.group(2)), int(m.group(3)))
        if re.search(r"还.{0,6}[：:]\s*未选择", t):
            unset = True
    return pick, drop, unset


def date_marker_ok(items, label, day):
    """没有底部回读行时的退路：「取车 / 还车」标记旁边（左右 70、上下 90 像素内）有写着该日数字的格子。"""
    for mk in (i for i in items if re.search(label, i["t"])):
        mx, my = mk["x"] + mk["w"] // 2, mk["y"] + mk["h"] // 2
        for it in items:
            if it["t"].strip() == str(day) and abs(it["x"] + it["w"] // 2 - mx) <= 70 and abs(it["y"] + it["h"] // 2 - my) <= 90:
                return True
    return False


def date_cmd(args, after):
    macro_off("日期")
    a = list(args)
    confirm = "--no-ok" not in a
    a = [x for x in a if x != "--no-ok"]
    try:
        if not 1 <= len(a) <= 2:
            raise ValueError("个数")
        raw = [date_parse(x) for x in a]
    except ValueError:
        print("用法：kp date 开始 [结束] [--no-ok]（日期写 11-06 或 2026-11-06；--no-ok 选完不点确定）"); sys.exit(2)
    steps, n = [], [0]

    def step(name):
        n[0] += 1; klog(date_step=name); return n[0]
    r = scan_first(after)                                             # 第 1 步：读当前屏（含结算页检查）
    step("读屏认月份")
    labs = date_labels(r["items"])
    if not labs:
        macro_fail("date", 1, "读屏认月份", "屏上没认出「XXXX年X月」月份标题，这不像日历页，先用 kp look 看看在哪一屏", r["items"])
    y0, m0 = labs[0][1]
    dates = []
    for (v, full) in raw:
        if full:
            dates.append(v); continue
        try:
            if dates:  # 结束日期没写年：取不早于开始日期的最近一年
                cand = []
                for k in (0, 1, 2, 3, 4):
                    try:
                        cand.append(_dt.date(dates[0].year + k, v[0], v[1]))
                    except ValueError:
                        pass
                dates.append(next(c for c in cand if c >= dates[0]))
            else:  # 开始日期没写年：取离屏上第一个月份标题最近的那一年
                cand = []
                for k in (-1, 0, 1):
                    try:
                        cand.append(_dt.date(y0 + k, v[0], v[1]))
                    except ValueError:
                        pass  # 例如 2 月 29 日只在闰年存在
                dates.append(min(cand, key=lambda c: abs((c.year - y0) * 12 + c.month - m0)))
        except (ValueError, StopIteration):
            print(f"日期不存在：{v[0]}-{v[1]}"); sys.exit(2)
    if len(dates) == 2 and dates[1] < dates[0]:
        print("结束日期早于开始日期"); sys.exit(2)
    flips = 0
    for di, tgt in enumerate(dates):
        word = "取车日" if di == 0 else "还车日"
        while True:
            res = date_find(r["items"], tgt)
            if res[0] == "fail":
                macro_fail("date", step("找" + word), "找" + word, res[1], r["items"])
            if res[0] == "hit":
                break
            if flips >= DATE_MAX_FLIPS:
                macro_fail("date", step("翻月"), "翻月", f"已翻 {flips} 次仍没到 {tgt.year}年{tgt.month}月（{res[2]}）", r["items"])
            import random
            anchor = date_anchor(r["items"], 0)
            base = min(max((anchor if anchor < INF else 800) - 90, 480), 760)
            r, scr, hint = act(swipe_ops(res[1], 500, random.uniform, (base - 20, base + 20, 260, 320)), after, False, None, ret=True)
            scan_abort(r["items"], scr, hint)
            flips += 1
        _, x, y, how = res
        s = step("点" + word)
        r = macro_tap(r["items"], x, y, after, thumb=r.get("thumb"))
        steps.append(f"点{word} {tgt.month}-{tgt.day}（{how}）")
        klog(date_tap=f"{tgt.month}-{tgt.day}", how=how)
    # 回读
    pick, drop, unset = date_readback(r["items"])
    want_p = (dates[0].month, dates[0].day)
    want_d = (dates[1].month, dates[1].day) if len(dates) == 2 else None
    bad = []
    if pick is not None:
        if pick != want_p:
            bad.append(f"取车日读到 {pick[0]}-{pick[1]}，要的是 {want_p[0]}-{want_p[1]}")
    elif not date_marker_ok(r["items"], "取车|出发|入住|开始", want_p[1]):
        bad.append("屏上没有「取（当地）」那一行，「取车」标记旁也没认出该日数字，无法确认")
    if want_d:
        if drop is not None:
            if drop != want_d:
                bad.append(f"还车日读到 {drop[0]}-{drop[1]}，要的是 {want_d[0]}-{want_d[1]}")
        elif not date_marker_ok(r["items"], "还车|返程|离店|结束", want_d[1]):
            bad.append("屏上没有「还（当地）」那一行，「还车」标记旁也没认出该日数字，无法确认")
    step("回读核对")
    if bad:
        macro_fail("date", n[0], "回读核对", "；".join(bad), r["items"])
    steps.append("回读一致：" + (f"取 {pick[0]}-{pick[1]}" if pick else f"取 {want_p[0]}-{want_p[1]}（按标记）")
                 + ((f"，还 {drop[0]}-{drop[1]}" if drop else f"，还 {want_d[0]}-{want_d[1]}（按标记）") if want_d else ""))
    if confirm:
        ok = next((it for it in r["items"] if it["t"].strip() == "确定" and it["y"] >= 600), None)
        if ok:
            step("点确定")
            r = macro_tap(r["items"], ok["x"] + ok["w"] // 2, ok["y"] + ok["h"] // 2, after, "确定", thumb=r.get("thumb"))
            steps.append("点确定")
        else:
            steps.append("没找到「确定」，没点；需要的话自己 kp tap 确定")
    print(f"[date] 自动做了：翻月 {flips} 次；" + "；".join(steps) + "。时间滚轮没动，要改时间另调。当前屏：")
    show(r["items"])
    diff_remember(r["items"])
    macro_end(r["items"])
    k1_save(diff_ts=round(time.time()))


def fuzzy_has(a, b):
    """a 是否（容忍 OCR 错字）出现在 b 里。比较前统一成 NFKC、去空格、小写。"""
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a in b:
        return 1.0
    w = len(a)
    if len(b) <= w:
        return _dl.SequenceMatcher(None, a, b).ratio()
    return max(_dl.SequenceMatcher(None, a, b[i:i + w]).ratio() for i in range(len(b) - w + 1))


def place_rank(name, text):
    """候选与地名的匹配档：0 完全相同、1 以地名开头、2 包含地名、3 近似（OCR 错字）；不沾边是 None。"""
    a, b = norm(name), norm(text)
    if not a or not b:
        return None
    if a == b:
        return 0
    if b.startswith(a):
        return 1
    if a in b:
        return 2
    return 3 if fuzzy_has(name, text) >= 0.72 else None


def place_box(items):
    """搜索框：靠上（状态栏以下、330 以内）、写着占位字或 Q 开头的那条。"""
    c = [it for it in items if STATUS_Y <= it["y"] <= 330 and PLACE_BOX.search(it["t"].strip()) and "历史搜索" not in it["t"]]
    return min(c, key=lambda i: (i["y"], i["x"])) if c else None


def keyboard_top(items):
    ks = [it["y"] for it in items if it["y"] > 600 and len(it["t"].strip()) == 1 and it["t"].strip().isascii() and it["t"].strip().isalpha()]
    return min(ks) if len(ks) >= 5 else None


def place_candidates(items, box, name):
    """搜索框下面、键盘上面的候选行，按匹配档和从上到下排序。返回 [(档, 条目)]。"""
    kt = keyboard_top(items) or INF
    bottom = box["y"] + box["h"]
    out = []
    for it in items:
        t = it["t"].strip()
        if it is box or it["y"] <= bottom + 4 or it["y"] >= kt - 8 or len(norm(t)) < 2 or PLACE_SKIP.search(t):
            continue
        rk = place_rank(name, t)
        if rk is not None:
            out.append((rk, it))
    out.sort(key=lambda p: (p[0], p[1]["y"], p[1]["x"]))
    return out


def place_cmd(args, after):
    macro_off("地点")
    name = " ".join(args).strip()
    if not name:
        print("用法：kp place 地名（先停在表单页或地点搜索页；英文名按屏上显示的写法）"); sys.exit(2)
    n, steps = [0], []

    def step(s):
        n[0] += 1; klog(place_step=s); return n[0]
    r = scan_first(after)
    step("读当前屏")
    box = place_box(r["items"])
    if not box:
        # 在表单页：「取还车地点」标题下面第一行就是地点，点它进搜索页
        lab = next((it for it in sorted(r["items"], key=lambda i: (i["y"], i["x"])) if it["y"] >= STATUS_Y and re.search(r"取还车地点|取车地点", it["t"])), None)
        step("打开地点页")
        fld = None
        if lab:
            below = [it for it in r["items"] if it["y"] > lab["y"] + lab["h"] // 2 + 10 and it["y"] < lab["y"] + 180
                     and not re.search(r"同地还车|异地还车|^[>›＞]$|^\W*$", it["t"].strip())]
            fld = min(below, key=lambda i: (i["y"], i["x"])) if below else None
        if not fld:
            macro_fail("place", n[0], "打开地点页", "既不在地点搜索页，也没在表单上找到「取还车地点」下面的地点行", r["items"])
        r = macro_tap(r["items"], fld["x"] + fld["w"] // 2, fld["y"] + fld["h"] // 2, after, fld["t"], thumb=r.get("thumb"))
        steps.append(f"点开地点行「{kpx.redact(fld['t'])}」")
        box = place_box(r["items"])
        if not box:
            macro_fail("place", n[0], "打开地点页", "点了地点行但没出现搜索框", r["items"])
    s = step("点搜索框")
    r = macro_tap(r["items"], box["x"] + box["w"] // 2, box["y"] + box["h"] // 2, after, box["t"], thumb=r.get("thumb"))
    box = place_box(r["items"]) or box
    if not PLACE_HOLDER.search(box["t"]):  # 框里已有旧字：全选删掉
        call("/act", {"ops": [{"op": "key", "key": "KeyA", "mods": ["MetaLeft"]}, {"op": "key", "key": "Backspace", "mods": []}]})
        steps.append("清掉搜索框旧字")
    step("输入地名")
    zh_type(name, True)
    steps.append(f"输入「{name}」")
    step("等候选")
    prev, cands, r = None, [], None
    for i in range(6):
        r = settle(0.4)
        scan_abort(r["items"], None)
        box = place_box(r["items"]) or box
        loading = any("正在加载" in it["t"] for it in r["items"])
        cands = [] if loading else place_candidates(r["items"], box, name)
        key = tuple(norm(c[1]["t"]) for c in cands[:4])
        if key and key == prev:
            break  # 连续两次读到同一批候选：列表已经刷新完
        prev = key or None
        time.sleep(0.7)
    if not cands:
        macro_fail("place", n[0], "等候选", f"等了几次仍没有与「{name}」沾边的候选（可能还在加载、搜不到，或写法与屏上不同）", r["items"])
    rk, pick = cands[0]
    step("点候选")
    r = macro_tap(r["items"], pick["x"] + pick["w"] // 2, pick["y"] + pick["h"] // 2, after, pick["t"], thumb=r.get("thumb"))
    steps.append(f"点候选「{kpx.redact(pick['t'])}」（匹配档 {rk}，共 {len(cands)} 条沾边）")
    step("回读核对")
    if place_box(r["items"]):
        macro_fail("place", n[0], "回读核对", "点完候选还停在搜索页，没选上", r["items"])
    seen = max(fuzzy_has(name, it["t"]) for it in r["items"] if it["y"] >= STATUS_Y) if r["items"] else 0
    seen2 = max(fuzzy_has(pick["t"], it["t"]) for it in r["items"] if it["y"] >= STATUS_Y) if r["items"] else 0
    if max(seen, seen2) < 0.72:
        macro_fail("place", n[0], "回读核对", f"已离开搜索页，但屏上没看到「{name}」或所点候选的字样，无法确认选对了", r["items"])
    steps.append("回读：屏上出现了地名")
    print(f"[place] 自动做了：" + "；".join(steps) + "。当前屏：")
    show(r["items"])
    diff_remember(r["items"])
    macro_end(r["items"])
    k1_save(diff_ts=round(time.time()))


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__); return
    global BASE, DEV, DEVCFG, LOG, FULL_OUT
    dev = os.environ.get("KP_DEV")
    if "--dev" in argv:
        i = argv.index("--dev")
        if i + 1 >= len(argv):
            print("用法：kp --dev 设备名 命令…（--dev 后面缺设备名；kp devs 看有哪些）"); sys.exit(2)
        dev = argv[i + 1]; del argv[i:i + 2]
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
    FULL_OUT = "--full" in argv  # K3：本条命令保留全量输出（动作后默认只显示变化的行）
    argv = [a for a in argv if a != "--full"]
    grep = None
    if "-g" in argv:
        i = argv.index("-g")
        if i + 1 >= len(argv):
            print("用法：kp 命令 -g 关键词（-g 后面缺关键词，例如 kp read -g 价格）"); sys.exit(2)
        grep = argv[i + 1]; del argv[i:i + 2]
    after = 900
    if "-w" in argv:
        i = argv.index("-w")
        if i + 1 >= len(argv) or not argv[i + 1].isdigit():
            print("用法：kp 命令 -w 毫秒数（-w 后面缺等待毫秒数，例如 -w 1500）"); sys.exit(2)
        after = int(argv[i + 1]); del argv[i:i + 2]
    if not argv:
        print(__doc__); sys.exit(2)
    cmd, args = argv[0], argv[1:]
    klog(cmd=cmd, args=args)
    K2["cmd"] = k2_cmd(cmd, args)
    kpx.init(DEV, owner())
    # K0：用户插话被转达时记一条 steer_ack（steer.seen 行数增加 = 本次转达了新插话；kpx.steer 本身不动）
    from pathlib import Path
    sn = lambda: int((Path(kpx.JOB) / "steer.seen").read_text() or 0) if kpx.JOB and (Path(kpx.JOB) / "steer.seen").exists() else 0
    try:
        n0 = sn()
    except Exception:
        n0 = 0
    try:
        kpx.steer(cmd in ACTS)
    finally:
        try:
            if sn() > n0:
                klog(steer_ack=sn() - n0)
        except Exception:
            pass

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
        force_social = "-f" in args  # 只放行闲鱼社交类按钮，付款护栏不看它
        if len(args) >= 2 and args[0].lstrip("-").isdigit() and args[1].lstrip("-").isdigit():
            import random
            x = int(args[0]) + round(max(-3, min(3, random.gauss(0, 1.5))))
            y = int(args[1]) + round(max(-3, min(3, random.gauss(0, 1.5))))
            # 等画面稳住再判：11-01 那笔误付就是连着三下坐标点击，第三下落在刚弹出来的「立即支付」上，护栏还拿着上一屏在判
            # K1：只有「刚读过、判稳、画面严格没变」才沿用那次读屏，并把屏指纹交给桥在按下前核对；结算 / 付款类一律完整重读
            r, expect = k1_read(x=x, y=y)
            target = guard(r["items"], x, y, coord=True, force=force_social)
            k2_repeat_guard(r["items"], x, y, True, force_social)
            k2_xianyu_hint(r["items"], x, y)

            def restart():
                r2 = call("/read?settle=200")
                k1_save(checkout=bool(on_checkout(r2["items"])))
                return [{"op": "tap", "x": x, "y": y}], guard(r2["items"], x, y, coord=True, force=force_social)
        else:
            nth = 0; exact = "-e" in args
            args = [a for a in args if a not in ("-e", "-f")]
            if "-n" in args:
                i = args.index("-n")
                if i + 1 >= len(args) or not args[i + 1].lstrip("-").isdigit():
                    print("用法：kp tap 文字 -n 第几个（-n 后面缺序号，从 0 开始）"); sys.exit(2)
                nth = int(args[i + 1]); del args[i:i + 2]
            if not args:
                print("用法：kp tap 文字 [-n 第几个] [-e]，或 kp tap X Y（缺要点的文字）"); sys.exit(2)
            t0 = time.time()
            r, expect = k1_read(text=args[0], nth=nth, exact=exact)  # K1：见上面坐标点击的说明
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
            guard(r["items"], x, y, force=force_social)
            k2_repeat_guard(r["items"], x, y, False, force_social)
            k2_xianyu_hint(r["items"], x, y)
            target = it["t"]
            print(f"点 {x},{y}「{kpx.redact(it['t'])}」")
            text_q, text_nth = args[0], nth

            def restart():
                r2 = call("/read?settle=200")
                k1_save(checkout=bool(on_checkout(r2["items"])))
                if forbidden(text_q, r2["items"]):
                    deny(text_q)
                it2 = find(r2["items"], text_q, text_nth, exact)
                if not it2:
                    print(f"没找到「{text_q}」，当前屏：")
                    show(r2["items"])
                    sys.exit(2)
                x2 = it2["x"] + it2["w"] // 2 + round(max(-it2["w"] / 4, min(it2["w"] / 4, random.gauss(0, 3))))
                y2 = it2["y"] + it2["h"] // 2 + round(max(-it2["h"] / 4, min(it2["h"] / 4, random.gauss(0, 2))))
                guard(r2["items"], x2, y2, force=force_social)
                print(f"重读后点 {x2},{y2}「{kpx.redact(it2['t'])}」")
                return [{"op": "tap", "x": x2, "y": y2}], it2["t"]
        return act([{"op": "tap", "x": x, "y": y}], after, quiet, grep, target, expect, restart, before=r["items"], track=True)
    if cmd in ("type", "paste") and DEVCFG.get("kind") == "wda":  # WDA 能直接打任意文字，不走剪贴板
        return act([{"op": "type", "text": " ".join(args)}], after, quiet, grep)
    if cmd == "open":
        if DEVCFG.get("kind") == "wda":
            return act([{"op": "launch", "app": " ".join(args)}], after, quiet, grep)
        return open_url(args, quiet, grep)
    if cmd in ("type", "paste"):  # 英文也走这里：先确认输入法在英文，不然字母会落进拼音候选（11-01 狂打字母）
        # 11-01 起不再走通用剪贴板：mini 没登录用户的 Apple ID，⌘V 粘出来的是用户 MacBook 剪贴板里的东西
        if cmd == "paste" and args and args[0] == "--fresh":
            return zh_type_fresh(" ".join(args[1:]), quiet, grep)
        return zh_type(" ".join(args), quiet, grep)
    if cmd == "key":
        ops = []
        for k in args:
            parts = k.split("+")
            mods = [{"Meta": "MetaLeft", "Ctrl": "ControlLeft", "Shift": "ShiftLeft", "Alt": "AltLeft"}.get(p, p) for p in parts[:-1]]
            ops.append({"op": "key", "key": parts[-1], "mods": mods})
        return act(ops, after, quiet, grep, track=True)
    if cmd in ("up", "down"):
        import random
        if args and not args[0].isdigit():
            print("用法：kp up|down [N]（N 是正整数）"); sys.exit(2)
        n = int(args[0]) if args else 1
        if n < 1:
            print("用法：kp up|down [N]（N 至少为 1）"); sys.exit(2)
        W, U = 500, random.uniform
        batches = []
        for k in range(0, n, SWIPE_BATCH):
            ops = []
            for _ in range(min(SWIPE_BATCH, n - k)):
                ops += swipe_ops(cmd, W, U)
            batches.append(ops)
        if len(batches) > 1:
            print(f"[{cmd}] 一共 {n} 次，每批最多 {SWIPE_BATCH} 次，拆成 {len(batches)} 批执行")
        for bi, ops in enumerate(batches):
            last = bi == len(batches) - 1
            # 中间批只滑不读屏，最后一批才按调用方的 quiet / -g 读屏打印
            r = act(ops, after, quiet if last else True, grep)
        return r
    if cmd == "scan":
        return scan_cmd(args, after)
    if cmd == "scroll-until":
        return scroll_until_cmd(args, after)
    if cmd == "date":
        return date_cmd(args, after)
    if cmd == "place":
        return place_cmd(args, after)
    if cmd == "drag":
        if len(args) not in (4, 5) or not all(re.fullmatch(r"\d+", a) for a in args):
            sys.exit("用法：kp drag X1 Y1 X2 Y2 [毫秒]")
        x1, y1, x2, y2 = map(int, args[:4]); ms = int(args[4]) if len(args) == 5 else 350
        return act([{"op": "drag", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "ms": ms, "steps": max(8, ms // 15)}], after, quiet, grep)
    if cmd == "back":
        import random
        y = random.uniform(470, 620)
        return act([{"op": "drag", "x1": 3, "y1": round(y), "x2": round(random.uniform(350, 410)), "y2": round(y + random.uniform(-25, 25)),
                     "ms": round(random.uniform(220, 280)), "steps": 18}], after, quiet, grep)
    if cmd == "price":
        print(price_of(read(show_=False)["items"])); return
    if cmd == "look":
        # 读屏 + JEV 判屏，一步拿到「这是什么屏、下一步干嘛、屏上有什么」
        r = read(grep, show_=False, remember=False)
        kpx.after(r["items"], acted=False)
        screen_hint(r["items"])
        show_screen(r["items"], grep, FULL_OUT); return
    if cmd == "app":
        return kpx.app_cmd(args, call("/read?cache=1")["items"])
    if cmd == "handoff":
        wait = 900
        if "--wait" in args:
            i = args.index("--wait")
            if i + 1 >= len(args) or not args[i + 1].isdigit():
                print("用法：kp handoff \"原因\" --wait 秒数（--wait 后面缺秒数）"); sys.exit(2)
            wait = int(args[i + 1]); del args[i:i + 2]
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
