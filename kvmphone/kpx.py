"""kpx —— kp 的附加能力（借鉴 Mika：github.com/wangzan101/Mika），三台手机（rmq1 / esp32 / ip7）共用。

  App 知识     knowledge/apps/<id>/APP.md（只写事实）+ 自学补充 ~/.config/kvmphone/learned/<id>.md；
               读屏认出当前 App，第一次进这个 App 时把说明附在输出里
  节奏         只对写了 pace: careful 的 App（风控严）放慢：每步之间按 Mika 的停留范围补足，一个任务换屏有上限；
               发送 / 打招呼 / 投递这类对外动作在同一 App 里连着做时隔开 8–30 秒。KP_PACE=off 关掉
  风控识别     滑块、安全验证、操作频繁、登录墙一类页面，提示别硬闯、转人工
  人工接管     kp handoff "原因"：截图推 decide（+ 飞书）等用户处理，按用户的回答返回不同退出码
  隐私打码     验证码、银行卡号、身份证号在输出和日志里打码
  插话 / 叫停  rmq say / rmq stop 写进任务目录，kp 每条命令开头转给驾驶模型
状态都在 /tmp/kvmphone/（桥所在那台机器）。
"""
import json, os, random, re, shutil, subprocess, sys, time, unicodedata
from pathlib import Path

DIR = Path(__file__).resolve().parent
KDIR = DIR / "knowledge" / "apps"
LDIR = Path(os.environ.get("KP_LEARNED") or Path.home() / ".config/kvmphone/learned")
RUN = Path("/tmp/kvmphone")
HDIR = RUN / "handoff"
DEV, OWNER = "rmq1", "manual"
JOB = os.environ.get("KP_JOB_DIR")  # rmq 任务目录；直控时为空，插话 / 叫停 / 接管计时都不涉及
PACE_OFF = os.environ.get("KP_PACE") == "off"

# Mika 的节奏参数（AppPacing.CAREFUL / OutwardPacing），单位秒
SAME = (0.9, 2.2)          # 同一屏上接着做
NEW = (2.5, 5.0)           # 换到新的一屏，另加读字时间
READ_PER_CHAR, READ_MAX = 0.015, 8.0
PAUSE_P, PAUSE = 0.1, (2.0, 5.0)
DWELL_MAX = 15.0
SCREEN_CAP = 24            # 慎用 App 一个任务里换到新屏的次数
OUT_FIRST, OUT_STEP, OUT_MAX, OUT_WIN = (8.0, 20.0), (0.5, 1.0), 30.0, 1800

OUTWARD = re.compile(r"发送|发\s*送|^send$|打招呼|立即沟通|继续沟通|投递|投简历|发布|发表|评论|回复|加好友|添加到通讯录"
                     r"|^提交$|^关注$|^follow$|^post$|^reply$|^share$|^分享$", re.I)
RISK = re.compile(r"滑块|拖动.{0,6}(滑块|拼图|完成)|向右滑动|安全验证|人机验证|请完成.{0,4}验证|身份验证|图形验证"
                  r"|操作(过于|太)?频繁|访问(过于|太)?频繁|请求(过于|太)?频繁|稍后再试|账号(存在)?异常|环境(存在)?异常|异常(访问|操作|行为)"
                  r"|verify you are human|unusual traffic|too many requests|try again later|security check|captcha", re.I)
LOGIN = re.compile(r"一键登录|本机号码|密码登录|验证码登录|获取验证码|手机号登录|登录/注册|请先登录|立即登录|同意.{0,10}(协议|条款)并登录"
                   r"|^log ?in$|^sign in$|sign in with apple|continue with apple", re.I)


def norm(s):
    return unicodedata.normalize("NFKC", s).replace(" ", "").lower()


def init(dev, owner):
    global DEV, OWNER
    DEV, OWNER = dev, owner


def say(msg):
    print(msg, flush=True)


# ───────────── 隐私打码（Mika Redactor）─────────────
CODE_WORD = re.compile(r"验证|校验码|动态码|动态密码|确认码|安全码|短信|口令|\b(code|otp|passcode|pin|2fa)\b", re.I)
CODE_DIGITS = re.compile(r"(?<![\d.¥$￥])\d(?:[ \-]?\d){3,7}(?!\d)(?!\.\d)"
                         r"(?!\s*(?:年|元|块|分钟|小时|秒|天|月|日|号|次|个|人|件|条|米|公里|克|斤|%|℃|[¥$￥]))")
CARD = re.compile(r"(?<![\d.])\d(?:[ \-]?\d){15,18}(?![\d.])")
CARD_WORD = re.compile(r"卡|银行|储蓄|信用|银联|card|visa|master", re.I)
IDNO = re.compile(r"(?<![\dA-Za-z])(\d{6})\d{8}(\d{3}[\dXx])(?![\dA-Za-z])")


def redact(s):
    if not s:
        return s
    s = IDNO.sub(lambda m: m.group(1) + "********" + m.group(2), s)
    if CARD_WORD.search(s):  # 订单号也常是 16–19 位，只在提到卡的行里打码
        s = CARD.sub(lambda m: "**** 尾号 " + re.sub(r"\D", "", m.group())[-4:], s)
    if CODE_WORD.search(s):
        s = CODE_DIGITS.sub("******", s)
    return s


# ───────────── App 知识 ─────────────
def _front(text):
    meta, body = {}, text
    if text.startswith("---"):
        head, _, body = text[3:].partition("\n---")
        for line in head.splitlines():
            k, _, v = line.partition(":")
            if not k.strip() or line.lstrip().startswith("#"):
                continue
            v = v.split("  #")[0].strip()
            try:
                meta[k.strip()] = json.loads(v) if v[:1] in '["{' else v
            except ValueError:
                meta[k.strip()] = v
    return meta, body.strip()


def apps():
    out = {}
    if KDIR.exists():
        for p in sorted(KDIR.glob("*/APP.md")):
            meta, body = _front(p.read_text())
            meta.setdefault("name", p.parent.name)
            meta["body"] = body
            out[meta["name"]] = meta
    return out


def find_app(name, known=None):
    known = known or apps()
    k = norm(name)
    for a in known.values():
        if k in [norm(x) for x in [a["name"], a.get("display", "")] + list(a.get("aliases", []))]:
            return a
    return None


SYSPAGE = {norm(x) for x in ["Siri Suggestions", "Siri 建议", "App Library", "App 资源库"]}
HOME = {norm(x) for x in ["Calendar", "Photos", "Camera", "Clock", "Messages", "Safari", "App Store", "Settings", "Maps", "Mail",
                          "Notes", "Weather", "Phone", "Music", "Wallet", "Reminders", "Files", "Health", "FaceTime", "Find My",
                          "日历", "照片", "相机", "时钟", "信息", "设置", "地图", "邮件", "备忘录", "天气", "电话", "音乐", "钱包", "文件"]}


def detect(items, known=None):
    """按读屏文字认 App：品牌词（brand）一个 3 分，特征词（detect）一个 1 分，3 分起算，取最高。iOS 拿不到包名，只能这么认。"""
    known = known or apps()
    texts = [norm(it["t"]) for it in items]
    names = HOME | {norm(x) for a in known.values() for x in [a.get("display", "")] + list(a.get("aliases", [])) if x}
    if "ios" in known and (sum(1 for t in texts if t in names) >= 3 or any(t in SYSPAGE for t in texts)):
        return known["ios"]  # 主屏 / 搜索页 / App 资源库：满屏 App 名字，别当成进了那个 App
    best, score, branded = None, 0, 0
    for a in known.values():
        b = sum(1 for w in a.get("brand", []) if any(norm(w) in t for t in texts))
        branded += b > 0
        s = 3 * b + sum(1 for w in a.get("detect", []) if any(norm(w) == t or (len(norm(w)) >= 3 and norm(w) in t) for t in texts))
        if s > score:
            best, score = a, s
    if branded >= 3 and "ios" in known:  # 三个以上 App 的品牌词同屏，多半是系统列表页
        return known["ios"]
    return best if score >= 3 else None


def learned(name):
    p = LDIR / f"{name}.md"
    return p.read_text().strip() if p.exists() else ""


def describe(a, full=True):
    tag = "（慎用：限制操作快慢，kp 会自动放慢）" if a.get("pace") == "careful" else ""
    lines = [f"[App] {a.get('display', a['name'])}{tag}"]
    if a.get("open"):
        lines.append(f"打开方式：{a['open']}")
    if full:
        lines.append(a["body"])
        if (l := learned(a["name"])):
            lines.append("## 以前任务里记下的\n" + "\n".join(l.splitlines()[-30:]))
    return "\n".join(lines)


def learn(fact):
    """把「App：事实」记进自学补充（只记事实，同一条不重复，每个 App 最多留 40 条）。返回记到哪个 App。"""
    parts = re.split(r"[：:]", fact, 1)
    if len(parts) < 2 or not parts[1].strip():
        return None
    a, f = find_app(parts[0].strip()), parts[1]
    if not a:
        return None
    LDIR.mkdir(parents=True, exist_ok=True)
    p = LDIR / f"{a['name']}.md"
    old = p.read_text().splitlines() if p.exists() else []
    f = redact(f.strip()).replace("\n", " ")
    if any(norm(f) == norm(re.sub(r"^- (\d\d-\d\d )?", "", o)) for o in old):
        return a["name"]
    old.append(f"- {time.strftime('%m-%d')} {f}")
    p.write_text("\n".join(old[-40:]) + "\n")
    return a["name"]


# ───────────── 节奏状态（按设备，调用方换了就重置）─────────────
def _pf():
    return RUN / f"{DEV}.pace.json"


def load():
    try:
        st = json.loads(_pf().read_text())
    except Exception:
        st = {}
    if st.get("owner") != OWNER:
        st = {"owner": OWNER, "since": time.time(), "screens": {}, "outward": {}, "outdone": [], "seen": []}
    return st


def save(st):
    RUN.mkdir(parents=True, exist_ok=True)
    tmp = _pf().with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False))
    tmp.replace(_pf())


def _sig(items):
    return sorted({it["t"] for it in items[2:]})[:80]  # 去掉状态栏


def _new_screen(a, b):
    a, b = set(a), set(b)
    return len(a & b) < 0.5 * max(len(a | b), 1)


def is_outward(text):
    t = norm(text)
    return bool(t) and len(t) <= 8 and bool(OUTWARD.search(t))


def before(items, target=None):
    """动作之前：在慎用 App 里补足停留；对外动作和上一次隔开。模型思考、读屏的时间都算在停留里，只补不够的部分。
    items 可以是取读屏结果的函数：只有要算读字时间时才去读（普通 App 不多花一次读屏）。"""
    if PACE_OFF:
        return
    st = load()
    app = st.get("app")
    known = apps()
    careful = app and known.get(app, {}).get("pace") == "careful"
    wait, why = 0.0, ""
    if careful and st.get("last"):
        if st.get("last_new"):
            items = items() if callable(items) else items
            need = random.uniform(*NEW) + min(READ_MAX, READ_PER_CHAR * sum(len(it["t"]) for it in items))
        else:
            need = random.uniform(*SAME)
        if random.random() < PAUSE_P:
            need += random.uniform(*PAUSE)
        wait = min(DWELL_MAX, need) - (time.time() - st["last"])
        why = f"{known[app].get('display', app)} 是慎用 App"
    if target and is_outward(target):
        key = app or "?"
        ts = [t for t in st["outward"].get(key, []) if time.time() - t < OUT_WIN]
        if ts:
            lo, hi = OUT_FIRST
            n = len(ts) - 1
            gap = min(OUT_MAX, random.uniform(lo + n * OUT_STEP[0], hi + n * OUT_STEP[1]))
            w2 = gap - (time.time() - ts[-1])
            if w2 > wait:
                wait, why = w2, f"「{target}」是对外动作，和上一次隔开"
    if wait > 0.3:
        say(f"[节奏] {why}，先停 {wait:.1f} 秒")
        time.sleep(wait)


def after(items, acted=True, target=None):
    """动作之后（或 look）：认 App、第一次进入附说明、风控 / 登录墙提示、慎用 App 换屏计数、记下对外动作。
    items 为 None（动作带 -q 没读屏）时只记时间和对外动作。"""
    st = load()
    known = apps()
    a = detect(items, known) if items else None
    sig = _sig(items) if items else st.get("sig", [])
    new = _new_screen(st.get("sig", []), sig) if items else False
    if a:
        if a["name"] not in st["seen"]:
            st["seen"].append(a["name"])
            say(describe(a))
        elif a["name"] != st.get("app"):
            say(describe(a, full=False) + f"（说明见 kp app {a['name']}）")
        st["app"] = a["name"]
    app = st.get("app")
    if acted:
        if target and is_outward(target):
            key = app or "?"
            st["outward"].setdefault(key, []).append(time.time())
            if key not in st["outdone"]:
                st["outdone"].append(key)
        if new and app and known.get(app, {}).get("pace") == "careful" and app not in st["outdone"]:
            n = st["screens"][app] = st["screens"].get(app, 0) + 1
            if n == SCREEN_CAP:
                say(f"[节奏] 这个任务在 {known[app].get('display', app)} 里已经换了 {n} 屏，再多容易被风控：能收尾就收尾，"
                    f"确实要接着看就少翻页、多用搜索直达")
        st["last"], st["last_new"] = time.time(), new
    st["sig"] = sig
    save(st)
    if not items:
        return
    hits = [it["t"] for it in items if RISK.search(it["t"])]
    if hits:
        say(f"[风控] 屏上有「{hits[0]}」：像是验证 / 限流页，别反复刷新或硬点；要用户出手就 kp handoff \"{hits[0]}\"")
    logins = [it["t"] for it in items if LOGIN.search(it["t"].strip())]
    if len(logins) >= 2 or any(re.search(r"一键登录|请先登录|本机号码", t) for t in logins):
        say(f"[登录] 这是登录页（{'、'.join(logins[:3])}）：登录交给用户，kp handoff \"要登录\"")


def app_cmd(args, items):
    known = apps()
    if not args:
        a = detect(items, known)
        if not a:
            say("认不出当前是哪个 App；kp apps 列出有说明的 App，kp app <名字> 直接看"); return
    else:
        a = find_app(" ".join(args), known)
        if not a:
            sys.exit(f"没有「{' '.join(args)}」的说明；kp apps 看有哪些")
    say(describe(a))


def apps_cmd():
    for a in apps().values():
        n = len(learned(a["name"]).splitlines())
        say(f"{a['name']:<14} {a.get('display', ''):<8} {'慎用' if a.get('pace') == 'careful' else '    '} "
            f"{a.get('description', '')}{f'（自学 {n} 条）' if n else ''}")


# ───────────── 插话 / 叫停 ─────────────
def steer(acting):
    """rmq say 写的插话转给驾驶模型；rmq stop 之后动作一律拒绝（退出码 130）。"""
    if not JOB:
        return
    j = Path(JOB)
    sp, seen = j / "steer.txt", j / "steer.seen"
    if sp.exists():
        lines = sp.read_text().splitlines()
        n = int(seen.read_text() or 0) if seen.exists() else 0
        for line in lines[n:]:
            say(f"[用户插话] {line}（照这句调整，和原任务冲突时以这句为准）")
        seen.write_text(str(len(lines)))
    if (j / "stop").exists() and acting:
        why = (j / "stop").read_text().strip()
        say(f"[用户叫停] {why or '用户让停下'}：不要再操作手机，把已经拿到的写进 result.json 后结束。")
        sys.exit(130)


# ───────────── 人工接管（Mika Takeover）─────────────
ANSWERS = [("处理好了", "我已经在手机上弄完了，你先核实再接着做", 0),
           ("我自己做完了", "这件事我办完了，你只核实结果然后结束", 10),
           ("跳过", "我不处理，你按办不成往下走", 11),
           ("取消任务", "停掉这个任务", 12)]
CODES = {a: c for a, _, c in ANSWERS} | {"写了意见": 13}  # 用户没点选项、直接写了话
EXPLAIN = {0: "用户说处理好了：先 kp look 核实画面，确认后接着做原任务",
           10: "用户说自己做完了：只 kp look 核实结果，写 result.json 后结束，不要再操作",
           11: "用户选了跳过：这一步按办不成处理，能绕开就绕开，绕不开就在结果的「没做到」写明",
           12: "用户取消了任务：不要再操作手机，写 result.json 后结束",
           13: "用户写了一句话（见上面的 [用户留言]）：照这句话调整做法，先 kp look 核实画面再接着做",
           124: "等了很久用户没来：这一步按办不成处理，在结果的「没做到」写明需要用户出手的原因"}


def push(req, img=None, timeout=900):
    """推一张接管卡到 decide 并等答案，返回选项文字；超时返回 None 并撤卡。本机要有 decide。"""
    title = f"{req.get('devname') or req['dev']} 需要你接手：{req['reason']}"[:80]
    cmd = ["decide", "q", "-p", "rmq1", "--pname", "RMQ1 手机任务", "--priority", "5",
           "--sub", f"在手机上处理完再点「处理好了」；{int(timeout / 60)} 分钟没人点就按办不成往下走",
           "--fact", f"调用方={req.get('owner', '')}", "--fact", f"任务={req.get('job') or '直控'}",
           title, *[f"{a}::{d}" for a, d, _ in ANSWERS], "--rec", "1"]
    if img and Path(img).exists():
        cmd[2:2] = ["--svg", str(img)]  # 截图当示意图贴在卡上
    p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    cid = (p.stdout.strip().splitlines() or [""])[0].strip()
    if p.returncode or not re.fullmatch(r"[0-9a-f]{6,}", cid):
        raise RuntimeError("decide 推卡失败：" + (p.stderr or p.stdout)[-300:])
    w = subprocess.run(["decide", "wait", cid, "--json", "--grace", "3", "--timeout", str(int(timeout))],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout + 120)
    try:
        got = json.loads(w.stdout)[0]
        lab = got["labels"][0]
        if (got.get("note") or "").strip():  # 卡上写的意见要原样转给驾驶模型（11-01 前被丢掉）
            lab = f"{lab}｜{got['note'].strip()}"
    except Exception:
        lab = None
    if lab is None:
        subprocess.run(["decide", "cancel", cid], stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    return lab


def handoff(reason, frame, renew, wait=900):
    """登录、验证码、滑块、要用户亲手点的地方交给用户：截图推卡，等用户在手机上处理完回话。
    本机有 decide 就直接推；没有（mini 上的 worker）就写请求文件，由本机 rmq relay 代推 decide 和飞书。"""
    RUN.mkdir(parents=True, exist_ok=True)
    try:
        img = frame()
    except Exception:
        img = None
    req = {"id": f"{DEV}-{time.strftime('%H%M%S')}-{random.randint(100, 999)}", "dev": DEV, "devname": os.environ.get("KP_DEVNAME", ""),
           "owner": OWNER, "job": os.environ.get("KP_JOB_ID", ""), "notify": os.environ.get("KP_NOTIFY", ""),
           "reason": reason, "img": img, "ts": time.time(), "wait": wait}
    t0 = time.time()
    if JOB:
        (Path(JOB) / "handoff.wait").write_text(str(t0))
    left = f"{wait // 60} 分钟" if wait >= 120 else f"{wait} 秒"
    say(f"[接管] 已请用户接手：{reason}（最多等 {left}，等的时间不算任务超时）")
    ans = None
    try:
        if shutil.which("decide") and not os.environ.get("KP_HANDOFF_FILE"):
            renew(wait + 300)
            ans = push(req, img, wait)
        else:
            HDIR.mkdir(parents=True, exist_ok=True)
            (HDIR / f"{req['id']}.json").write_text(json.dumps(req, ensure_ascii=False))
            af = HDIR / f"{req['id']}.answer"
            while time.time() - t0 < wait:
                renew(300)
                if af.exists():
                    ans = json.loads(af.read_text()).get("answer"); break
                if JOB and (Path(JOB) / "stop").exists():
                    ans = "取消任务"; break
                time.sleep(2)
            else:
                (HDIR / f"{req['id']}.expired").write_text("1")
    finally:
        dt = time.time() - t0
        if JOB:
            (Path(JOB) / "handoff.wait").unlink(missing_ok=True)
            with open(Path(JOB) / "handoff.jsonl", "a") as f:
                f.write(json.dumps({"reason": reason, "answer": ans, "waited": round(dt)}, ensure_ascii=False) + "\n")  # ans 带「｜意见」
    ans, _, note = (ans or "").partition("｜")
    ans = ans or None
    if note:
        say(f"[用户留言] {note}")
    code = CODES.get(ans, 124)
    if code == 12 and JOB:
        (Path(JOB) / "stop").write_text("用户在接管卡上取消了任务")
    load_st = load(); load_st["last"] = time.time(); load_st["last_new"] = True; save(load_st)
    say(f"[接管] {ans or '超时'}，等了 {int(dt)} 秒。{EXPLAIN[code]}")
    return code
