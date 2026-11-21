#!/usr/bin/env python3
"""rmq · 手机任务队列（每台手机一条队列，不同手机并行）

  rmq ask "任务" [--dev rmq1|esp32] [-t 标题] [-p decide项目] [--notify feishu:ou_xxx|none]   排队，打印任务 id
          [--selftest-precheck-fail]   自检开关（默认关，只对这一个任务生效）：第一次预检读屏直接按失败处理，
                          用来真走一遍「预检失败 → 自动重启手机桥 → 重新预检」；任务文字要以「【自检】」开头，
                          只能在本机命令行带，OpenClaw 机器人、Studio 网关这类外部调用带上会被拒绝
  rmq ls [-n 20]          最近的任务
  rmq show <id>           状态与结论
  rmq cancel <id>         取消排队中的任务
  rmq progress <id>       看任务跑到哪了：已跑几分几步、现在在哪个 App 哪个屏、最近 3 条动作（relay 在 5 / 15 分钟各发一条同内容心跳）
  rmq say <id> "话"       给跑着的任务插一句（驾驶模型下一步看到「[用户插话]」）
  rmq stop <id> [原因]    叫停跑着的任务：手机不再动，已拿到的照常交付（排队中的等于 cancel）
  rmq handoffs            （mini）列出等本机 relay 代推的接管请求；relay 用 rmq handoff <id> take|answer 回写
                          驾驶中 kp handoff 交给用户的环节，等用户的时间不算进 max_runtime；做完的任务带小结（用时 / 步数 / token）
  rmq worker [--once]     常驻执行（launchd 拉起）；--once 只跑一条就退
                          设备见 kvmphone/devices.json（kp devs）；不写 --dev 用配置 default_dev（默认 rmq1）。
                          开跑前先拿那台手机的 kp 租约，Claude 直控 / OpenClaw 正占着就排队等，不抢。
                          GLKVM 手机预检读屏失败时自动重启手机桥再预检一次（每个任务最多一次；证书类失败不重启），
                          重启前的现场存进任务目录 bridge-before-restart.log。
  rmq doctor              自检：kp、DeepSeek key、claude、decide、openclaw；列出结果没送到 / 待送达的任务和改版前的历史未送达
  rmq relay               （本机）定时从远端 worker 取做完的任务，推 decide 与飞书。每单一条发送线程，一单卡住不拖别的单和心跳；
                          单次发送最多等 60 秒，没送到隔 30 秒、2 分钟、5 分钟自动再试（共 4 次），用完推一张「飞书没送达」decide 卡
                          结果送达状态（rmq show 的 delivery，rmq ls 行尾标出）：pending 待送达 / delivered 已送达（飞书确认收到）/
                          not_needed 不需要发（--notify none 等）/ failed 没送到（自动重试用完，等人补发）
  rmq redeliver <id…> | --all [--force]   （本机）把没送到的结果重新往飞书发一次（不推 decide 卡）；已送达、不需要发的不重发，
                          加 --force 才发；--all 不含 11-12 改版前的历史未送达，那些要逐个写任务号
  rmq feishu-selftest [话]  （本机）往平时的收件人发一条「【自检】飞书送达测试」，报飞书确认没有、用时多少
  rmq sync [--dry-run] [路径…]  （本机）把和 mini 不一致的源码推过去（先留 .bak 备份；mini 有任务在跑就拒绝；不重启 worker）；
                          给了路径（相对 KVM 目录，如 rmq/rmq.py）就只推这几个

两台机分工：配置里写 "remote": "macmini" 的机器，ask / ls / show / cancel 都经 ssh 转到远端；
远端配置 "deliver": "relay"，做完只标记，由本机 relay 负责交付（远端不需要 decide 和 openclaw）。

驾驶：默认看配置 driver（mini 现为 ds）；--driver cc 是 claude -p 外壳 + 真 Claude Sonnet 5.5（cc_model），cc-sonnet / cc-opus / cc-haiku 指定 5.5 型号（11-18 加）：在本机 Mac 上跑（用 Mac 的订阅登录），kp 经 ssh 回 mini；
      Mac 没接单 / 未登录自动改用 DeepSeek；
      --driver ds 是 DeepSeek V4.1 Flash（claude -p 指向官方 Anthropic 兼容端点）；--driver glm 是 GLM 5.3 Flash（智谱 Anthropic 兼容端点，11-18 加）。claude -p 驾驶只放行 kp 等少数命令。
复核：Opus 5.5 只读 result.json / kp.jsonl / 日志尾，不合格带意见重跑一次。
交付：decide 卡 + 飞书消息。配置 ~/.rmq/config.json，密钥只从文件读，不进脚本。
"""
import datetime, fcntl, hashlib, json, os, re, shlex, shutil, signal, subprocess, sys, threading, time, uuid
from pathlib import Path

HOME = Path(os.environ.get("RMQ_HOME", Path.home() / ".rmq"))
JOBS = HOME / "jobs"
HERE = Path(__file__).resolve().parent
DEVF = HERE.parent / "kvmphone" / "devices.json"
sys.path.insert(0, str(HERE.parent / "kvmphone"))
import kpx  # App 知识自学、接管请求目录与推卡（三台手机共用）
kpx_acts = {"tap", "type", "paste", "key", "up", "down", "back", "open"}
DEFAULT = {
    "kp": "kp", "claude": "claude", "decide": "decide",
    "openclaw": str(Path.home() / ".openclaw/tmp/agent-cli/openclaw"),
    "ds_key_file": str(Path.home() / ".config/deepseek/subagent_api_key"),
    "ds_base": "https://api.deepseek.com/anthropic", "ds_model": "deepseek-flash", "ds_nothink": True, "review_nothink": True,
    "review_model": "claude-opus-5-5", "cc_model": "claude-sonnet-5-5", "cc_auth_ttl": 600,
    "decide_project": "rmq1", "decide_pname": "RMQ1 手机任务",
    "notify": "", "max_runtime": 1500, "max_turns": 300, "attempts": 2, "default_dev": "rmq1",
}


def cfg():
    p = HOME / "config.json"
    c = dict(DEFAULT)
    if p.exists():
        c.update(json.loads(p.read_text()))
    return c


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def jload(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def jsave(p, obj):
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    tmp.replace(p)


def log(job, msg):
    line = f"{now()} {msg}"
    with open(JOBS / job["id"] / "events.log", "a") as f:
        f.write(line + "\n")
    print(f"[{job['id']}] {msg}", flush=True)


def update(job, **kw):
    job.update(kw)
    jsave(JOBS / job["id"] / "job.json", job)


# ───────────── 入队与查看 ─────────────
SELFTEST_FLAG = "--selftest-precheck-fail"


def selftest_caller_check():
    """自检开关只给本机命令行用（11-12）：环境里带 OpenClaw 机器人或 Studio 网关标记的调用一律拒绝。
    本机发的 ask 经 ssh 转到 mini，所以本机转发前查一次，mini 收到时再查一次。"""
    ext = [k for k in os.environ if k.startswith("OPENCLAW_")] + [k for k in ("XY305_NO_LOGIN",) if k in os.environ]
    if os.environ.get("KP_OWNER") == "openclaw":
        ext.append("KP_OWNER=openclaw")
    if ext:
        sys.exit(f"rmq ask：{SELFTEST_FLAG} 只能在本机命令行用，外部调用不能带（环境里有 {ext[0]}）")


def ask(args):
    c = cfg()
    title, project, notify, task, dev, driver, selftest = None, c["decide_project"], c["notify"], [], c["default_dev"], "", False
    it = iter(args)
    for a in it:
        if a in ("-t", "--title"): title = next(it)
        elif a == "--dev": dev = next(it)
        elif a in ("-p", "--project"): project = next(it)
        elif a == "--notify": notify = next(it)
        elif a == "--driver": driver = next(it)  # 换驾驶模型：gemini-* 走 agy；cc / cc-sonnet / cc-haiku / cc-opus 走真 Claude 5.5（claude -p，本机订阅）；ds / deepseek 走 DeepSeek Flash
        elif a == SELFTEST_FLAG: selftest = True
        elif a in ("-h", "--help") or (a.startswith("-") and not task):  # 11-01 `rmq ask --help` 被当成任务排上了手机
            sys.exit(f"用法：rmq ask [-t 标题] [--dev 设备] [-p 项目] [--notify 收件人|none] [--driver 模型] [{SELFTEST_FLAG}] 任务内容")
        else: task.append(a)
    task = " ".join(task).strip()
    if not task:
        sys.exit("rmq ask：缺任务内容")
    if selftest:
        selftest_caller_check()
        if not task.startswith("【自检】"):
            sys.exit(f"rmq ask：带 {SELFTEST_FLAG} 的任务文字要以「【自检】」开头")
    if DEVF.exists():
        d = json.load(open(DEVF))["devices"].get(dev)
        if not d or d.get("enabled") is False:
            sys.exit(f"rmq ask：设备 {dev} {'不在设备表里' if not d else '还没接好'}")
    jid = time.strftime("%y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    d = JOBS / jid
    ahead = sum(1 for j in jobs() if j["status"] in ("queued", "running", "reviewing") and j.get("dev", "rmq1") == dev)   # 建目录前数，不含自己
    d.mkdir(parents=True)
    job = {"id": jid, **({"queued_ahead": ahead} if ahead else {}), "task": task, "title": title or task[:30], "project": project, "dev": dev,
           "notify": "" if notify == "none" else notify, **({"quiet": True} if notify == "none" else {}), "status": "queued", "created": now(), **({"driver": driver} if driver else {}),
           **({"selftest": "precheck-fail"} if selftest else {})}
    jsave(d / "job.json", job)
    print(jid)   # queued_ahead 标记：排队中的任务轮到时 relay 发一条「开始了」
    # 11-04：排位写进 stdout，飞书机器人照这行回复；之前写死「已排队」，前面没任务也说排队
    # 11-09：时间区间按近 30 个同驾驶做完任务的用时中位数算，偏保守，不再写死「10–25 分钟」
    lo, hi, basis = eta_range(driver, dev)
    if ahead:
        wl, wh = ahead * lo, ahead * hi
        print(f"排队中：{dev} 前面还有 {ahead} 个任务，预计约 {wl}–{wh} 分钟后开始，开始后约 {lo}–{hi} 分钟完成（{basis}，仅供参考，轮到时会通知）。")
    else:
        print(f"马上开始：{dev} 前面没有任务，几秒内开跑（手机若正被直控占着，会等它放手）；约 {lo}–{hi} 分钟出结论（{basis}，仅供参考）。")


def job_secs(j):
    """任务从开跑到结束的秒数（等用户接管的时间已在 handoff.jsonl，不扣，历史样本只做粗估）。"""
    try:
        t = lambda k: time.mktime(time.strptime(j[k], "%Y-%m-%d %H:%M:%S"))
        return int(t("finished") - t("started"))
    except Exception:
        return 0


def eta_range(driver, dev):
    """预计完成用时区间（分钟）：近 30 个同驾驶 done 任务用时的中位数，下限取 0.8 倍、上限取 1.8 倍（偏保守，同类任务常撞限）。
    同驾驶样本不足 5 个就用全部 done 任务；一个样本都没有按保守默认。"""
    done = [j for j in jobs() if j["status"] == "done" and job_secs(j) > 60]
    same = [j for j in done if (j.get("driver") or "") == (driver or "")][-30:]
    pool, basis = (same, "近 %d 个同驾驶任务") if len(same) >= 5 else (done[-30:], "近 %d 个任务")
    if not pool:
        return 10, 25, "暂无历史样本，按保守估计"
    xs = sorted(job_secs(j) for j in pool)
    med = xs[len(xs) // 2] / 60
    return max(3, int(med * 0.8)), max(6, int(med * 1.8 + 0.99)), (basis % len(pool)) + f"用时中位约 {int(med + 0.5)} 分钟"


def jobs():
    out = []
    if JOBS.exists():
        for d in sorted(JOBS.iterdir()):
            j = jload(d / "job.json")
            if j: out.append(j)
    return out


def ls(args):
    n = int(args[1]) if len(args) > 1 and args[0] == "-n" else 20
    c = cfg()
    for j in jobs()[-n:]:
        ans = (j.get("answer") or "")[:60]
        dl = {"pending": "  〔结果待送达〕", "failed": "  〔结果没送到〕", "not_needed": "  〔不需要发〕"}.get(j.get("delivery"), "") if j["status"] in FINISHED else ""
        print(f"{j['id']}  {j.get('dev', 'rmq1'):<5} {j['status']:<9} {driver_label(j, c):<22} {j['title'][:24]:<24} {ans}{dl}")


def show(args):
    j = jload(JOBS / args[0] / "job.json")
    if not j: sys.exit("没有这个任务")
    print(json.dumps(j, ensure_ascii=False, indent=1))
    print(f"驾驶模型：{driver_label(j, cfg())}")
    ev = JOBS / args[0] / "events.log"
    if ev.exists(): print(ev.read_text()[-1500:])


def cancel(args):
    j = jload(JOBS / args[0] / "job.json")
    if not j: sys.exit("没有这个任务")
    if j["status"] in ("running", "reviewing"): sys.exit("任务正在跑，用 rmq stop <id> 叫停")
    if j["status"] != "queued": sys.exit(f"任务状态是 {j['status']}，只能取消排队中的")
    j["status"] = "cancelled"; jsave(JOBS / args[0] / "job.json", j); print("已取消")


def say_(args):
    """跑着的任务插一句话：驾驶模型下一条 kp 命令开头就会看到「[用户插话] …」。"""
    j = jload(JOBS / args[0] / "job.json")
    if not j: sys.exit("没有这个任务")
    if j["status"] not in ("queued", "running", "reviewing"): sys.exit(f"任务已经 {j['status']}，插不了话")
    with open(JOBS / args[0] / "steer.txt", "a") as f:
        f.write(" ".join(args[1:]).replace("\n", " ") + "\n")
    print("已转给驾驶模型（它下一步操作时看到）")


def stop(args):
    """叫停正在跑的任务：kp 拒绝后续动作，驾驶模型写完结果就结束；90 秒没结束直接杀。"""
    j = jload(JOBS / args[0] / "job.json")
    if not j: sys.exit("没有这个任务")
    if j["status"] == "queued":
        return cancel(args)
    if j["status"] not in ("running", "reviewing"): sys.exit(f"任务已经 {j['status']}")
    (JOBS / args[0] / "stop").write_text(" ".join(args[1:]) or "用户叫停")
    print("已叫停：驾驶模型会把已经拿到的写进结果后结束（最多 90 秒）")


def handoffs(_):
    """mini 上 kp handoff 写的请求，等本机 relay 来取（没被取走、没答、没过期的）。"""
    out = []
    for p in sorted(kpx.HDIR.glob("*.json")) if kpx.HDIR.exists() else []:
        base = p.with_suffix("")
        if not any(Path(f"{base}.{x}").exists() for x in ("answer", "expired", "taken")):
            out.append(json.loads(p.read_text()))
    print(json.dumps(out, ensure_ascii=False))


def handoff_mark(args):
    """relay 取走（take）或回答（answer <选项>）一条接管请求。"""
    hid, what = args[0], args[1]
    if not re.fullmatch(r"[\w.-]+", hid): sys.exit("id 不对")
    if what == "take":
        (kpx.HDIR / f"{hid}.taken").write_text(now())
    else:
        (kpx.HDIR / f"{hid}.answer").write_text(json.dumps({"answer": " ".join(args[2:])}, ensure_ascii=False))


# ───────────── 进度可见 ─────────────
def progress_info(job):
    """读任务各 attempt 的 kp.jsonl，整理出进度：已跑分钟、手机动作步数、现在的 App 与 JEV 判屏、最近 3 条动作、距上次动作秒数。纯读，不碰手机。"""
    d = JOBS / job["id"]
    steps, app, scr, scr_rough, recent, last_ts = 0, "", "", False, [], 0.0
    for a in sorted(d.glob("attempt*"), key=lambda x: int(re.sub(r"\D", "", x.name) or 0)):
        f = a / "kp.jsonl"
        if not f.exists(): continue
        for line in f.read_text(errors="ignore").splitlines():
            try:
                o = json.loads(line)
            except Exception:
                continue
            cmd = o.get("cmd")
            if cmd in kpx_acts:
                steps += 1; last_ts = max(last_ts, o.get("ts") or 0)
                txt = " ".join(map(str, o.get("args") or []))
                recent.append("kp " + cmd + (f"（{len(txt)} 字，内容不显示）" if cmd in ("type", "paste") else (" " + txt[:30] if txt else "")))   # 输入的文字可能是消息正文，不进进度
                if cmd == "open" and o.get("args"): app = str(o["args"][0])
            elif cmd == "app" and o.get("args"):
                app = str(o["args"][0])
            js = o.get("jev_screen")
            if js:
                if js != "other": scr, scr_rough = js, False
                elif not scr: scr, scr_rough = "other", False
                else: scr_rough = True   # 最近一屏判不出，沿用上一次判准的屏，标「约」
    start = 0.0
    try:
        start = time.mktime(time.strptime(job["started"], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        pass
    end = start
    if start:
        end = time.time() if job["status"] in ("running", "reviewing") else (time.mktime(time.strptime(job["finished"], "%Y-%m-%d %H:%M:%S")) if job.get("finished") else time.time())
    return {"id": job["id"], "title": job.get("title", ""), "status": job["status"], "mins": int((end - start) // 60) if start else 0,
            "steps": steps, "app": app, "screen": scr, "rough": scr_rough, "recent": recent[-3:],
            "idle": int((end if job["status"] not in ("running", "reviewing") and start else time.time()) - last_ts) if last_ts else None}


def progress_line(p, short=False):
    """多行版给 rmq progress，一句话版给飞书心跳。"""
    now_ = (p["app"] or "未知 App") + " · " + (("约 " if p["rough"] else "") + p["screen"] if p["screen"] else "还没读到屏")
    head = f"已跑 {p['mins']} 分 {p['steps']} 步；现在：{now_}"
    idle = f"距上次动作 {p['idle']} 秒" if p["idle"] is not None else "还没有手机动作"
    if short:
        return f"{head}；{idle}"
    rec = "\n".join("  " + x for x in p["recent"]) or "  （还没有）"
    return f"{head}\n最近 3 条动作：\n{rec}\n{idle}"


def progress(args):
    """rmq progress <id>：看一个任务跑到哪了（读 attempt*/kp.jsonl 末尾）。"""
    if not args: sys.exit("用法：rmq progress <任务id>")
    j = jload(JOBS / args[0] / "job.json")
    if not j: sys.exit("没有这个任务")
    if j["status"] == "queued":
        ahead = sum(1 for x in jobs() if x["status"] in ("queued", "running", "reviewing") and x["id"] < j["id"] and x.get("dev", "rmq1") == j.get("dev", "rmq1"))
        sys.exit(f"还在排队，前面还有 {ahead} 个任务")
    p = progress_info(j)
    print(f"任务 {j['id']}（{j['status']}）{j.get('title', '')}\n" + progress_line(p))


def running_json(_):
    """（mini）列出正在跑的任务及进度，relay 用它发 5 / 15 分钟心跳和「开始了」通知。"""
    out = []
    for j in jobs():
        if j["status"] == "running":
            out.append({"job": j, "progress": progress_info(j)})
    print(json.dumps(out, ensure_ascii=False))


HB_NAME = {"hb_start": "开始通知", "hb5": "5 分钟心跳", "hb15": "15 分钟心跳"}


def hb_due(st, key):
    """这条心跳还要不要发：已发（旧版记 True，新版记 "ok"）、不需要发、试满两次没送到的都不再发；没送到的等 HB_GAP 秒再试。"""
    return st.get(key) in (None, False) and time.time() >= (st.get(key + "_next") or 0)


def hb_send(c, job, tgt, due):
    """在这单的发送线程里按顺序发到点的心跳。只有飞书确认收到才记已发（11-12 前超时也算已发）；
    超时、失败隔 HB_GAP 秒再试一次，两次都没送到记「没送到」不再发；结果已经送过的单不再发心跳。"""
    for key, text in due:
        st = dstate(job)
        if st.get("final") or not hb_due(st, key):
            continue
        fs, detail = feishu(c, tgt, text, job, what=HB_NAME[key])
        if fs in ("ok", "skip"):
            dsave(job, **{key: fs, key + "_at": now()})
            continue
        n = int(st.get(key + "_tries") or 0) + 1
        if n >= HB_TRIES:
            dsave(job, **{key: "failed", key + "_tries": n, key + "_err": clip(detail, 200)})
            log(job, f"{HB_NAME[key]}试了 {n} 次都没送到，不再发")
        else:
            dsave(job, **{key + "_tries": n, key + "_next": time.time() + HB_GAP, key + "_err": clip(detail, 200)})
            log(job, f"{HB_NAME[key]}没送到，{HB_GAP} 秒后再试一次")


def heartbeats(c, r):
    """relay 每轮调一次：对 running 任务在 5 分钟和 15 分钟各发一条飞书心跳（只两次）；排队后才轮到的任务开跑时发一条「开始了」。
    11-12 起放进这单的发送线程里发（lane_run），一条卡住不拖住别的单，也不拖住 relay 主循环。"""
    p = subprocess.run(["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", r, "~/.local/bin/rmq", "running"],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    try:
        items = json.loads(p.stdout or "[]")
    except Exception:
        return
    for it in items:
        job, pr = it["job"], it["progress"]
        if job.get("quiet"): continue
        (JOBS / job["id"]).mkdir(exist_ok=True)
        st = dstate(job)
        tgt = job.get("notify") or c.get("notify") or ""
        due = []
        if job.get("queued_ahead") and hb_due(st, "hb_start"):
            due.append(("hb_start", f"RMQ1 ▶️ 轮到了，开始做：{job.get('title', '')}（任务 {job['id']}）"))
        for mark_, key in ((5, "hb5"), (15, "hb15")):
            # 已经过了更晚的那条就不补发早的（relay 掉线恢复后不连发两条）
            if pr["mins"] >= mark_ and hb_due(st, key) and not (key == "hb5" and pr["mins"] >= 15):
                due.append((key, f"RMQ1 ⏱ 任务 {job['id']} 进行中（{job.get('title', '')}）：{progress_line(pr, short=True)}"))
        if due:
            lane_run(job["id"], hb_send, c, job, tgt, due)


# ───────────── 执行 ─────────────
def run_claude(cmd, env, cwd, out_path, timeout, jdir=None, budget=False):
    """跑一个 claude -p，超时就杀。给了任务目录 jdir 时：kp handoff 等用户的时间不算进超时；
    用户 rmq stop 后 90 秒还没自己结束就杀（退出码 130）。
    budget=True（驾驶轮才开）：把已等用户的秒数写进 budget.json，并在用掉 70% / 90% 时间时各写一条预算提醒到 budget.txt
    （kpx 以「[系统预算]」前缀转给驾驶；不走 steer.txt，不带「以这句为准」）。等用户接手期间不发。"""
    with open(out_path, "w") as out:
        p = subprocess.Popen(cmd, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out,
                             stderr=subprocess.STDOUT, start_new_session=True)
        start = time.time()
        deadline, prev, rc = start + timeout, start, 124
        waited, sent = 0.0, set()
        if jdir and budget:
            for f in ("budget.txt", "budget.seen"):
                (jdir / f).unlink(missing_ok=True)
            budget_write(jdir, env, waited)
        while True:
            try:
                return p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            t = time.time()
            holding = bool(jdir and (jdir / "handoff.wait").exists())
            if holding:
                deadline += t - prev; waited += t - prev
            prev = t
            if jdir and budget:
                budget_write(jdir, env, waited)
                if not holding:
                    used = t - start - waited
                    for pct in (70, 90):
                        if pct not in sent and used >= timeout * pct / 100:
                            sent.add(pct); budget_remind(jdir, pct, used, timeout)
            st = jdir / "stop" if jdir else None
            if st and st.exists() and t - st.stat().st_mtime > 90:
                rc = 130; break
            if t > deadline:
                break
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try: os.killpg(p.pid, sig)
            except (ProcessLookupError, PermissionError):
                try: p.send_signal(sig)
                except ProcessLookupError: pass
            time.sleep(3)
        return rc


def budget_write(jdir, env, waited):
    """把本轮起点与已等用户的秒数留给 kpx 算「已用」（kpx 用 KP_T0 对上号才认，防读到上一轮的旧文件）。"""
    try:
        tmp = jdir / "budget.json.tmp"
        tmp.write_text(json.dumps({"t0": env.get("KP_T0", ""), "waited": round(waited)}))
        tmp.replace(jdir / "budget.json")
    except OSError:
        pass


def budget_remind(jdir, pct, used, timeout):
    """预算提醒：只在 budget.txt 追加一行，措辞不下命令式的「以这句为准」，只提醒先落盘。"""
    left = max(int(timeout - used), 0)
    msg = (f"已用掉 {pct}% 的时间（约 {int(used)//60} 分钟，还剩约 {left//60} 分 {left%60:02d} 秒）。"
           "先把已确认的结论写进 result.json，再补最关键的一项。")
    try:
        with open(jdir / "budget.txt", "a") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def kenv(job):
    """kp 的设备与身份：rmq 任务以 rmq:<id> 名义持有那台手机的租约。任务目录给 kp 转插话 / 叫停、记接管。"""
    dev = job.get("dev", "rmq1")
    try:
        name = json.load(open(DEVF))["devices"][dev].get("name", "")
    except Exception:
        name = ""
    return {**os.environ, "KP_DEV": dev, "KP_OWNER": f"rmq:{job['id']}", "KP_JOB_DIR": str(JOBS / job["id"]),
            "KP_JOB_ID": job["id"], "KP_NOTIFY": job.get("notify") or "", "KP_DEVNAME": name}


def kp(c, job, *a, timeout=60):
    return subprocess.run([c["kp"], *a], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=timeout, env=kenv(job))


def nothink_base(c, job, direct=None, port=7678, up=()):
    """驾驶不需要思考：经本机 dsnothink 代理把默认思考关掉（DeepSeek 每步省一段约千字的思考；
    GLM 复核 178 s → 81 s、结论一致，11-01 实测）。代理没起来就自己拉起；起不来退回直连并记一笔。"""
    import urllib.request
    url = f"http://127.0.0.1:{port}"
    def alive():
        try: return urllib.request.urlopen(url + "/health", timeout=2).read() == b"ok"
        except Exception: return False
    if not alive():
        subprocess.Popen([sys.executable, str(HERE / "dsnothink.py"), "--port", str(port), *up], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=open("/tmp/kvmphone/dsnothink.log", "a"), start_new_session=True)
        for _ in range(20):
            time.sleep(0.25)
            if alive(): break
        else:
            log(job, f"dsnothink 代理 :{port} 起不来，带思考直连"); return direct or c["ds_base"]
    return url


def driver_kind(job, c):
    """这个任务实际要用哪类驾驶：gemini（agy）/ cc（真 Claude）/ glm（GLM 5.3 Flash）/ ds（DeepSeek）。"""
    drv = job.get("driver") or c.get("driver") or ""
    if drv.startswith("gemini"): return "gemini"
    if drv == "glm" or drv.startswith("glm-"): return "glm"  # 11-18 加：claude -p 指智谱 Anthropic 兼容端点
    if drv == "cc" or drv.startswith("cc-"): return "cc"
    return "ds"


CC_ALIASES = {"cc-sonnet": "claude-sonnet-5-5", "cc-haiku": "claude-haiku-5-5", "cc-opus": "claude-opus-5-5"}


def cc_model(job, c):
    """cc 驾驶用哪个 Claude（11-18 加，比模型用）：--driver cc 用配置 cc_model；cc-sonnet / cc-haiku / cc-opus 选对应 5.5 型号；
    cc-claude-xxx 直接给型号名。"""
    drv = job.get("driver") or c.get("driver") or ""
    if drv.startswith("cc-claude-"): return drv[3:]
    return CC_ALIASES.get(drv) or c["cc_model"]


def driver_label(job, c):
    """job 显示用的真实模型名（rmq ls / show / 开跑日志）；Claude 没登录改走 DeepSeek 时以实际为准。"""
    if job.get("driver_real"): return job["driver_real"]
    k = driver_kind(job, c)
    if k == "cc" and job.get("status") not in ("queued", "running") and str(job.get("id", "")) < "261109-2300":
        return c["ds_model"]  # 11-09 23:00 以前的 cc 任务其实是 Claude Code 外壳 + DeepSeek Flash，没有 driver_real 字段
    if k == "glm": return c.get("glm_model", "glm-5.3-flash")
    return (job.get("driver") or c.get("driver")) if k == "gemini" else (cc_model(job, c) + "（本机）" if k == "cc" else c["ds_model"])


def claude_bin(c):
    """claude 可执行文件：配置里写了绝对路径就用它；否则 PATH 里找，再试常见位置（launchd 拉起的 relay PATH 很短）。"""
    b = c.get("claude", "claude")
    if "/" in b: return b
    for p in (shutil.which(b), "/opt/homebrew/bin/" + b, str(Path.home() / ".local/bin" / b), "/usr/local/bin/" + b):
        if p and os.path.exists(p): return p
    return b


def claude_login(c, fresh=False, unknown_ok=False):
    """本机（Mac relay 里就是 Mac）Claude 订阅登录态：`claude auth status` 解析 loggedIn，带超时，结果缓存 cc_auth_ttl 秒（默认 10 分钟）。
    取不到结果（超时、命令不存在）按未登录处理但不写缓存，下次再试。
    unknown_ok=True 时，取不到结果返回 None（11-10：Mac 过载时 auth status 超过 20 秒，被误判成未登录退回 DeepSeek）。"""
    f = HOME / "cc_auth.json"
    cached = jload(f, {})
    if not fresh and cached and time.time() - cached.get("t", 0) < c.get("cc_auth_ttl", 600):
        return bool(cached.get("ok"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_") and k != "CLAUDE_CONFIG_DIR"}
    try:
        p = subprocess.run([claude_bin(c), "auth", "status"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=c.get("cc_auth_timeout", 45), env=env)
        ok = bool(json.loads(p.stdout).get("loggedIn"))
    except Exception:
        return None if unknown_ok else False
    try:
        HOME.mkdir(parents=True, exist_ok=True); jsave(f, {"t": time.time(), "ok": ok})
    except OSError:
        pass
    return ok


def budget_env(c):
    """预算环境变量：KP_T0 = 本轮驾驶开始的时间戳，KP_BUDGET = 本轮时限（秒）；kpx 用它在每条 kp 输出末尾报「已用 / 总共」。"""
    return {"KP_T0": str(int(time.time())), "KP_BUDGET": str(int(c["max_runtime"]))}


def render_prompt(kind):
    """prompt.md 里 <!--cc:begin-->…<!--cc:end--> 与 <!--gemini:begin-->…<!--gemini:end--> 两种段落按驾驶挑一种；
    kind 取 cc（Sonnet / DeepSeek 都算）或 gemini，其余部分是公共的。"""
    txt = (HERE / "prompt.md").read_text()
    return re.sub(r"<!--(cc|gemini):begin-->\n?(.*?)<!--\1:end-->\n?", lambda m: m.group(2) if m.group(1) == kind else "", txt, flags=re.S)


def kp_help_text(c, local=True):
    """kp -h 的输出，直接附在提示里，省掉驾驶自己探路那一轮（本机和 mini 的 kp.py 是同一份）。"""
    try:
        kpy = str(Path(shutil.which("kp") or "").resolve()) if local and shutil.which("kp") else c.get("kp_local", str(HERE.parent / "kvmphone" / "kp.py"))
        return subprocess.run([sys.executable, kpy, "-h"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return ""


def claude_setup(job, c, kind, env):
    """按驾驶种类补好 claude -p 的环境变量，返回模型名。cc = 真 Sonnet：用 mini 默认 ~/.claude 的订阅登录，
    不设端点、不设密钥、不换配置目录；其余（ds / deepseek）走 DeepSeek 端点。env 里的 ANTHROPIC_* 调用前已去掉。"""
    if kind == "cc":
        env.pop("CLAUDE_CONFIG_DIR", None)
        return cc_model(job, c)
    if kind == "glm":  # 11-18：GLM 5.3 Flash 驾驶，接口文件与复核共用（{"base","key"}），同样经 :7679 关思考
        api = json.loads(Path(c.get("glm_api") or c["review_api"]).expanduser().read_text())
        base = api["base"]
        if c.get("ds_nothink") and "bigmodel.cn/api/anthropic" in base:
            base = nothink_base(c, job, base, 7679, ("--up", "open.bigmodel.cn", "--prefix", "/api/anthropic"))
        env.update(ANTHROPIC_BASE_URL=base, ANTHROPIC_AUTH_TOKEN=api["key"],
                   CLAUDE_CONFIG_DIR=str(HOME / "claude-glm"), CLAUDE_CODE_MAX_CONTEXT_TOKENS="1000000")
        (HOME / "claude-glm").mkdir(exist_ok=True)
        return c.get("glm_model", "glm-5.3-flash")
    key = Path(c["ds_key_file"]).expanduser().read_text().strip()
    base = nothink_base(c, job) if c.get("ds_nothink") else c["ds_base"]
    env.update(ANTHROPIC_BASE_URL=base, ANTHROPIC_API_KEY=key,
               CLAUDE_CONFIG_DIR=str(HOME / "claude-ds"), CLAUDE_CODE_MAX_CONTEXT_TOKENS="1000000")
    (HOME / "claude-ds").mkdir(exist_ok=True)
    return c["ds_model"]


def drive(job, c, adir, hint):
    kind = driver_kind(job, c)
    if kind == "gemini":
        return drive_remote(job, c, adir, hint, job.get("driver") or c.get("driver"))
    if kind == "cc" and not job.get("cc_fallback"):
        # 11-10：cc 驾驶在本机 Mac 上跑（Mac 的 Claude 订阅登录），kp 经 ssh 回 mini；mini 上不再登录 Claude
        rc, why = drive_mac_cc(job, c, adir, hint)
        if why is None:
            return rc
        log(job, why)
        for f in ("driver.remote.log", "drive.done", "drive.decline", "drive.taken", "drive.req", "drive.kill"):  # 别留 Mac 拒单的残渣给 review / stats
            (adir / f).unlink(missing_ok=True)
        update(job, driver_real=c["ds_model"], driver_note=why, cc_fallback=True)
        kind = "ds"
    elif kind == "cc":
        kind = "ds"  # 这个任务前面已经退回过 DeepSeek，后面几轮不再等 Mac
    env = {k: v for k, v in kenv(job).items() if not k.startswith("ANTHROPIC_")}
    env["KP_LOG"] = str(adir / "kp.jsonl")
    env.update(budget_env(c))
    model = claude_setup(job, c, kind, env)
    kph = kp_help_text(c)
    prompt = (render_prompt("cc") + f"\n当前任务目录：{adir}（result.json 写在这里）。\n\n"
              + (f"kp 用法（即 kp -h 的输出）：\n{kph}\n\n" if kph else "") + f"任务：{job['task']}\n")
    if hint:
        prompt += f"\n上一轮复核没通过，这次重点改：{hint}\n"
    a = str(adir)
    # Write 规则与 Edit 同范围：11-09 实测（Sonnet 5.5，同参数 dontAsk）Edit 规则本身已放行 Write，
    # 显式再写一条 Write 规则是为了不依赖这个行为；`;` 复合的 kp 命令也放行。不放任何能跑任意命令的通配。
    allowed = ["Bash(kp:*)", "Bash(sleep:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(grep:*)",
               "Read(//tmp/kvmphone/**)", f"Read(/{a}/**)", f"Edit(/{a}/**)", f"Write(/{a}/**)"]
    cmd = [c["claude"], "-p", prompt, "--model", model, "--safe-mode", "--no-session-persistence",
           "--permission-mode", "dontAsk", "--tools", "Bash,Read,Write", "--allowedTools", *allowed,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
           "--max-turns", str(c["max_turns"]), "--output-format", "stream-json", "--verbose"]
    return run_claude(cmd, env, adir, adir / "driver.log", c["max_runtime"], JOBS / job["id"], budget=True)


CC_DECLINE_RC = 97   # 本机 relay 接了单但不能跑（Claude 未登录等）：drive.done 里写这个，原因写 drive.decline


def drive_mac_cc(job, c, adir, hint):
    """cc 驾驶（11-10）：留驾驶请求给本机 Mac 的 relay（kind=cc），relay 在 Mac 上跑 claude -p，kp 经 ssh 回 mini 执行；
    result.json / driver.log 由 relay 每 15 秒同步回这个任务目录。mini 这边起 rwait 占位交给 run_claude：超时、rmq stop、等用户接手的计时都照旧，
    被收走时 rwait 留 drive.kill，relay 3 秒内杀掉 Mac 上的 claude。
    返回 (rc, None) 正常；(None, 原因) 表示要退回 DeepSeek（Mac 没接单 / Claude 未登录）。"""
    for f in ("drive.done", "drive.kill", "drive.taken", "drive.decline", "driver.remote.log"):
        (adir / f).unlink(missing_ok=True)
    env = {**kenv(job), "KP_LOG": str(adir / "kp.jsonl"), **budget_env(c)}
    q = {"job": job["id"], "adir": str(adir), "kind": "cc", "model": cc_model(job, c), "max_turns": c["max_turns"],
         "task": job["task"], "hint": hint or "",
         "env": {k: env[k] for k in ("KP_DEV", "KP_OWNER", "KP_JOB_DIR", "KP_JOB_ID", "KP_NOTIFY", "KP_DEVNAME", "KP_LOG", "KP_T0", "KP_BUDGET")}}
    jsave(adir / "drive.req", q)
    log(job, f"驾驶 {cc_model(job, c)}（本机）：等本机 Mac 的 relay 接手")
    rc = run_claude([sys.executable, str(HERE / "rmq.py"), "rwait", str(adir), "--accept", str(c.get("cc_accept_secs", 90))],
                    env, adir, adir / "driver.log", c["max_runtime"], JOBS / job["id"], budget=True)
    if rc == 2 and not (adir / "drive.taken").exists():
        (adir / "drive.req").unlink(missing_ok=True)
        return None, "本机 Mac 没接单，本次用 DeepSeek Flash 驾驶"
    if rc == CC_DECLINE_RC:
        why = ""
        try: why = (adir / "drive.decline").read_text().strip()
        except OSError: pass
        return None, why or "本机 Mac 不能跑 Claude，本次用 DeepSeek Flash 驾驶"
    if rc in (124, 130):  # 被收走：给 relay 一点时间做最后一次同步（它会写 drive.done），再让主流程读 result.json
        for _ in range(40):
            if (adir / "drive.done").exists(): break
            time.sleep(0.5)
    return rc, None


def drive_remote(job, c, adir, hint, model):
    """Gemini 驾驶（11-01 用户要试）：agy 只装在本机 Mac，mini 又连不回 Mac。这里留一张驾驶请求，本机 relay 取走后在 Mac 上跑 agy，
    agy 里的 kp 经 ssh 回 mini 执行；mini 这边起个等待进程交给 run_claude，超时、叫停、等用户接手的计时都照旧。"""
    for f in ("drive.done", "drive.kill", "drive.taken", "driver.remote.log"):
        (adir / f).unlink(missing_ok=True)
    env = {**kenv(job), "KP_LOG": str(adir / "kp.jsonl"), **budget_env(c)}
    q = {"job": job["id"], "adir": str(adir), "model": model, "task": job["task"], "hint": hint or "",
         "env": {k: env[k] for k in ("KP_DEV", "KP_OWNER", "KP_JOB_DIR", "KP_JOB_ID", "KP_NOTIFY", "KP_DEVNAME", "KP_LOG", "KP_T0", "KP_BUDGET")}}
    if c.get("gemini_local"):  # 11-03 起 mini 自己登录了谷歌、走东京节点：agy 就在 mini 上跑，不再绕 Mac
        (adir / "drive.taken").touch()  # 先占住，免得 Mac 的 relay 还来取
        jsave(adir / "drive.req", q)
        threading.Thread(target=relay_drive, args=(c, None, q), daemon=True).start()
        log(job, f"驾驶换成 {model}：在 mini 本机跑 agy")
    else:
        jsave(adir / "drive.req", q)
        log(job, f"驾驶换成 {model}：等本机 Mac 的 relay 接手")
    rc = run_claude([sys.executable, str(HERE / "rmq.py"), "rwait", str(adir)], env, adir, adir / "driver.log",
                    c["max_runtime"], JOBS / job["id"], budget=True)
    if rc == 2 and not (adir / "drive.taken").exists():  # Mac 没开或 relay 没跑：这一轮退回 DeepSeek，任务照跑
        log(job, "本机两分钟没接手，这一轮改用 DeepSeek 驾驶")
        (adir / "drive.req").unlink(missing_ok=True)
        return drive({**job, "driver": "deepseek"}, c, adir, hint)
    return rc


def rwait(args):
    """mini 上的驾驶占位：等本机跑完（drive.done 里是退出码）。被 run_claude 收走时留 drive.kill，本机那边也停。"""
    adir = Path(args[0])
    accept = float(args[args.index("--accept") + 1]) if "--accept" in args else 120
    def bye(*_):
        (adir / "drive.kill").touch(); sys.exit(124)
    signal.signal(signal.SIGTERM, bye)
    t0 = time.time()
    while not (adir / "drive.done").exists():
        if time.time() - t0 > accept and not (adir / "drive.taken").exists():
            print(f"本机 relay {int(accept)} 秒没来取驾驶请求（Mac 没开或 relay 没跑）", flush=True); sys.exit(2)
        time.sleep(0.5)
    rl = adir / "driver.remote.log"
    if rl.exists():
        sys.stdout.write(rl.read_text(errors="ignore")[-300000:])
    sys.exit(int((adir / "drive.done").read_text().strip() or 1))


def drives(args):
    """本机 relay 取驾驶请求。--wait N：暂时没有就每 0.3 秒看一次、最多等 N 秒（长轮询，请求一落地就被接走；原来 relay 10 秒才问一次）"""
    wait = float(args[args.index("--wait") + 1]) if "--wait" in args else 0
    t0 = time.time()
    while True:
        out = [json.loads(f.read_text()) for f in JOBS.glob("*/attempt*/drive.req")
               if not (f.parent / "drive.taken").exists() and not (f.parent / "drive.done").exists()]
        if out or time.time() - t0 >= wait:
            break
        time.sleep(0.3)
    print(json.dumps(out, ensure_ascii=False))


def stats(job, adirs):
    """任务小结（Mika StopSummary）：总用时、其中等用户多久、手机动作几步、模型几轮、用了多少 token。"""
    s = {"actions": 0, "turns": 0, "in_tok": 0, "new_in": 0, "cache_read": 0, "out_tok": 0, "cost": 0.0, "cost_basis": None,
         "waited": 0, "step_ms": 0, "step_n": 0}
    for a in adirs:
        for line in (a / "kp.jsonl").read_text(errors="ignore").splitlines() if (a / "kp.jsonl").exists() else []:
            try:
                o = json.loads(line)
                s["actions"] += o.get("cmd") in kpx_acts
                if o.get("act_done") and isinstance(o.get("dur_total"), (int, float)):  # K0 埋点；旧日志没有这个字段就跳过
                    s["step_ms"] += o["dur_total"]; s["step_n"] += 1
            except Exception:
                pass
        gem = False
        if (a / "driver.remote.log").exists():  # Gemini（agy stream-json）：每个 agent_response 步算一轮，用量逐步累加
            for line in (a / "driver.remote.log").read_text(errors="ignore").splitlines():
                try:
                    su = json.loads(line).get("step_update") or {}
                except Exception:
                    continue
                if su:
                    gem = True
                u = su.get("usage")
                if su.get("state") == "DONE" and su.get("step_type") == "agent_response" and u:
                    s["turns"] += 1
                    s["in_tok"] += (u.get("input_tokens") or 0) + (u.get("cache_read_tokens") or 0)
                    s["new_in"] += u.get("input_tokens") or 0
                    s["cache_read"] += u.get("cache_read_tokens") or 0
                    s["out_tok"] += (u.get("output_tokens") or 0) + (u.get("thinking_tokens") or 0)
        if gem:
            continue
        # Claude stream-json：cc 驾驶的数据在 driver.remote.log（被收走时 rwait 不会把它打进 driver.log），DeepSeek 在 driver.log
        done = False
        for fn in ("driver.log", "driver.remote.log"):
            if done or not (a / fn).exists():
                continue
            for line in reversed((a / fn).read_text(errors="ignore").splitlines()):
                if '"type":"result"' not in line.replace(" ", ""):
                    continue
                done = True
                try:
                    o = json.loads(line)
                except Exception:
                    break
                u = o.get("usage") or {}
                s["turns"] += o.get("num_turns") or 0
                s["in_tok"] += sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                s["new_in"] += (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)  # 新写缓存也是新输入
                s["cache_read"] += u.get("cache_read_input_tokens") or 0
                s["out_tok"] += u.get("output_tokens") or 0
                s["cost"] += o.get("total_cost_usd") or 0
                s["cost_basis"] = s["cost_basis"] or o.get("costBasis") or o.get("cost_basis")
                break
    hf = JOBS / job["id"] / "handoff.jsonl"
    for line in hf.read_text().splitlines() if hf.exists() else []:
        s["waited"] += json.loads(line).get("waited", 0)
    try:
        s["secs"] = int(time.time() - time.mktime(time.strptime(job["started"], "%Y-%m-%d %H:%M:%S")))
    except Exception:
        s["secs"] = 0
    k = lambda n: f"{n / 1e6:.1f}M" if n >= 1e6 else (f"{n / 1e3:.0f}k" if n >= 1e3 else str(int(n)))
    # 11-09：token 拆成新输入 / 缓存复用 / 输出（之前把缓存读算进「入」，一个任务显示入 21M 吓人）；
    # 成本只在 costBasis 已知时显示（claude -p 的 total_cost_usd 是按 Anthropic 价目估的，DeepSeek / Sonnet 订阅都不是真实扣费，所以默认不显示）
    s["text"] = (f"用时 {s['secs'] // 60} 分 {s['secs'] % 60} 秒" + (f"（其中等你 {s['waited'] // 60} 分）" if s["waited"] else "")
                 + f" · 手机 {s['actions']} 步 · 模型 {s['turns']} 轮 · 新输入 {k(s['new_in'])} · 缓存复用 {k(s['cache_read'])} · 输出 {k(s['out_tok'])}"
                 + (f" · 每步均 {s['step_ms'] / s['step_n'] / 1000:.1f} 秒" if s["step_n"] else "")
                 + (f" · 成本 ${s['cost']:.2f}（{s['cost_basis']}）" if s["cost_basis"] and s["cost"] else ""))
    return s


def tail_text(p, n=40000):
    try:
        t = Path(p).read_text(errors="ignore")
    except Exception:
        return ""
    # stream-json：只留 assistant 文本与工具结果的摘要
    keep, said = [], {}
    for line in t.splitlines():
        try:
            o = json.loads(line)
        except Exception:
            continue
        su = o.get("step_update") if o.get("event") == "step_update" else None
        if su:  # agy（Gemini）stream-json：文本按 step 拼起来，工具步记参数和输出
            i = su.get("step_index")
            said[i] = said.get(i, "") + (su.get("text_delta") or "")
            if su.get("state") != "DONE":
                continue
            if su.get("step_type") == "agent_response" and said.get(i, "").strip():
                keep.append("说：" + said[i].strip()[:400])
            elif su.get("step_type") == "tool":
                ti = su.get("tool_info") or {}
                keep.append("做：" + json.dumps(ti.get("parameters"), ensure_ascii=False)[:200])
                if ti.get("output"):
                    keep.append("屏：" + str(ti["output"])[:1500])
            continue
        if o.get("type") == "assistant":
            for b in o.get("message", {}).get("content", []):
                if b.get("type") == "text": keep.append("说：" + b["text"][:400])
                elif b.get("type") == "tool_use": keep.append("做：" + json.dumps(b.get("input"), ensure_ascii=False)[:200])
        elif o.get("type") == "user":
            for b in o.get("message", {}).get("content", []):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    cc = b.get("content")
                    cc = cc if isinstance(cc, str) else json.dumps(cc, ensure_ascii=False)
                    keep.append("屏：" + cc[:1500])
    return "\n".join(keep)[-n:]


def review(job, c, adir):
    rl = adir / "driver.remote.log"
    (adir / "driver_tail.txt").write_text(tail_text(rl if rl.exists() else adir / "driver.log"))
    hf = JOBS / job["id"] / "handoff.jsonl"
    if hf.exists():
        (adir / "handoff.jsonl").write_text(hf.read_text())
    prompt = (HERE / "review.md").read_text().replace("{DIR}", str(adir)).replace("{TASK}", job["task"])
    env = {k: v for k, v in kenv(job).items() if not k.startswith("ANTHROPIC_")}
    if c.get("review_api"):  # 复核走第三方 Anthropic 兼容端点（如 GLM），不需要登录 Claude；文件里是 {"base","key"}
        api = json.loads(Path(c["review_api"]).expanduser().read_text())
        base = api["base"]
        if c.get("review_nothink") and "bigmodel.cn/api/anthropic" in base:
            base = nothink_base(c, job, base, 7679, ("--up", "open.bigmodel.cn", "--prefix", "/api/anthropic"))
        env.update(ANTHROPIC_BASE_URL=base, ANTHROPIC_AUTH_TOKEN=api["key"],
                   CLAUDE_CONFIG_DIR=str(HOME / "claude-review"), CLAUDE_CODE_MAX_CONTEXT_TOKENS="200000")
        (HOME / "claude-review").mkdir(exist_ok=True)
    a = str(adir)
    cmd = [c["claude"], "-p", prompt, "--model", c["review_model"], "--safe-mode", "--no-session-persistence",
           "--permission-mode", "dontAsk", "--tools", "Read", "--allowedTools", f"Read(/{a}/**)", "Read(//tmp/kvmphone/**)",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", ""]
    rc = run_claude(cmd, env, adir, adir / "review.log", 600)
    t = (adir / "review.log").read_text(errors="ignore")
    dec, found = json.JSONDecoder(), None  # 日志里可能夹着别的 {…} 行（如 unrecognized_model），逐个起点解析，取最后一个带 pass 的对象
    for i in (k for k, ch in enumerate(t) if ch == "{"):
        try:
            r, _ = dec.raw_decode(t, i)
        except ValueError:
            continue
        if isinstance(r, dict) and "pass" in r:
            found = r
    if found:
        found["rc"] = rc; jsave(adir / "review.json", found); return found
    return {"pass": False, "answer": "", "issues": [f"复核没有给出 JSON（退出码 {rc}）"], "retry_hint": "", "boundary": "未知", "rc": rc}


PAID = re.compile(r"支付成功|付款成功|下单成功|支付完成|订单提交成功")


PAID_TS = re.compile(r"(?<!\d)(?:(20\d\d)\s*[-./年]\s*)?(\d{1,2})\s*[-./月]\s*(\d{1,2})\s*日?(?:\s|\\[rnt]|[|•·])*(\d{1,2}):(\d{2})")


def job_t0(job_id):
    """任务 id 形如 261109-234112-4945（排队时刻，本机时区）；解析不了返回 None"""
    try:
        return datetime.datetime.strptime(str(job_id)[:13], "%y%m%d-%H%M%S")
    except ValueError:
        return None


def paid_hit(text, t0=None):
    """付款成功词命中，但排除订单详情里的历史时间线（11-10：淘宝物流基准读到饿了么订单跟踪「• 订单提交成功 11-04 22:44」误报）。
    命中词后 60 字内带「月-日 时:分」且早于任务排队 5 分钟以上 = 旧订单记录，跳过；没带时间、时间解析不了或时间在任务开始后都照旧算付款痕迹。"""
    for m in PAID.finditer(text):
        if t0 is not None:
            ts = PAID_TS.search(text[m.end():m.end() + 60])
            if ts:
                y, mo, d, hh, mi = ts.groups()
                try:
                    yr = int(y) if y else (t0.year - 1 if int(mo) > t0.month else t0.year)
                    when = datetime.datetime(yr, int(mo), int(d), int(hh), int(mi))
                except ValueError:
                    when = None
                if when is not None and when < t0 - datetime.timedelta(minutes=5):
                    continue
        return m.group(0)
    return ""


UNDONE = re.compile(r"正在(前往|比|查|搜|看|进|去|打开|处理|继续)|尚未|还没有?(比|查|搜|看|做|去|进)|待继续|未完成|没做完|准备前往|继续前往|接下来(去|再|要)")


NOBD = re.compile(r"^(无|没有|未发现|未越界|none|null)", re.I)
NEGPAY = re.compile(r"(未|没有|没|无|不曾|并未)(曾|有)?(付款|支付|下单|购买|提交订单|月付)")
PAYB = re.compile(r"付款|误付|支付|下单|购买|提交订单|月付")  # 复核的 boundary 是自由文字，命中这些词按付款痕迹算
UNVER = re.compile(r"未核实|待确认|未确认|不确定|没写明|未写明|没写|未写|需问|要问卖家|需要问")
AMOUNT = re.compile(r"[¥￥]\s*(\d[\d,]*(?:\.\d+)?)")
TIER_HEAD = {"ok": "✅ 办好了", "part": "🟡 部分完成", "fail": "❌ 没做成", "stop": "⏹ 已叫停"}


def clip(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "…"


def fmt_cand(x):
    """候选可能是字符串，也可能是带字段的 dict：按「商家 · 价 · 关键参数 · 待确认」的顺序逐字段摆出来。"""
    if not isinstance(x, dict):
        return clip(x, 160)
    order = ("平台", "商家", "店", "店名", "名称", "商品", "价格", "价", "实付", "参数", "关键参数", "时效", "到货", "提醒")
    parts = [str(x[k]) for k in order if x.get(k)]
    parts += [f"{k}：{v}" for k, v in x.items() if k not in order and k not in ("待确认", "未核实") and v]
    parts += [f"待确认：{x[k]}" for k in ("待确认", "未核实") if x.get(k)]
    return clip(" · ".join(parts), 160)


def norm_items(res):
    """result.json → 逐条要点：有候选用候选，没有候选就把「细节」按键逐条摆出来（超时的半成品常只有细节）。"""
    if not isinstance(res, dict):
        return []
    cand = res.get("候选")
    if isinstance(cand, list) and cand:
        return [fmt_cand(x) for x in cand if x][:6]
    det = res.get("细节")
    if isinstance(det, dict):
        out = []
        for k, v in det.items():
            if v in (None, "", [], {}):
                continue
            v = "，".join(f"{a}：{b}" for a, b in v.items()) if isinstance(v, dict) else (", ".join(map(str, v)) if isinstance(v, list) else v)
            out.append(clip(f"{k}：{v}", 160))
        return out[:6]
    return []


def usable(res):
    """半成品也算有用的标准：写了答案，且有非空的候选或细节。"""
    return isinstance(res, dict) and bool(res.get("答案")) and bool(res.get("候选") or res.get("细节"))


def tri_state(res, r, ok, stopped):
    """三态：ok ✅完成 / part 🟡部分完成 / fail ❌失败 / stop ⏹叫停。返回 (态, 是否付款痕迹)。
    付款痕迹和越界独立于三态：永远 fail，不被候选降级。"""
    bd = re.sub(r"[\s。.!！]+$", "", str((r or {}).get("boundary") or "").strip())
    none_ = (not bd) or (bool(NOBD.match(bd)) and "付款痕迹" not in bd)
    if none_:
        paid = viol = False
    else:
        paid = "付款痕迹" in bd or bool(PAYB.search(NEGPAY.sub("", bd)))
        viol = True
    if paid or viol:
        return "fail", paid
    if stopped:
        return "stop", False
    if ok:
        return ("part" if (r or {}).get("todo") or (isinstance(res, dict) and res.get("没做到")) else "ok"), False
    return ("part" if usable(res) else "fail"), False


def mark_amounts(s, raw):
    """交付前数字核对：消息里出现的金额必须能在 result.json 原文里找到；找不到的标「金额未核实」，不删。"""
    if not isinstance(s, str) or not raw:
        return s
    flat = raw.replace(",", "")
    bad = [m for m in AMOUNT.findall(s) if m.replace(",", "") not in flat]
    return s + "（金额未核实）" if bad and "金额未核实" not in s else s


def stop_point(adir):
    """超时交付要说停在哪：kp.jsonl 里最后一次判屏（JEV 状态）和最后一条 kp 命令。"""
    scr, cmd = "", ""
    try:
        for line in reversed((adir / "kp.jsonl").read_text(errors="ignore").splitlines()):
            try:
                o = json.loads(line)
            except Exception:
                continue
            if not cmd and o.get("cmd"):
                cmd = "kp " + str(o["cmd"]) + (" " + " ".join(map(str, o.get("args") or []))[:40] if o.get("args") else "")
            if not scr and o.get("jev_screen"):
                scr = str(o["jev_screen"])
            if scr and cmd:
                break
    except OSError:
        pass
    return "；".join(filter(None, [f"最后一屏判为 {scr}" if scr else "", f"最后一条命令 {cmd.strip()}" if cmd else ""]))


def selfcheck(job, adir, res, rc=0):
    """不复核时（配置 review: false，11-01 用户要「直接一遍过」）：只信驾驶自己写的 result.json，外加付款痕迹、
    超时和「结论还在进行时」三道机械检查（11-01 USB 那单 rc=124、答案写着「正在前往京东秒送比价」却判成办好了）"""
    hit = ""
    for f in ("kp.jsonl", "driver.log", "driver.remote.log"):
        p = adir / f
        if p.exists() and (hit := paid_hit(p.read_text(errors="ignore"), job_t0(job.get("id")))):
            break
    issues = [f"手机屏幕上出现过「{hit}」，可能误付了款，去订单里看一眼"] if hit else []
    if not res.get("答案"):
        issues.append("没写出结论")
    if res.get("没做到"):
        issues.append(res["没做到"])
    todo = res.get("没做到") or ""
    if rc == 124:
        issues.append("到 %d 分钟时限被收走，没做完" % (int(cfg().get("max_runtime", 1500)) // 60))
        todo = todo or "到时限被收走，后面没做完（看结论里停在哪一步）"
    m = UNDONE.search(" ".join(str(res.get(k) or "") for k in ("答案", "备注")))
    if m and not res.get("没做到"):
        issues.append(f"结论里写着「{m.group(0)}」，说明还没做完")
        todo = todo or f"结论停在：{(res.get('答案') or '')[:80]}"
    side = res.get("副作用")
    return {"pass": not issues, "answer": res.get("答案") or "", "issues": issues, "boundary": "付款痕迹" if hit else "无",
            "headline": res.get("答案") or "", "label": "候选" if res.get("候选") else "要点",
            "items": norm_items(res),
            "todo": todo, "why": "；".join(issues),
            "leftover": "；".join(map(str, side)) if isinstance(side, list) else (side or "")}


def dstate(job):
    """交付状态（decide 卡推过没有、飞书超时重试过没有、失败卡推过没有、告警写过没有）：落在任务目录，relay 每轮覆盖 job.json 也不丢。"""
    return jload(JOBS / job["id"] / "delivery.json", {}) or {}


def dsave(job, **kw):
    st = dstate(job); st.update(kw)
    jsave(JOBS / job["id"] / "delivery.json", st)


# ───────────── 结果送达（11-12 起如实记账）─────────────
# 送达状态（job.json 的 delivery，写在任务所在的机器上，relay 用 rmq mark 写回）：
#   pending 待送达（做完了，结果正在发 / 自动重试中）  delivered 已送达（openclaw 退出码 0 = 飞书接口回了消息 id）
#   not_needed 不需要发（--notify none、收件人不是 feishu:…、本机没有 openclaw）  failed 没送到（自动重试用完，等人 rmq redeliver）
# delivered 布尔只在「已送达」时为 True。改版前的任务没有 delivery 字段，按本机 relay 的 events.log 推断（delivery_state）。
DSTATES = {"pending": "待送达", "delivered": "已送达", "not_needed": "不需要发", "failed": "没送到"}
FINISHED = ("done", "failed", "stopped")
# 单次 openclaw message send 的等待上限（秒；配置 feishu_timeout 可改）。正常一次 2–8 秒（openclaw 日志 client ready → Sent 1.3–1.8 秒，
# 起步 2–3 秒）；11-12 23:45 本机负载 21、交换区快满时一次 29.9 秒（起步就占 28 秒）。openclaw 自己每个 HTTP 请求超时 30 秒、一次发送两个请求，
# 所以等 60 秒：慢的不误杀，真卡住就杀掉交给下面的重试
FEISHU_TIMEOUT = 60
DELIVER_GAPS = (30, 120, 300)   # 结果没送到时隔 30 秒、2 分钟、5 分钟再试：共 4 次，约 8 分钟内试完
HB_TRIES, HB_GAP = 2, 60    # 心跳 / 开始通知：没送到隔 60 秒再试一次，两次都没送到记「没送到」，不再发


def scrub(s, n=300):
    """错误输出进日志前：去掉令牌、密钥、长串 id，截断到 n 字。"""
    s = redact(" ".join(str(s or "").split()))
    s = re.sub(r"(?i)\b(bearer|basic)\s+\S+", r"\1 ***", s)
    s = re.sub(r"\b(ou|on|oc|om|cli)_[0-9a-zA-Z]{6,}", r"\1_***", s)   # 飞书用户 / 会话 / 消息 / 应用 id
    s = re.sub(r"[A-Za-z0-9+/_=-]{32,}", "***", s)
    return s[-n:] if len(s) > n else s


def feishu(c, target, msg, job=None, what="飞书"):
    """发一条飞书，只试一次，返回 (状态, 说明)：ok = openclaw 退出码 0（飞书接口确认收到）；skip = 故意不发
    （收件人不是 feishu:…、本机没有 openclaw）；timeout = feishu_timeout 秒没回（整组进程杀掉，按没送到算）；fail = 非零退出或起不来。
    说明里带用时和截断、去密钥后的错误输出。重试由调用方安排：结果见 deliver_due，心跳见 hb_send。"""
    why = "" if target.startswith("feishu:") else f"收件人「{target or '空'}」不是 feishu:…"
    if not why and not Path(c["openclaw"]).exists():
        why = f"本机没有 openclaw（{c['openclaw']}）"
    if why:
        if job: log(job, f"{what}不需要发：{why}")
        return "skip", why
    acct = ["--account", c["feishu_account"]] if c.get("feishu_account") else []
    lim = int(c.get("feishu_timeout") or FEISHU_TIMEOUT)
    t0 = time.time()
    try:
        p = subprocess.Popen([c["openclaw"], "message", "send", "--channel", "feishu", *acct, "-t", target.split(":", 1)[1], "-m", msg],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             start_new_session=True)
    except Exception as e:
        detail = f"openclaw 起不来：{scrub(e)}"
        if job: log(job, f"{what}发送失败（{detail}）")
        return "fail", detail
    try:
        out, err = p.communicate(timeout=lim)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)   # openclaw 是 node：整组杀，别留一个挂着的进程过后又把消息发出去
        except Exception:
            p.kill()
        try:
            out, err = p.communicate(timeout=5)
        except Exception:
            out, err = "", ""
        detail = f"{lim} 秒没回，已杀掉；输出：{scrub((err or '') + ' ' + (out or '')) or '（没有）'}"
        if job: log(job, f"{what}发送超时，按没送到算（{detail}）")
        return "timeout", detail
    secs = time.time() - t0
    if p.returncode == 0:
        mid = re.search(r"\bom_[0-9a-zA-Z]{6,}", out or "")   # 飞书回的消息 id，只留开头几位
        detail = f"用时 {secs:.1f}s" + (f"，消息 id {mid.group(0)[:10]}…" if mid else "")
        if job: log(job, f"{what}：飞书推送 rc=0 {detail}")
        return "ok", detail
    detail = f"rc={p.returncode} 用时 {secs:.1f}s：{scrub((err or '') + ' ' + (out or '')) or '（没有输出）'}"
    if job: log(job, f"{what}发送失败（{detail}）")
    return "fail", detail


def brief_parts(job, ok, answer):
    """给人看的汇报：结论在前、候选逐行，复核的内部细节不往外发（11-01 用户嫌飞书那段又长又乱）"""
    b = job.get("brief") or {}
    tier = b.get("tier") or ("stop" if job.get("status") == "stopped" else "ok" if ok else "fail")
    head = "❌ 疑似已付款，先去订单里核对" if b.get("paid") else TIER_HEAD[tier]
    items = [s.strip() for s in b.get("items") or [] if isinstance(s, str) and s.strip()][:6]
    secs, waited = (job.get("stats") or {}).get("secs", 0), (job.get("stats") or {}).get("waited", 0)
    foot = f"任务 {job['id']}" + (f" · 用时 {secs // 60} 分" if secs else "") + (f"（等你 {waited // 60} 分）" if waited >= 60 else "") + (f" · {job['driver_note']}" if job.get("driver_note") else "")
    return head, (b.get("headline") or answer).strip(), b.get("label") or "要点", items, b, foot


def brief_msg(job, ok, answer):
    head, line, label, items, b, foot = brief_parts(job, ok, answer)
    out = [f"{head}｜{job['title']}"]
    if items:
        nun = sum(1 for s in items if UNVER.search(s) or "金额未核实" in s)
        out.append(f"已核实 {len(items) - nun} 项 / 未核实 {nun} 项")
    out += ["", line]
    if items:
        out += ["", f"【{label}】"] + [f"{i}. {s}" for i, s in enumerate(items, 1)]
    st = job.get("stats") or {}
    if b.get("timeout"):  # 超时交付：用了多久、已确认什么、停在哪、手机上发生过什么，一句说清
        sure = "；".join(clip(s, 30) for s in [x for x in items if not (UNVER.search(x) or "金额未核实" in x)][:3]) or "暂无"
        mins = max(0, (st.get("secs", 0) - st.get("waited", 0))) // 60
        out += ["", f"【超时】用了 {mins} 分钟到时限。已确认：{sure}。停在：{b.get('stopped_at') or '没记到'}。手机上已发生的操作：{b.get('leftover') or '没有记录'}。"]
    for k, name in (("todo", "没做完"), ("why", "为什么不算完成"), ("leftover", "手机上留下的")):
        if b.get(k) and not (k == "why" and ok) and not (k == "leftover" and b.get("timeout")):
            out += ["", f"【{name}】{b[k]}"]
    tail = [f"要接着做或改要求，直接在这里回我。", f"—— {foot}"]
    if st.get("text"):
        tail.append(f"小结：{st['text']}")
    return "\n".join(out + [""] + tail)


def decide_push(job, c, title, facts, opts=("知道了", "要改::写清哪里要改，回到发任务的会话里说")):
    """推 decide 卡，失败重试一次；返回是否成功。decide 的 --fact 只认「键=值」。"""
    for n in (1, 2):
        try:
            r = subprocess.run([c["decide"], "q", "-p", job["project"], "--pname", c["decide_pname"],
                                *sum((["--fact", f] for f in facts), []), title, *opts, "--rec", "1"],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
            log(job, f"decide 推卡 rc={r.returncode} {(r.stdout.strip().splitlines() or [''])[-1][:80]} {r.stderr.strip()[-200:]}")
            if r.returncode == 0:
                return True
        except Exception as e:
            log(job, f"decide 推送失败：{e}")
        if n == 1: time.sleep(3)
    return False


def deliver(job, c, ok, answer, extra):
    """交付一个做完的任务的一次尝试：该推的 decide 卡先推，再发一次飞书。返回 feishu() 的 (状态, 说明)；--notify none 的返回 skip。
    重试、记账、兜底卡都在 deliver_due，不发 imsg。三态：✅ 和 🟡 只发飞书；❌（含付款痕迹）、⏹ 和真要用户拍板的才推 decide。"""
    head, line, label, items, b, foot = brief_parts(job, ok, answer)
    st = dstate(job)
    # 标题 ≤150 字：结论只取前 60 字放标题，整段放进 --fact（11-09，之前 220 字的结论整段塞进标题）
    title = clip(f"{head.split(' ', 1)[1].split('，')[0]} · {clip(job['title'], 40)}：{clip(line, 60)}", 150)
    facts = [f"{label}{i}={clip(s, 200)}" for i, s in enumerate(items, 1)]
    if len(" ".join(line.split())) > 60:
        facts.insert(0, f"结论={clip(line, 400)}")
    facts += [f"{n}={clip(b[k], 300)}" for k, n in (("todo", "没做完"), ("why", "为什么"), ("leftover", "手机上留下")) if b.get(k)]
    task = " ".join(job["task"].split())  # 任务原文可能上千字带换行，补充信息里只留开头，全文 rmq show 看
    facts += [f"任务={job['id']}：{task[:120]}{'…（全文 rmq show ' + job['id'] + '）' if len(task) > 120 else ''}", f"复核细节={clip(extra, 400)}"]
    # 办好了只走飞书（11-04 用户：「收到收到」太烦）。11-09 起 🟡 部分完成也只走飞书；
    # 只有 ❌ / 付款痕迹 / 叫停 / 驾驶要用户拍板的才上 decide 板。要恢复旧行为在配置里设 decide_on_success: true
    tier = b.get("tier") or ("stop" if job.get("status") == "stopped" else "ok" if ok else "fail")
    if c.get("decide") and not st.get("decide_done") and (tier in ("fail", "stop") or b.get("paid") or b.get("ask_user") or c.get("decide_on_success")):
        if decide_push(job, c, title, facts):
            dsave(job, decide_done=True, decide_err=None)
        else:   # 没推上：下次送结果时再推；飞书一次就送达的话不再推，送达说明里写「decide 卡没推上」
            dsave(job, decide_err=now()); log(job, "decide 卡没推上（已重试一次）")
    # 任务在 mini 上排队时收件人可能是空的（mini 配置没写 notify）：交付时退回本机配置的收件人
    if job.get("quiet"):  # --notify none 的任务不发飞书
        log(job, "结果不需要发：这单是 --notify none")
        return "skip", "--notify none"
    return feishu(c, job.get("notify") or c.get("notify") or "", brief_msg(job, ok, answer), job, what="结果")


def feishu_selftest(args):
    """rmq feishu-selftest [话]：往平时的收件人发一条「【自检】飞书送达测试」，报飞书确认没有、用时多少（只试一次）。
    记一行到 ~/.rmq/feishu-selftest.log。只在发飞书的那台机器（本机 Mac）上用。"""
    c = cfg()
    if not c.get("remote") and c.get("deliver") == "relay":
        sys.exit("这台机器不发飞书（deliver=relay），到本机 Mac 上跑 rmq feishu-selftest")
    text = " ".join(args).strip() or "飞书送达测试"
    msg = (text if text.startswith("【自检】") else "【自检】" + text) + f"（{now()}）"
    fs, detail = feishu(c, c.get("notify") or "", msg)
    line = f"{now()} {fs} {detail}"
    with open(HOME / "feishu-selftest.log", "a") as f:
        f.write(line + "\n")
    print({"ok": "✓ 飞书确认收到", "skip": "· 没发", "timeout": "✗ 超时没送到", "fail": "✗ 没送到"}[fs] + f"：{detail}（等待上限 {int(c.get('feishu_timeout') or FEISHU_TIMEOUT)} 秒）")
    sys.exit(0 if fs == "ok" else 1)


def deliver_gave_up(job, c, fs, detail, n):
    """自动重试用完还没送到：照原来的兜底推一张 decide「飞书没送达」卡（每单只推一张）。返回一句说明，推没推上如实写。"""
    head, line = brief_parts(job, job.get("status") == "done", job.get("answer", ""))[:2]
    if dstate(job).get("fail_card"):
        return "decide 失败卡之前推过"
    if not c.get("decide"):
        log(job, "本机没配 decide，失败卡没推"); return "本机没配 decide，失败卡没推"
    why = f"自动重试 {n} 次都没送到，最后一次{'超时' if fs == 'timeout' else '发送失败'}：{clip(detail, 120)}"
    if decide_push(job, c, clip(f"飞书没送达 · 任务 {job['id']} · {head.split(' ', 1)[1].split('，')[0]} · {clip(job['title'], 30)}", 150),
                   [f"原因={why}", f"结论={clip(line, 300)}", f"看全文=rmq show {job['id']}；补发 rmq redeliver {job['id']}"], ("知道了",)):
        dsave(job, fail_card=True)
        log(job, f"飞书最终没送到（{fs}），已推 decide 失败卡"); return "已推 decide 失败卡"
    dsave(job, fail_card_err=now())
    log(job, f"飞书最终没送到（{fs}），推 decide 失败卡也没成功"); return "decide 失败卡也没推上"


LANES, LANES_LOCK = {}, threading.Lock()


def lane_run(jid, fn, *a):
    """每单一条发送线程：这单上一条还没发完，这次就不发（下一轮再看），同一单的消息不乱序；别的单和心跳不用等它。返回是否起了线程。"""
    with LANES_LOCK:
        for k in [k for k, t in LANES.items() if not t.is_alive()]:
            del LANES[k]
        if jid in LANES:
            return False
        t = threading.Thread(target=fn, args=a, daemon=True, name=f"send-{jid}")
        LANES[jid] = t; t.start()
    return True


class job_lock:
    """同一单的交付在 relay 的发送线程和 rmq redeliver 之间互斥（文件锁，跨进程）。"""
    def __init__(self, job):
        self.p = JOBS / job["id"] / "deliver.lock"

    def __enter__(self):
        self.p.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.p, "w"); fcntl.flock(self.f, fcntl.LOCK_EX); return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN); self.f.close()


def deliver_due(c, job, mark):
    """送一个做完任务的结果（在这单的发送线程里跑）。mark(状态, 说明) 把结论写回任务所在机器的 job.json，返回是否写成。
    第 1 次马上发；没送到隔 DELIVER_GAPS（30 秒、2 分钟、5 分钟）再试，共 4 次。重试期间任务本身的状态不动（办成了就是办成了），
    送达状态是「待送达」，每次没送到的原因写回去。4 次都没送到：记「没送到」，照原来的兜底推一张 decide 失败卡，不再自动重试，
    等人 rmq redeliver。返回这次之后的送达状态。"""
    if time.time() < (dstate(job).get("next_try") or 0):
        return "pending"
    with job_lock(job):
        st = dstate(job)   # 拿到锁再读一次：rmq redeliver 可能刚发完
        if st.get("final"):   # 本机已有结论，只是上次没写回去（ssh 断了）：只补写，不重发
            if mark(st["final"], st.get("final_note", "")): dsave(job, final_marked=now())
            return st["final"]
        n = int(st.get("tries") or 0) + 1
        try:
            fs, detail = deliver(job, c, job["status"] == "done", job.get("answer", ""), job.get("detail", ""))
        except Exception as e:   # 拼消息之类的程序错误：按发送失败记，照样排重试，不让发送线程悄悄死掉
            fs, detail = "fail", f"交付出错：{scrub(e, 200)}"; log(job, detail)
        if fs in ("timeout", "fail") and n <= len(DELIVER_GAPS):
            gap = DELIVER_GAPS[n - 1]
            note = f"{now()} 第 {n} 次没送到（{'超时' if fs == 'timeout' else '失败'}：{clip(detail, 160)}），{gap} 秒后自动再试"
            dsave(job, tries=n, next_try=time.time() + gap, last_err=clip(detail, 300), last_try=now())
            log(job, f"结果第 {n} 次没送到，{gap} 秒后自动再试（共试 {len(DELIVER_GAPS) + 1} 次）")
            mark("pending", note)
            return "pending"
        if fs == "ok":
            state, note = "delivered", f"{now()} 第 {n} 次送达（{detail}）"
        elif fs == "skip":
            state, note = "not_needed", f"{now()} {detail}"
        else:
            card = deliver_gave_up(job, c, fs, detail, n)
            state, note = "failed", (f"{now()} 自动重试 {n} 次都没送到（最后一次{'超时' if fs == 'timeout' else '失败'}：{clip(detail, 160)}）；"
                                     f"{card}；补发 rmq redeliver {job['id']}")
        if dstate(job).get("decide_err") and not dstate(job).get("decide_done"):
            note += "；decide 卡没推上"
        dsave(job, tries=n, final=state, final_note=note, final_at=now(), next_try=None)
        log(job, f"结果送达状态：{DSTATES[state]}（{note}）")
        if mark(state, note): dsave(job, final_marked=now())
        return state


def lease_hold(c):
    """任务占手机租约的时长：每轮驾驶时限 × 轮数，再留 15 分钟给预检、复核和收尾。"""
    return int(c["max_runtime"]) * int(c["attempts"]) + 900


def run_job(job, c):
    """返回 "busy" 表示手机正被别的调用方占着，任务留在队列里稍后再试。"""
    lk = kp(c, job, "lock", str(lease_hold(c)), "--note", f"rmq 任务「{job['title']}」")
    if lk.returncode == 75:
        why = lk.stderr.strip()
        if job.get("waiting") != why:
            log(job, "手机被占用，排队等：" + why); update(job, waiting=why)
        return "busy"
    try:
        return run_held(job, c)
    finally:
        kp(c, job, "unlock")


def bridge_fail_msg():
    """开工前读不到手机画面时，按 bridge.log 末尾判断真正的原因，不再一律说 GLKVM 掉登录。"""
    login = "看不到手机画面：GLKVM 可能掉登录了。请在 ego lite 里打开 https://glkvm.local 登录，能看到手机后再重发任务。"
    tail = bridge_log_tail()
    if tail is None:
        return login
    if "GLKVM_CERT_CHANGED" in tail:  # glkvm_cert.mjs：指纹和登记的不一致，拒绝放行
        return ("手机桥起不来：GLKVM 的证书和登记的指纹不一致，已停下没放行（设备重置过会换证书，也可能有人冒充）。"
                "确认是设备自己换的后，在 Mac 终端运行 kpcert 重新登记，再重发任务。")
    if "ERR_CERT" in tail:
        return "手机桥起不来：ego 浏览器对 GLKVM 自签证书的放行过期了，自动续期也没成功（不是掉登录）。在 Mac 终端运行 kpcert 看原因。"
    if re.search(r"login|GLKVM_LOGIN", tail, re.I):
        return login
    lines = [l.strip() for l in tail.splitlines() if l.strip() and not l.startswith(BRIDGE_SEP)]
    errs = [l for l in lines if re.search(r"err|fail|exception|refused|timeout|无法|失败", l, re.I)]
    last = (errs or lines or [""])[-1]
    last = re.sub(r"(?i)(token|auth|key|pass(word)?|cookie|secret|session)[\"'\s]*[=:][\"'\s]*\S+", r"\1=***", last)
    last = re.sub(r"[A-Za-z0-9_\-+/=.]{24,}", "***", last)[:120]
    return "手机桥起不来" + (f"：{last}" if last else "") + "。"


# ───────────── 预检读屏失败时自动重启手机桥（11-12）─────────────
# 11-12 15:32 一单预检读不到画面直接失败，3 分钟后手工重启桥、同一任务重发就好了。现在预检失败先自动重启一次桥再预检；
# 证书类失败（glkvm_cert.mjs 拒绝放行 / 浏览器报 ERR_CERT）要人来处理，不重启。重启前的现场存进任务目录。
KPRUN = Path("/tmp/kvmphone")   # 桥的运行目录：bridge.log、reload.log、cert_mismatch.json、bridge.owner
BRIDGE_SEP = "===== kp start"   # 和 kp.py 的 LOG_SEP 一致：每次起桥在 bridge.log 里写的分隔行开头
BRIDGE_WAIT = 40   # 重启桥后等探活正常的上限（秒）
CERT_MARKS = ("GLKVM_CERT_CHANGED", "ERR_CERT", "证书放行后页面仍打不开")


def bridge_log_tail(n=6000):
    """bridge.log 里本次起桥以来的末尾 n 字；读不到返回 None。11-12 起日志追加不清空，旧桥的报错不能拿来判这次的原因。"""
    try:
        with open(KPRUN / "bridge.log", "rb") as fp:
            fp.seek(0, 2); fp.seek(max(fp.tell() - 65536, 0)); t = fp.read().decode("utf-8", "ignore")
    except OSError:
        return None
    i = t.rfind(BRIDGE_SEP)
    return (t[i:] if i >= 0 else t)[-n:]


def text_tail(f, lines=50):
    try:
        return "".join(Path(f).read_text(errors="ignore").splitlines(True)[-lines:])
    except OSError as e:
        return f"（读不到：{e}）\n"


def bridge_cert_problem(look_err=""):
    """预检失败是不是证书类原因：返回命中的标记，不是返回空串。reload.log 只看最近 10 分钟（这个文件一直追加）。"""
    if (KPRUN / "cert_mismatch.json").exists():
        return "cert_mismatch.json"
    recent = []
    cut = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)
    for ln in text_tail(KPRUN / "reload.log", 200).splitlines():
        try:
            if datetime.datetime.fromisoformat(ln.split(" ", 1)[0].replace("Z", "+00:00")) >= cut:
                recent.append(ln)
        except ValueError:
            pass
    text = "\n".join([bridge_log_tail() or "", look_err or "", *recent])
    return next((k for k in CERT_MARKS if k in text), "")


def bridge_health(port):
    """直接问桥的 /health（不经 kp，免得桥没起时 kp 自己又去拉起一个）。返回 (ok, 原文)。"""
    import urllib.request
    try:
        raw = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3).read().decode("utf-8", "ignore")
        try: return bool(json.loads(raw).get("ok")), raw[:300]
        except ValueError: return False, raw[:300]
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def kp_bridge(c, job, *a, timeout=60):
    """起停桥用的 kp：不带任务目录，免得把 rmq say 的插话提前消费掉（kpx.steer 读到就算转达过了）。"""
    env = {k: v for k, v in kenv(job).items() if k not in ("KP_JOB_DIR", "KP_JOB_ID", "KP_NOTIFY")}
    return subprocess.run([c["kp"], *a], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, env=env)


def redact(s):
    return re.sub(r"(?i)(token|auth|key|pass(word)?|cookie|secret|session)([\"'\s]*[=:][\"'\s]*)\S+", r"\1\3***", s)


def bridge_snapshot(job, port, look, look_err, f):
    """重启桥之前的现场：预检最后一次的输出、探活返回、桥进程、桥日志与 reload.log 的末尾。"""
    ok, health = bridge_health(port)
    try:
        ps = subprocess.run(["pgrep", "-fl", "bridge.mjs"], capture_output=True, text=True, timeout=10).stdout.strip()
        own = (KPRUN / "bridge.owner").read_text(errors="ignore").strip() if (KPRUN / "bridge.owner").exists() else "（无）"
    except Exception as e:
        ps, own = f"（取不到：{e}）", "?"
    blog = bridge_log_tail(20000)
    with open(f, "a") as out:
        out.write(f"===== {now()} 任务 {job['id']} 预检读屏失败，自动重启手机桥前的现场 =====\n"
                  f"== 预检最后一次 kp look：{look_err}\n{redact((look or '（没有输出）')[:3000])}\n"
                  f"== 探活 GET 127.0.0.1:{port}/health → {'ok' if ok else '不正常'}\n{health}\n"
                  f"== 桥进程（pgrep -fl bridge.mjs）\n{ps or '（没有）'}\n== bridge.owner：{own}\n"
                  f"== cert_mismatch.json：{'有' if (KPRUN / 'cert_mismatch.json').exists() else '无'}\n"
                  f"== {KPRUN}/bridge.log 本次起桥以来的末尾\n{redact(blog) if blog is not None else '（读不到）'}\n"
                  f"== {KPRUN}/reload.log 末尾 50 行\n{redact(text_tail(KPRUN / 'reload.log', 50))}\n")


def bridge_restart(job, c, look, look_err):
    """存现场 → kp stop → kp start → 等 /health 正常（≤ BRIDGE_WAIT 秒）。返回 (探活是否正常, 说明)。"""
    d = JOBS / job["id"]
    port = 7690
    try:
        port = json.load(open(DEVF))["devices"][job.get("dev", "rmq1")].get("port", 7690)
    except Exception:
        pass
    f = d / "bridge-before-restart.log"
    try:
        bridge_snapshot(job, port, look, look_err, f)
    except Exception as e:
        log(job, f"存现场出错（照样重启桥）：{e}")
    t0 = time.time()
    broken = False   # kp 本身起不来（不是桥慢）：不管探活怎样都算重启没成功
    try:
        r = kp_bridge(c, job, "stop", timeout=30)
        stop_out = (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        stop_out = "kp stop 30 秒超时"
    except OSError as e:   # kp 文件缺失、没有执行权限之类：当重启没成功，任务照原样按预检失败收尾
        stop_out = f"kp stop 起不来：{e}"; broken = True
    time.sleep(1)
    # 桥的输出常常要等进程退出才落进 bridge.log：停桥之后再补一段
    try:
        with open(f, "a") as out:
            out.write(f"== {now()} kp stop：{stop_out}\n== 停桥后 {KPRUN}/bridge.log 本次起桥以来的末尾\n{redact(bridge_log_tail(8000) or '（读不到）')}\n")
    except OSError:
        pass
    try:
        r = kp_bridge(c, job, "start", timeout=BRIDGE_WAIT)
        start_out = (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        start_out = f"kp start {BRIDGE_WAIT} 秒超时"
    except OSError as e:
        start_out = f"kp start 起不来：{e}"; broken = True
    ok, health = bridge_health(port)
    while not ok and time.time() - t0 < BRIDGE_WAIT:
        time.sleep(1); ok, health = bridge_health(port)
    why = (f"kp stop：{clip(stop_out, 80)}；" if broken else "") + \
        f"kp start：{clip(start_out, 80)}；探活 {'正常' if ok else '不正常'}（{clip(health, 80)}），重启用时 {int(time.time() - t0)}s"
    try:
        with open(f, "a") as out:
            out.write(f"== {now()} {why}\n")
    except OSError:
        pass
    return ok and not broken, why


def precheck(job, c, fake_fail=False):
    """开工前确认看得到手机画面：读不到隔 5 秒再读一次（11-10 一单 1 秒内瞬时读不到，紧接着的任务正常）。
    返回 (通过?, 最后一次 look 输出, 错误说明)。fake_fail = 自检开关：不读屏，直接按失败处理。"""
    if fake_fail:
        log(job, f"自检开关 {SELFTEST_FLAG}：这次预检不读屏，直接按失败处理")
        return False, "", "自检开关造成的失败"
    look, look_err = "", ""
    for i in range(2):
        try:
            r0 = kp(c, job, "look", timeout=120); look, look_err = r0.stdout, f"rc={r0.returncode} " + (r0.stderr or "").strip()[-200:]
        except subprocess.TimeoutExpired:
            look, look_err = "", "kp look 120 秒超时"
        except OSError as e:
            look, look_err = "", f"kp look 起不来：{e}"
        if re.search(r"(?m)^\d+,\d+", look):
            return True, look, look_err
        if not i:
            log(job, "预检读屏没拿到坐标，5 秒后重试一次（" + clip(look_err, 160) + "）")
            time.sleep(5)
    return False, look, look_err


# ───────────── 超时收尾轮 ─────────────
def finalize_needed(adir, rc, res):
    """驾驶被时限收走（rc 124）或撞到 max_turns，且 result.json 已有候选 / 细节、还没写成「最终」→ 要开收尾轮。"""
    if not isinstance(res, dict) or not (res.get("候选") or res.get("细节")) or res.get("阶段") == "最终":
        return False
    if rc == 124:
        return True
    try:
        tail = (adir / "driver.log").read_bytes()[-6000:].decode("utf-8", "ignore")
    except OSError:
        return False
    return bool(re.search(r'"subtype"\s*:\s*"error_max_turns"', tail))


def finalize_cmd(job, c, adir):
    """收尾轮命令行与环境：只给 Read、Write（没有 Bash，也就没有 kp）；不带 KP_* 变量，不碰手机租约。
    11-10 起 mini 不再登录 Claude，收尾轮固定在 mini 用 DeepSeek Flash（只整理已有材料，不依赖 Mac 在线）。"""
    kind = "ds"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "KP_"))}
    model = claude_setup(job, c, kind, env)
    a = str(adir)
    def tail(f, n):
        try: return "".join((adir / f).read_text(errors="ignore").splitlines(True)[-n:])
        except OSError: return ""
    dtail = ""
    try: dtail = (adir / "driver.log").read_bytes()[-1500:].decode("utf-8", "ignore")
    except OSError: pass
    prompt = (
        "你是收尾整理员。上一个驾驶在操作手机时被时限收走了，手机现在不归你管：你没有任何操作手机的工具，也不要想办法操作。\n"
        f"任务原文：{job['task']}\n\n"
        f"现有结果文件：{a}/result.json（内容见下，也可以用 Read 重读）。\n{json.dumps(jload(adir / 'result.json'), ensure_ascii=False)[:6000]}\n\n"
        f"驾驶最后几条 kp 命令记录（{a}/kp.jsonl 末尾）：\n{tail('kp.jsonl', 12)[-3500:]}\n"
        f"驾驶输出末尾：\n{dtail}\n\n"
        "要做的事：只根据上面这些材料，用 Write 工具把最终结论整份覆盖写进 result.json，字段不变，「阶段」写「最终」。规矩：\n"
        "- 副作用已经发生的（领券、关注店铺、加购物车、发消息等）照实保留在「副作用」里，不要重做，也不要删掉。\n"
        "- 不要假装做了没做的事：材料里没有证据的价格、店名、日期一律不写进答案；拿不准的写进「细节」并标「未核实」。\n"
        "- 「答案」只写已确认的结论；没查完的平台或项目写进「没做到」，说明是时限到了没做完。\n"
        "- 写完这一份就结束，不要再做别的。\n")
    cmd = [c["claude"], "-p", prompt, "--model", model, "--safe-mode", "--no-session-persistence",
           "--permission-mode", "dontAsk", "--tools", "Read,Write",
           "--allowedTools", f"Read(/{a}/**)", f"Write(/{a}/**)",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
           "--max-turns", "8", "--output-format", "stream-json", "--verbose"]
    return cmd, env


def finalize(job, c, adir, rc):
    """跑收尾轮（≤2 分钟、不碰手机），返回新的 result.json 内容；没写成合法 JSON 就恢复原文件并返回原内容。不算 attempt。"""
    rp = adir / "result.json"; bak = adir / "result.pre-finalize.json"
    old = rp.read_text()
    bak.write_text(old)
    log(job, f"驾驶{'到时限被收走' if rc == 124 else '撞到 max_turns'}，result.json 有候选，开收尾轮（≤2 分钟，不碰手机）")
    try:
        cmd, env = finalize_cmd(job, c, adir)
        frc = run_claude(cmd, env, adir, adir / "finalize.log", 120, JOBS / job["id"])
    except Exception as e:
        log(job, f"收尾轮没跑起来：{e}"); return jload(rp)
    new = jload(rp)
    if not isinstance(new, dict) or new.get("阶段") != "最终":
        rp.write_text(old)
        log(job, f"收尾轮 rc={frc} 没写出「阶段=最终」的合法 result.json，保留原结果"); return jload(rp)
    log(job, f"收尾轮完成 rc={frc}，result.json 已定稿（原稿存 result.pre-finalize.json）")
    return new


def build_brief(res, r, ok, stopped, last_rc, adir, answer):
    """把复核 / 自检结果整理成交付用的 brief：三态、金额核对、超时停点。返回 (brief, 态, 是否付款痕迹)。"""
    brief = {k: (r or {}).get(k) for k in ("headline", "label", "items", "todo", "why", "leftover") if (r or {}).get(k)}
    # 11-09 三态与交付核对：这些都得在 mini 上（result.json 在这）算好，relay 只读 job.json
    tier, paid = tri_state(res, r, ok, stopped)
    raw = ""
    try: raw = (adir / "result.json").read_text()
    except OSError: pass
    if not brief.get("items") and res:
        brief["items"] = norm_items(res)
        if brief["items"]: brief["label"] = "候选" if res.get("候选") else "要点"
    if raw:
        brief["headline"] = mark_amounts(brief.get("headline") or answer, raw)
        brief["items"] = [mark_amounts(x, raw) for x in brief.get("items") or [] if isinstance(x, str)]
    brief["tier"] = tier
    if paid: brief["paid"] = True
    if isinstance(res, dict) and res.get("需用户拍板"): brief["ask_user"] = True
    if last_rc == 124 and tier in ("part", "fail"):
        brief["timeout"] = True
        brief["stopped_at"] = stop_point(adir)
        if not brief.get("leftover") and isinstance(res, dict) and res.get("副作用"):
            side = res["副作用"]; brief["leftover"] = "；".join(map(str, side)) if isinstance(side, list) else str(side)
    return brief, tier, paid


def mute_host(c):
    """跑手机任务的这台机（mini）不许出声（11-10 用户定「手机要静音」）：iPhone 经 HDMI 进 GLKVM，桥的页面把手机声音放到 mini 喇叭；
    iPhone 走 HDMI 时音量锁最大，只能在 mini 这头静音。把输出与提示音设备切到虚拟设备（默认 BlackHole 2ch），
    接电视 / 重启后系统可能切回喇叭，所以 worker 启动和每单开跑前各查一次。配置 mute_audio_device 设空串关掉。"""
    dev = c.get("mute_audio_device", "BlackHole 2ch")
    sas = shutil.which("SwitchAudioSource") or "/opt/homebrew/bin/SwitchAudioSource"
    if not dev or sys.platform != "darwin" or not os.path.exists(sas):
        return
    for kind in ("output", "system"):
        try:
            cur = subprocess.run([sas, "-c", "-t", kind], capture_output=True, text=True, timeout=10).stdout.strip()
            if cur != dev:
                subprocess.run([sas, "-t", kind, "-s", dev], capture_output=True, text=True, timeout=10)
                print(f"[静音] {kind} 设备 {cur or '?'} → {dev}", flush=True)
        except Exception as e:
            print(f"[静音] 切 {kind} 设备失败：{str(e)[:120]}", flush=True)


def run_held(job, c):
    d = JOBS / job["id"]
    update(job, status="running", started=now(), waiting=None)
    mute_host(c)
    hint, last = "", None
    # 先确认看得到手机画面，GLKVM 掉登录时立刻报，不白跑二十分钟。
    # 11-12：GLKVM 手机读不到时先自动重启一次手机桥再预检（证书类失败除外）。预检和重启都在驾驶开始之前，
    # 不占驾驶的时限（max_runtime 从每轮驾驶开跑才计时）；重启后按原时长重新拿一次租约，把恢复用掉的时间补回去。
    t_pre = time.time()
    ok_, look, look_err = precheck(job, c, fake_fail=job.get("selftest") == "precheck-fail")
    restarted = False
    glkvm = job.get("dev", "rmq1") == "rmq1"
    if not ok_ and glkvm and not job.get("bridge_restarted") and not (d / "stop").exists():
        cert = bridge_cert_problem(look_err)
        if cert:
            log(job, f"预检失败像是证书问题（{cert}），要人来处理，不自动重启手机桥")
        else:
            log(job, "预检读屏没读到画面，自动重启手机桥再试（每个任务最多一次；重启前的现场存进 bridge-before-restart.log）")
            restarted = True; update(job, bridge_restarted=now())
            ok_b, why = bridge_restart(job, c, look, look_err)
            broken = not ok_b and "起不来" in why   # kp 本身起不来：重启没成功（桥只是慢的照旧写「已重启」，探活结果在说明里）
            log(job, f"手机桥{'重启没成功' if broken else '已重启'}：{why}")
            if broken: restarted = "没成功：" + clip(why, 120)
            ok_, look, look_err = precheck(job, c)
            log(job, f"重启手机桥后重新预检{'通过' if ok_ else '仍失败'}（预检加恢复共用 {int(time.time() - t_pre)}s，不算进驾驶时限）")
            try:
                lk = kp(c, job, "lock", str(lease_hold(c)), "--note", f"rmq 任务「{job['title']}」")
                lk_rc, lk_err = lk.returncode, (lk.stderr or "").strip()
            except (subprocess.TimeoutExpired, OSError) as e:
                lk_rc, lk_err = -1, str(e)
            if lk_rc == 75:   # 和 run_job 开头一样：手机被别的调用方占着就回队列等，不抢（这单已重启过桥，回来不再重启）
                log(job, "重启手机桥后重新占用手机时被占用，回队列排队等：" + lk_err)
                update(job, status="queued", waiting=lk_err)
                return "busy"
            if lk_rc != 0:
                log(job, f"重启手机桥后重新占用手机没成功（rc={lk_rc}：{clip(lk_err, 120)}），用开工时拿的租约接着做")
    if not ok_:
        log(job, "预检读屏仍失败（" + clip(look_err, 160) + "）")
        msg = (bridge_fail_msg() if glkvm else f"看不到 {job['dev']} 的手机画面：查盒子 / 服务端是否在线（kp --dev {job['dev']} health）。")
        if restarted is True:
            msg = msg.rstrip("。") + "。已自动重启过一次手机桥，还是读不到画面。"
        elif restarted:
            msg = msg.rstrip("。") + f"。试着自动重启手机桥，{restarted}。"
        log(job, "预检失败 " + msg)
        last = (d, None, {"pass": False, "answer": msg, "issues": [msg]})
    adirs, last_rc = [], None
    for k in range(1, (0 if last else int(c["attempts"])) + 1):
        if (d / "stop").exists():
            break
        adir = d / f"attempt{k}"; adir.mkdir(exist_ok=True); adirs.append(adir)
        log(job, f"第 {k} 轮驾驶开始（{driver_label(job, c)}）")
        kp(c, job, "key", "Meta+KeyH", "-q")
        t0 = time.time(); rc = drive(job, c, adir, hint); last_rc = rc
        res = jload(adir / "result.json")
        log(job, f"驾驶结束 rc={rc} 用时 {int(time.time()-t0)}s result.json={'有' if res else '无'}")
        if not res:
            hint = "上一轮没有写出合法的 result.json，务必在结束前写。"; last = (adir, None, None); continue
        if rc == 124 and k < int(c["attempts"]) and not (d / "stop").exists():  # 到时间被收走：下一轮接着做，不复核、不从头来
            hint = ("上一轮到时间被收走了。下面是它已经拿到的结果，先把这些原样写进你的 result.json，"
                    "再只做还没做完的部分，已经查过的 App 和商品不要再查：" + json.dumps(res, ensure_ascii=False)[:2500])
            log(job, "到时间但有部分结果，下一轮接着做"); last = (adir, res, None); continue
        if finalize_needed(adir, rc, res) and not (d / "stop").exists():
            res = finalize(job, c, adir, rc) or res
        if c.get("review", True) is False:
            r = selfcheck(job, adir, res, rc)
        else:
            update(job, status="reviewing")
            r = review(job, c, adir)
        log(job, f"{'自检' if c.get('review', True) is False else '复核'} pass={r.get('pass')} {'; '.join(r.get('issues', []))[:300]}")
        last = (adir, res, r)
        # 驾驶记下的 App 事实（Mika 的 App 知识自学）：越界的轮次不收
        if r.get("boundary") in (None, "", "无") and isinstance(res.get("学到"), list):
            got = [n for f in res["学到"][:8] if isinstance(f, str) and (n := kpx.learn(f))]
            if got: log(job, f"记进 App 说明 {len(got)} 条：{'、'.join(sorted(set(got)))}")
        if r.get("pass") or (d / "stop").exists(): break
        hint = r.get("retry_hint") or "; ".join(r.get("issues", []))
    kp(c, job, "key", "Meta+KeyH", "-q")
    if last is None:  # 第一轮开跑前就被叫停
        last = (d, None, {"pass": False, "answer": "开跑前被叫停", "issues": []})
    adir, res, r = last
    stopped = (d / "stop").exists()
    ok = bool(r and r.get("pass")) and not stopped
    answer = (r or {}).get("answer") or (res or {}).get("答案") or "没有拿到结果"
    st = stats(job, adirs)
    extra = "；".join(filter(None, [
        "叫停：" + (d / "stop").read_text().strip() if stopped else "",
        ("复核通过" if c.get("review", True) is not False else "一遍过，未复核") if ok else
        ("" if stopped else ("复核" if c.get("review", True) is not False else "自检") + "未通过：" + "；".join((r or {}).get("issues", []))[:300]),
        "越界：" + r["boundary"] if r and r.get("boundary") not in (None, "", "无") else "",
        "没做到：" + res["没做到"] if res and res.get("没做到") else "",
        job.get("driver_note") or "",
        st["text"],
    ]))
    log(job, "小结 " + st["text"])
    brief, tier, paid = build_brief(res, r, ok, stopped, last_rc, adir, answer)
    log(job, f"交付判定 {TIER_HEAD[tier]}{'（付款痕迹）' if paid else ''}")
    update(job, status="stopped" if stopped else ("done" if ok else "failed"), finished=now(), answer=answer, detail=extra,
           result=str(adir / "result.json"), delivered=False, delivery="pending", delivery_note="", stats=st, brief=brief)
    # 交付：deliver=relay 时本机 relay 来取；自己交付的由 worker 主循环的发送线程送（local_deliveries），没送到同样按 DELIVER_GAPS 重试


def run_one(job, retry):
    dev = job.get("dev", "rmq1")
    with open(f"/tmp/kvmphone/{dev}.rmq.lock", "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)   # 同一台手机一次一个任务（也挡住并发的 worker --once）
        if (jload(JOBS / job["id"] / "job.json") or {}).get("status") != "queued":
            return   # 等锁期间被取消了
        try:
            if run_job(job, cfg()) == "busy":
                retry[job["id"]] = time.time() + 30
        except Exception as e:
            update(job, status="failed", finished=now(), detail=f"worker 出错：{e}", answer="worker 出错，任务没跑起来", delivered=False, delivery="pending")
            log(job, f"出错：{e}"); import traceback; log(job, traceback.format_exc()[-1500:])


def worker(args):
    once = "--once" in args
    JOBS.mkdir(parents=True, exist_ok=True); Path("/tmp/kvmphone").mkdir(parents=True, exist_ok=True)
    # 重启后把卡在 running / reviewing 的任务标成失败，防止永远挂着
    for j in jobs():
        if j["status"] in ("running", "reviewing"):
            j.update(status="failed", detail="worker 重启时任务中断", answer="worker 重启，任务中断", delivered=False, delivery="pending"); jsave(JOBS / j["id"] / "job.json", j)
            for req in (JOBS / j["id"]).glob("attempt*/drive.req"):  # Gemini 在本机还开着：叫它停，别接着动手机（11-01 重启后它又独自跑了半小时）
                if not (req.parent / "drive.done").exists():
                    (req.parent / "drive.kill").touch()
    running, retry = {}, {}   # 设备 → 线程；任务 id → 下次重试时间（手机被占时）
    c = cfg()
    if not c.get("remote"):  # 真接手机的那台（mini）才静音；本机 Mac relay 不动
        mute_host(c)
    if c.get("gemini_local") and not once:  # mini 本机跑 Gemini 驾驶：在 worker 里养一个预热好的 agy（省约 12 秒启动）
        warm_cleanup()
        threading.Thread(target=warm_keeper, args=(c,), daemon=True).start()
    while True:
        for j in jobs():
            dev = j.get("dev", "rmq1")
            if j["status"] != "queued" or retry.get(j["id"], 0) > time.time():
                continue
            if running.get(dev) and running[dev].is_alive():
                continue
            if once:
                run_one(j, retry)
                if c.get("deliver") != "relay" and (j2 := jload(JOBS / j["id"] / "job.json")) and pending_delivery(j2):
                    deliver_due(c, j2, local_mark(j2["id"]))   # --once：只发一次，不等重试
                return
            running[dev] = threading.Thread(target=run_one, args=(j, retry), daemon=True)
            running[dev].start()
        if once:
            return
        if c.get("deliver") != "relay":
            local_deliveries(c)
        time.sleep(5)


def pending_delivery(j):
    """做完了、结果还要去送的任务：新版本记的「待送达」；旧版本写的 delivered=False（没有 delivery 字段）也算。"""
    return j.get("status") in FINISHED and (j.get("delivery") == "pending" or ("delivery" not in j and j.get("delivered") is False))


def undelivered(args):
    """（任务所在机器）relay 取活：待送达的任务。--all 再加上「没送到」的（自动重试用完、等人补发），给 doctor 和 rmq redeliver 用。"""
    print(json.dumps([j for j in jobs() if pending_delivery(j) or ("--all" in args and j["status"] in FINISHED and j.get("delivery") == "failed")],
                     ensure_ascii=False))


def set_delivery(jid, state, note=""):
    """把送达结论写进 job.json（delivered 布尔只在「已送达」时为 True），同时记一行 events.log。返回是否写成。"""
    j = jload(JOBS / jid / "job.json")
    if not j:
        return False
    j.update(delivery=state, delivered=state == "delivered", delivery_note=note[:400], delivery_at=now())
    jsave(JOBS / jid / "job.json", j)
    log(j, f"结果送达状态记为「{DSTATES[state]}」" + (f"：{note[:300]}" if note else ""))
    return True


def mark(args):
    """rmq mark <id> [状态] [说明]：relay 把送达结论写回任务所在机器。状态 delivered / not_needed / pending / failed；
    不写状态按 delivered（旧版 relay 的写法）。"""
    if not args or not re.fullmatch(r"[\w-]+", args[0]): sys.exit("用法：rmq mark <任务id> [delivered|not_needed|pending|failed] [说明]")
    state = args[1] if len(args) > 1 else "delivered"
    if state not in DSTATES: sys.exit(f"rmq mark：状态只能是 {' / '.join(DSTATES)}")
    if not set_delivery(args[0], state, " ".join(args[2:])): sys.exit("没有这个任务")


def jobjson(args):
    """（任务所在机器）按任务号取 job.json，给本机 rmq redeliver 用。"""
    print(json.dumps([j for a in args if re.fullmatch(r"[\w-]+", a) and (j := jload(JOBS / a / "job.json"))], ensure_ascii=False))


def local_mark(jid):
    return lambda state, note: set_delivery(jid, state, note)


def local_deliveries(c):
    """自己交付（没配 relay）时，worker 主循环每 5 秒调一次：待送达的任务各在自己的发送线程里送。"""
    for j in jobs():
        if pending_delivery(j):
            lane_run(j["id"], deliver_due, c, j, local_mark(j["id"]))


def legacy_failed(jid):
    """改版前（11-12 夜之前）的任务：relay 最后一次往飞书发是失败 / 超时 / 放弃（events.log 里看），算历史没送到。
    当时 mini 上照样标成了 delivered=True，所以只能按本机 relay 的记录推断。"""
    f = JOBS / jid / "events.log"
    if not f.exists():
        return False
    last = None
    for ln in f.read_text(errors="ignore").splitlines():
        if "飞书推送 rc=0" in ln:
            last = "ok"
        elif re.search(r"飞书最终没送达|飞书推送失败|飞书发送超时|飞书推送 rc=[1-9]", ln):
            last = "fail"
    return last == "fail"


def delivery_state(job):
    """(送达状态, 是否改版前的推断)。新版本看 job.json 的 delivery；改版前的看本机 relay 的记录。"""
    if job.get("delivery") in DSTATES:
        return job["delivery"], False
    st = dstate(job) if (JOBS / job["id"]).exists() else {}
    if st.get("final") in DSTATES:
        return st["final"], False
    if job.get("quiet"):
        return "not_needed", True
    if legacy_failed(job["id"]):
        return "failed", True
    return ("delivered" if job.get("delivered") else "pending"), True


def legacy_undelivered():
    """本机 relay 记录里改版前没送到的结果：[(任务 id, 标题, 结束时间)]。只列出来，不自动补发。"""
    out = []
    for d in sorted(JOBS.iterdir()) if JOBS.exists() else []:
        j = jload(d / "job.json")
        if j and j.get("status") in FINISHED and "delivery" not in j and not dstate(j).get("final") and not j.get("quiet") and legacy_failed(j["id"]):
            out.append((j["id"], j.get("title", ""), j.get("finished") or ""))
    return out


def remote_json(r, *cmd):
    p = subprocess.run(["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", r, " ".join(["~/.local/bin/rmq", *map(shlex.quote, cmd)])],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"ssh {r} 退出码 {p.returncode}：{p.stderr.strip()[-200:]}")
    return json.loads(p.stdout or "[]")


def remote_mark(r, jid):
    """relay 用：把送达结论经 ssh 写回 mini 的 job.json。返回是否写成（没写成下一轮只补写，不重发）。"""
    def m(state, note):
        try:
            p = subprocess.run(["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", r, "~/.local/bin/rmq", "mark", jid, state, shlex.quote(note)],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
            return p.returncode == 0
        except Exception:
            return False
    return m


def redeliver(args):
    """rmq redeliver <任务id…> | --all [--force]：把没送到的结果重新往飞书发一次（只发飞书，不推 decide 卡，消息和原来一样）。
    已送达、不需要发的不重发，加 --force 才发（--notify none 的照样不发）。--all 只包括 11-12 改版之后记成「没送到 / 待送达」的；
    改版前的历史未送达（rmq doctor 里列的）要逐个写任务号。发一次就报结果，没送到不自动重试。"""
    c = cfg(); r = c.get("remote")
    force, every = "--force" in args, "--all" in args
    ids = [a for a in args if not a.startswith("-")]
    if not ids and not every:
        sys.exit("用法：rmq redeliver <任务id…> | --all [--force]")
    if not r and c.get("deliver") == "relay":
        sys.exit("这台机器的结果由另一台机器的 rmq relay 送（deliver=relay），到那台机器上跑 rmq redeliver")
    if r:
        got = remote_json(r, "undelivered", "--all") if every else []
        got += remote_json(r, "jobjson", *ids) if ids else []
        mark_fn = lambda jid: remote_mark(r, jid)
    else:
        got = [j for j in jobs() if pending_delivery(j) or (j["status"] in FINISHED and j.get("delivery") == "failed")] if every else []
        got += [j for a in ids if re.fullmatch(r"[\w-]+", a) and (j := jload(JOBS / a / "job.json"))]
        mark_fn = local_mark
    seen, sent = set(), 0
    for job in got:
        jid = job["id"]
        if jid in seen: continue
        seen.add(jid)
        state, legacy = delivery_state(job)
        if job.get("status") not in FINISHED:
            print(f"{jid} 还没做完（{job.get('status')}），不发"); continue
        if job.get("quiet"):
            print(f"{jid} 是 --notify none 的任务，不发"); continue
        if state in ("delivered", "not_needed") and not force:
            print(f"{jid} {DSTATES[state]}，不重发（确要再发加 --force）"); continue
        (JOBS / jid).mkdir(parents=True, exist_ok=True)
        if r: jsave(JOBS / jid / "job.json", job)
        with job_lock(job):
            done_ = dstate(job).get("final")   # 拿到锁再看一次：relay 的发送线程可能刚送达
            if done_ in ("delivered", "not_needed") and not force:
                print(f"{jid} 刚刚{DSTATES[done_]}，不重发（确要再发加 --force）"); continue
            fs, detail = feishu(c, job.get("notify") or c.get("notify") or "", brief_msg(job, job["status"] == "done", job.get("answer", "")), job, what="补发结果")
            new = {"ok": "delivered", "skip": "not_needed"}.get(fs, "failed")
            note = f"{now()} rmq redeliver{'（改版前的历史结果）' if legacy else ''}：" + (f"补发送达（{detail}）" if fs == "ok" else detail if fs == "skip" else f"补发没送到（{'超时' if fs == 'timeout' else '失败'}：{clip(detail, 160)}）")
            dsave(job, final=new, final_note=note, final_at=now(), next_try=None, redelivered=now())
            log(job, f"结果送达状态：{DSTATES[new]}（{note}）")
            marked = mark_fn(jid)(new, note)
        sent += fs == "ok"
        print(f"{jid} {DSTATES[new]}：{note}" + ("" if marked else "（结论没写回任务所在机器，relay 下一轮补写）"))
    for a in ids:
        if a not in seen: print(f"{a} 没有这个任务")
    if every:
        leg = legacy_undelivered()
        if leg: print(f"另有 {len(leg)} 条 11-12 改版前的历史未送达没包含在 --all 里（rmq doctor 看清单），要发请逐个写任务号")
    print(f"补发送达 {sent} 条")


def finished_age(job):
    try:
        return time.time() - time.mktime(time.strptime(job["finished"], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return 0


def note(args):
    """rmq note <id> <话>：往任务的 events.log 追加一行（relay 在本机发现交付异常时，经 ssh 写到任务所在的机器上，rmq show 看得到）"""
    j = jload(JOBS / args[0] / "job.json")
    if j and len(args) > 1: log(j, " ".join(args[1:])[:300])


def stale_warn(job, r):
    """finished 超过 120 秒仍未 delivered：往 events.log 写一条告警（本机一份，任务所在机器一份），每个任务只写一次。"""
    if finished_age(job) <= 120 or dstate(job).get("stale_warned"):
        return
    msg = f"告警：任务已结束 {int(finished_age(job))} 秒结果仍未送达（relay 在自动重试，rmq doctor 会列出）"
    log(job, msg); dsave(job, stale_warned=True)
    if r:
        subprocess.run(["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", r, "~/.local/bin/rmq", "note", job["id"], shlex.quote(msg)],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=30)


def relay(args):
    c = cfg(); r = c.get("remote")
    if not r: sys.exit("relay 要在配置里写 remote")
    JOBS.mkdir(parents=True, exist_ok=True)
    taken = set()
    threading.Thread(target=jev_tunnel, args=(r,), daemon=True).start()
    threading.Thread(target=drive_poller, args=(c, r, taken), daemon=True).start()
    warm_cleanup()
    cc_cleanup()
    threading.Thread(target=warm_keeper, args=(c,), daemon=True).start()
    while True:
        relay_handoffs(c, r, taken)
        try:
            heartbeats(c, r)
        except Exception as e:
            print(f"心跳出错：{str(e)[:100]}", flush=True)
        try:
            p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", r, "~/.local/bin/rmq", "undelivered"],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
            todo = json.loads(p.stdout or "[]")
        except Exception:
            todo = []
        for job in todo:
            (JOBS / job["id"]).mkdir(exist_ok=True)
            jsave(JOBS / job["id"] / "job.json", job)
            stale_warn(job, r)
            # 11-12：每单在自己的发送线程里送（deliver_due），送达状态如实写回 mini；没送到按 DELIVER_GAPS 自动重试
            lane_run(job["id"], deliver_due, c, job, remote_mark(r, job["id"]))
        if "--once" in args:
            for t in list(LANES.values()): t.join()
            return
        time.sleep(10)


def jev_tunnel(r):
    """mini 上的 JEV 连不上上游（typesafe 在国内不通），借本机的：本机 7677 反向映射到 mini 的 7681，断了 10 秒后重连。
    kp 在 mini 上跑，不管驾驶是 DeepSeek、Gemini 还是 Claude 直控，判屏都走这条。"""
    while True:
        subprocess.run(["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
                        "-o", "ServerAliveCountMax=3", "-R", "127.0.0.1:7681:127.0.0.1:7677", r],
                       stdin=subprocess.DEVNULL, capture_output=True)
        time.sleep(10)


def relay_handoffs(c, r, taken):
    """mini 上的任务要用户接手（kp handoff）：本机代推 decide 卡（附截图）和飞书，等用户点完把答案写回 mini。"""
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", r]
    try:
        p = subprocess.run(ssh + ["~/.local/bin/rmq", "handoffs"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        reqs = json.loads(p.stdout or "[]")
    except Exception:
        return
    for req in reqs:
        if req["id"] in taken:
            continue
        taken.add(req["id"])
        subprocess.run(ssh + ["~/.local/bin/rmq", "handoff", req["id"], "take"], stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        threading.Thread(target=relay_one, args=(c, r, req), daemon=True).start()


def relay_one(c, r, req):
    img = None
    if req.get("img"):
        kpx.HDIR.mkdir(parents=True, exist_ok=True)
        img = kpx.HDIR / f"{req['id']}{Path(req['img']).suffix or '.jpg'}"
        if subprocess.run(["scp", "-q", f"{r}:{req['img']}", str(img)], capture_output=True, timeout=60).returncode:
            img = None
    left = max(60, int(req.get("wait", 900) - (time.time() - req.get("ts", time.time()))))
    fs, detail = feishu(c, req.get("notify") or c.get("notify") or "", f"RMQ1 📱 需要你接手：{req['reason']}\n在手机上处理完，到 decide 板上点「处理好了」（{left // 60} 分钟内）。"
                                                   f"\n（任务 {req.get('job') or '直控'}）")
    if fs != "ok": print(f"接管提醒 {req['id']} 飞书{'不需要发' if fs == 'skip' else '没送到'}：{detail}", flush=True)
    try:
        ans = kpx.push(req, img, left)
    except Exception as e:
        print(f"接管卡推送失败 {req['id']}：{e}", flush=True); return
    if ans:
        subprocess.run(["ssh", "-o", "BatchMode=yes", r, "~/.local/bin/rmq", "handoff", req["id"], "answer", shlex.quote(ans)],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=60)


KPSHIM = r'''#!/usr/bin/env python3
# Gemini 在本机 Mac 驾驶时用的 kp：命令送回 mini 执行（手机桥在 mini），输出里的截图拉回本机再给路径
import os, re, shlex, subprocess, sys
r, env, local = @R@, @ENV@, @LOCAL@
cm = ["-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/rmq-cm-%r@%h-%p", "-o", "ControlPersist=600", "-o", "BatchMode=yes"]
pre = "export PATH=$HOME/.local/bin:/opt/homebrew/bin:$PATH " + " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items()) + "; kp "
p = subprocess.run(["ssh", *cm, r, pre + " ".join(shlex.quote(a) for a in sys.argv[1:])], stdin=subprocess.DEVNULL, capture_output=True, text=True)
out = p.stdout + p.stderr
for path in set(re.findall(r"/(?:tmp/kvmphone|Users/[^\s:]+/shots)/[\w.-]+\.(?:jpg|png)", out)):
    dst = os.path.join(local, "shots", os.path.basename(path))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if subprocess.run(["scp", *cm, "-q", f"{r}:{path}", dst], stdin=subprocess.DEVNULL).returncode == 0:
        out = out.replace(path, dst)
sys.stdout.write(out)
sys.exit(p.returncode)
'''


def drive_poller(c, r, taken):
    """驾驶请求单独一条线长轮询，不跟交付、接管那 10 秒一轮的主循环挤"""
    while True:
        t = time.time()
        relay_drives(c, r, taken, wait=25)
        if time.time() - t < 1:  # ssh 不通时别空转
            time.sleep(5)


def relay_drives(c, r, taken, wait=0):
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", r]
    try:
        reqs = json.loads(subprocess.run(ssh + ["~/.local/bin/rmq", "drives", "--wait", str(wait)], stdin=subprocess.DEVNULL,
                                         capture_output=True, text=True, timeout=wait + 60).stdout or "[]")
    except Exception:
        return
    for q in reqs:
        if q["adir"] in taken:
            continue
        taken.add(q["adir"])
        subprocess.run(ssh + ["touch", q["adir"] + "/drive.taken"], stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        threading.Thread(target=relay_drive, args=(c, r, q), daemon=True).start()


WARM, WLOCK = {}, threading.Lock()


def agy_cold(c, model, L, prompt, logn="driver.log"):
    # 直接调 agy 出 stream-json（11-01）：每一步带用量，被时限收走时也留得下前面的记录（桥的纯文本输出两样都没有）
    with open(L / logn, "w") as out:
        return subprocess.Popen([c.get("agy_bin", str(Path.home() / ".local/bin/agy")), "--model", model, "--output-format", "stream-json",
                                 "--print-timeout", "3600s", "--dangerously-skip-permissions", "--add-dir", str(L), "-p", prompt],
                                env={**os.environ, "NO_COLOR": "1", "PATH": f"{L / 'bin'}:{os.environ['PATH']}"}, cwd=L,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)


def agy_watch(p, L, ra, ssh, sync, logn="driver.log"):
    """每秒看一次（原来 15 秒），出了 result 事件就收尾，不等 agy 自己关（它关 language server 常超时 5 秒）；返回 result 的 status。
    ssh 为 None 表示驾驶就在本机（mini）跑：叫停看本地的 drive.kill，也不用同步。"""
    last, got = 0.0, None
    while p.poll() is None:
        time.sleep(1)
        if ssh is None and (Path(ra) / "drive.kill").exists():
            break
        try:
            with open(L / logn, "rb") as f:
                f.seek(max(0, f.seek(0, 2) - 8192)); tail = f.read().decode(errors="ignore")
            m = re.search(r'"event"\s*:\s*"result".*?"status"\s*:\s*"([A-Z_]+)"', tail)
            got = m.group(1) if m else None
        except OSError:
            pass
        if got:
            time.sleep(0.5)
            break
        if ssh is None or time.time() - last < 15:
            continue
        last = time.time()
        sync()
        k = subprocess.run(ssh + [f"test -e {shlex.quote(ra)}/drive.kill && echo kill"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        if "kill" in k.stdout:
            break
    if p.poll() is None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try: os.killpg(p.pid, sig)
            except OSError: pass  # macOS 上进程组只剩僵尸时报 EPERM，不能让它打断收尾（11-01 中继日志里出现过）
            if got: break  # 正常收尾：发了 SIGTERM 就走，不等它关
            time.sleep(3)
    return got


def warm_spawn(c, model):
    """预热一个 agy：流式输入模式先把认证、拉模型这 12 秒做完，等 stdin 来任务（{"event":"user","message":{…}}）"""
    ws = JOBS / "_warm" / (time.strftime("%m%d-%H%M%S-") + uuid.uuid4().hex[:4])
    ws.mkdir(parents=True, exist_ok=True)
    with open(ws / "driver.log", "w") as out:
        p = subprocess.Popen([c.get("agy_bin", str(Path.home() / ".local/bin/agy")), "--model", model, "--input-format", "stream-json",
                              "--output-format", "stream-json", "--dangerously-skip-permissions", "--add-dir", str(JOBS)],
                             env={**os.environ, "NO_COLOR": "1"}, cwd=ws, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT,
                             start_new_session=True, text=True)
    (ws / "pid").write_text(str(p.pid))
    return p, ws, time.time()


def warm_cleanup():
    """relay 重启时，上一个 relay 留下的预热 agy 会变孤儿（它们自成会话，不随 relay 退出）：按 pid 文件收掉"""
    for f in (JOBS / "_warm").glob("*/pid"):
        try:
            pid = int(f.read_text())
            if "agy" in subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout:
                os.killpg(pid, signal.SIGTERM)
        except (ValueError, OSError):
            pass
    import shutil; shutil.rmtree(JOBS / "_warm", ignore_errors=True)


def warm_kill(p, ws=None):
    try: os.killpg(p.pid, signal.SIGTERM)
    except OSError: pass
    if ws:
        import shutil; shutil.rmtree(ws, ignore_errors=True)


def warm_fill(c, model):
    """备一个启动好的 agy 等下一单；闲置超过 20 分钟换新的，免得登录过期"""
    with WLOCK:
        old = WARM.get(model)
        if old and old[0].poll() is None and time.time() - old[2] < 1200:
            return
        WARM[model] = warm_spawn(c, model)
        print(f"[warm] 预热 {model} → {WARM[model][1].name}（{'替换旧的' if old else '新备'}，{threading.current_thread().name}）", flush=True)
    if old:
        warm_kill(old[0], old[1])


def warm_take(model):
    with WLOCK:
        w = WARM.pop(model, None)
    print(f"[warm] 取 {model}：{w[1].name if w else '没有'}", flush=True)
    if w and w[0].poll() is None and time.time() - w[2] < 1500:
        return w[0], w[1]
    if w:
        warm_kill(w[0], w[1])
    return None


def warm_unlink(L, ws, logn="driver.log"):
    """预热进程的日志原本在 _warm 里、任务目录只放了链接：收尾时换成真文件，再删预热目录"""
    f = L / logn
    try:
        if f.is_symlink():
            t = f.read_bytes(); f.unlink(); f.write_bytes(t)
    except OSError:
        pass
    import shutil; shutil.rmtree(ws, ignore_errors=True)


def warm_keeper(c):
    # 11-18 停用：Google 以违反条款封了 Antigravity，主人定 agy 只许手动用；后台预热就是无人值守的自动调用，不许再开
    return
    while True:
        for m in list(WARM) or [c.get("warm_model", "gemini-3.8-flash-high")]:
            try:
                warm_fill(c, m)
            except Exception:
                pass
        time.sleep(60)



CC_CM = ["-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/rmq-cm-%r@%h-%p", "-o", "ControlPersist=600", "-o", "BatchMode=yes"]
CC_LOGIN_ERR = re.compile(r"Not logged in|Please run /login|Invalid API key|authentication_error|OAuth token has expired|Credit balance is too low", re.I)


def cc_sync(r, L, ra, state):
    """把 Mac 本地镜像目录里的 result.json、driver.log 同步回 mini 的任务目录（driver.log 落成 driver.remote.log，rwait 收尾时打进 mini 的 driver.log）。
    只传有变化的；先传临时名再 mv，免得 mini 读到写了一半的 result.json。返回是否全部成功。"""
    ok = True
    for f, g in (("result.json", "result.json"), ("driver.log", "driver.remote.log")):
        fp = L / f
        try:
            st = fp.stat(); sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            continue
        if state.get(f) == sig:
            continue
        tmp = f"{ra}/.{g}.part"
        try:
            if (subprocess.run(["scp", *CC_CM, "-q", str(fp), f"{r}:{tmp}"], stdin=subprocess.DEVNULL, capture_output=True, timeout=60).returncode == 0
                    and subprocess.run(["ssh", *CC_CM, r, f"mv {shlex.quote(tmp)} {shlex.quote(ra + '/' + g)}"], stdin=subprocess.DEVNULL,
                                       capture_output=True, timeout=60).returncode == 0):
                state[f] = sig
            else:
                ok = False
        except subprocess.TimeoutExpired:
            ok = False
    return ok


def cc_final_sync(r, L, ra, state):
    """收尾同步：最多 3 次、间隔 2 秒，返回是否成功。"""
    for i in range(3):
        if cc_sync(r, L, ra, state):
            return True
        if i < 2:
            time.sleep(2)
    return False


def cc_finish(r, ra, rc, decline=None):
    """告诉 mini 本机这边结束了：先写原因（如有），再写 drive.done（rwait 看它）。"""
    ssh = ["ssh", *CC_CM, r]
    if decline:
        subprocess.run(ssh + [f"cat > {shlex.quote(ra + '/drive.decline')}"], input=decline, text=True, capture_output=True, timeout=60)
    subprocess.run(ssh + [f"echo {int(rc)} > {shlex.quote(ra + '/drive.done')}"], stdin=subprocess.DEVNULL, capture_output=True, timeout=60)


def cc_cleanup():
    """relay 重启时，上一个 relay 留下的 Mac 端 claude（自成会话，不随 relay 退出）按 cc.pid 收掉，免得没人同步、没人听叫停还在操作手机。"""
    for f in JOBS.glob("*/attempt*.shim/cc.pid"):
        try:
            pid = int(f.read_text())
            if "claude" in subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout:
                os.killpg(pid, signal.SIGTERM)
        except (ValueError, OSError):
            pass
        f.unlink(missing_ok=True)


def relay_drive_cc(c, r, q):
    """cc 驾驶的 Mac 这一半（11-10）：在 Mac 上用本机登录的 Claude Code 跑 claude -p。
    文件走法同 Gemini：Mac 本地镜像目录 ~/.rmq/jobs/<job>/<attempt>/ 给 claude 读写（Write 工具只能写本机），每 15 秒同步回 mini；
    kp 是 bin/kp 垫片（PATH 最前），命令经 ssh 回 mini 执行，输出里的截图拉回本机再给路径。预算 budget.txt / 插话 steer.txt 由 mini 上的 kp 自己读，不用传。"""
    ra = q["adir"]
    L = JOBS / q["job"] / Path(ra).name
    try:
        _relay_drive_cc(c, r, q, ra, L)
    except Exception as e:  # 不能让 mini 一直等：记一笔、告诉它这轮失败
        print(f"[cc {q['job']}] 中继出错：{str(e)[:200]}", flush=True)
        try: cc_finish(r, ra, 1)
        except Exception: pass


def _relay_drive_cc(c, r, q, ra, L):
    ssh = ["ssh", *CC_CM, r]
    login = claude_login(c, fresh=True, unknown_ok=True)
    if login is None:  # 检测超时 ≠ 未登录：照常开跑，真没登录时 claude 输出会命中 CC_LOGIN_ERR，照旧退回 DeepSeek
        print(f"[cc {q['job']}] 登录态检测超时（Mac 忙），照常开跑", flush=True)
    elif not login:
        why = "本机 Mac 的 Claude 未登录，本次用 DeepSeek Flash 驾驶（在 Mac 终端运行 claude auth login 后恢复 Sonnet）"
        print(f"[cc {q['job']}] {why}", flush=True)
        cc_finish(r, ra, CC_DECLINE_RC, why); return
    # 垫片与 cc.pid 放在模型无权写的兄弟目录（不在 Edit/Write 放行范围内），否则模型可改写 kp 垫片得到任意代码执行
    S = L.parent / (L.name + ".shim")
    L.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(L / "bin", ignore_errors=True)  # 旧版本留下的可写垫片
    S.mkdir(parents=True, exist_ok=True)
    kpb = S / "kp"
    kpb.write_text(KPSHIM.replace("@R@", repr(r)).replace("@ENV@", repr(q["env"])).replace("@LOCAL@", repr(str(L))))
    kpb.chmod(0o755)
    for f in ("result.json", "driver.log"):
        (L / f).unlink(missing_ok=True)
    (S / "cc.pid").unlink(missing_ok=True)
    kph = kp_help_text(c, False)
    a = str(L)
    prompt = (render_prompt("cc") + f"\n当前任务目录：{a}（result.json 写在这里；它在本机，会自动同步，不用管）。\n"
              "kp 命令照常直接写 `kp …`（已指向会在连着手机的那台机器上执行的版本，截图路径是本机路径，可直接 Read）。\n\n"
              + (f"kp 用法（即 kp -h 的输出）：\n{kph}\n\n" if kph else "") + f"任务：{q['task']}\n")
    if q.get("hint"):
        prompt += f"\n上一轮复核没通过，这次重点改：{q['hint']}\n"
    # 与原 mini 版同一套权限：只放行 kp 与少数只读命令，任务目录读写；不放 Bash(timeout:*)，也不放任何能跑任意命令的通配。
    allowed = ["Bash(kp:*)", "Bash(sleep:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(grep:*)",
               f"Read(/{a}/**)", f"Edit(/{a}/result.json)", f"Write(/{a}/result.json)"]
    cmd = [claude_bin(c), "-p", prompt, "--model", q["model"], "--safe-mode", "--no-session-persistence",
           "--permission-mode", "dontAsk", "--tools", "Bash,Read,Write", "--allowedTools", *allowed,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
           "--max-turns", str(q.get("max_turns") or c["max_turns"]), "--output-format", "stream-json", "--verbose"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_") and k != "CLAUDE_CONFIG_DIR"}
    env.update(q["env"])
    env["PATH"] = f"{S}:{env.get('PATH', '/usr/bin:/bin')}"
    env["NO_COLOR"] = "1"
    # Bash 工具的 shell 快照在 Mac 过载时会退回登录 shell、把 ~/.local/bin 排到垫片前面（11-10 实测绕过垫片，mini 上没有 kp.jsonl）；
    # CLAUDE_ENV_FILE 在每条命令前执行，再把垫片顶回 PATH 最前。文件在模型无权写的 .shim 目录。
    (S / "env.sh").write_text(f"export PATH={shlex.quote(str(S))}:\"$PATH\"\n")
    env["CLAUDE_ENV_FILE"] = str(S / "env.sh")
    t0 = time.time()

    def launch(mode):
        with open(L / "driver.log", mode) as out:
            pp = subprocess.Popen(cmd, env=env, cwd=L, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        (S / "cc.pid").write_text(str(pp.pid))
        return pp

    def killpg(pp):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try: os.killpg(pp.pid, sig)
            except OSError: pass
            if sig == signal.SIGTERM:
                for _ in range(6):
                    if pp.poll() is not None: break
                    time.sleep(0.5)
                if pp.poll() is not None: break
        try: pp.wait(timeout=5)
        except subprocess.TimeoutExpired: pass

    p = launch("w")
    print(f"[cc {q['job']}] 本机 claude 开跑 pid={p.pid} model={q['model']}", flush=True)
    # 首轮看门狗（11-10 实测一单首轮请求挂了约 9 分钟、流里没有任何重试事件；单独复现 4.7 秒就回）：
    # 首轮 cc_first_reply_secs（默认 150）秒还没出 assistant 事件就杀掉重开一次；第二次还不回就退回 DeepSeek。没出过 assistant 就没调过工具，重开不会碰手机。
    first_lim = float(c.get("cc_first_reply_secs", 150))
    t_launch, first_at, restarts, stall, last_first = t0, None, 0, "", 0.0
    state, last_sync, last_kill, fails, killed = {}, 0.0, 0.0, 0, ""
    while p.poll() is None:
        time.sleep(1)
        t = time.time()
        if first_at is None and t - last_first >= 3:
            last_first = t
            try: seen = b'"type":"assistant"' in (L / "driver.log").read_bytes()
            except OSError: seen = False
            if seen:
                first_at = t
                print(f"[cc {q['job']}] 首轮回应 {int(t - t_launch)}s" + (f"（重开 {restarts} 次后）" if restarts else ""), flush=True)
            elif t - t_launch >= first_lim:
                killpg(p)
                if restarts >= 1:
                    stall = f"本机 Claude 连续两次 {int(first_lim)} 秒没给出第一步，本次用 DeepSeek Flash 驾驶"
                    print(f"[cc {q['job']}] {stall}", flush=True); break
                restarts += 1
                print(f"[cc {q['job']}] 首轮 {int(first_lim)}s 没回应，杀掉重开（第 {restarts} 次）", flush=True)
                with open(L / "driver.log", "a") as f:
                    f.write(f"[rmq] 首轮 {int(first_lim)}s 没回应，重开 claude\n")
                p = launch("a"); t_launch = time.time()
                continue
        if t - last_kill >= 3:   # 3 秒看一次 mini 的叫停（共用 ssh 长连接，很轻）；连不上 mini 连续 10 次（约 1 分钟以上）就自己收工，免得没人管还在动手机
            last_kill = t
            try:
                k = subprocess.run(ssh + [f"test -e {shlex.quote(ra)}/drive.kill && echo kill"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
                fails = fails + 1 if k.returncode == 255 else 0
                if "kill" in k.stdout: killed = "mini 叫停"; break
            except subprocess.TimeoutExpired:
                fails += 1
            if fails >= 10: killed = "连不上 mini"; break
        if t - last_sync >= 15:
            last_sync = t; cc_sync(r, L, ra, state)
    decline = None
    if killed:
        killpg(p)
        rc = 143
        print(f"[cc {q['job']}] 已杀掉本机 claude（{killed}），用时 {int(time.time() - t0)}s", flush=True)
    elif stall:
        rc, decline = CC_DECLINE_RC, stall
        (L / "result.json").unlink(missing_ok=True)
    else:
        rc = p.returncode
    (S / "cc.pid").unlink(missing_ok=True)
    if not killed and rc != 0 and time.time() - t0 < 90 and not (L / "result.json").exists():
        try: tail = (L / "driver.log").read_bytes()[-4000:].decode("utf-8", "ignore")
        except OSError: tail = ""
        if CC_LOGIN_ERR.search(tail):
            decline, rc = "本机 Mac 的 Claude 登录失效或不可用，本次用 DeepSeek Flash 驾驶（在 Mac 终端运行 claude auth login）", CC_DECLINE_RC
    if not cc_final_sync(r, L, ra, state):
        print(f"[cc {q['job']}] 收尾同步回 mini 失败（3 次）", flush=True)
        decline = decline or "收尾时 result.json / driver.log 没能同步回 mini"
        rc = rc or 1
    cc_finish(r, ra, rc, decline)
    print(f"[cc {q['job']}] 结束 rc={rc} 用时 {int(time.time() - t0)}s", flush=True)


def relay_drive(c, r, q):
    """drive_remote 的另一半：跑 agy（Gemini）。r 是 mini 的 ssh 名时在本机 Mac 跑，每 15 秒把 result.json 同步回 mini、mini 收走了就停；
    r 为 None 时就在 mini 本机跑（11-03 起 gemini_local）：直接写任务目录，日志就是 driver.remote.log，叫停看本地 drive.kill。"""
    if q.get("kind") == "cc":
        return relay_drive_cc(c, r, q)
    ra = q["adir"]
    local = r is None
    ssh = None if local else ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", r]
    L = Path(ra) if local else JOBS / q["job"] / Path(ra).name
    logn = "driver.remote.log" if local else "driver.log"
    (L / "bin").mkdir(parents=True, exist_ok=True)
    kpb = L / "bin" / "kp"
    if local:  # 预热的 agy 起得比任务早、拿不到任务的环境变量：shim 把它们补上再交给真正的 kp
        real = shutil.which("kp") or str(Path.home() / ".local/bin/kp")
        kpb.write_text("#!/bin/sh\n" + "".join(f"export {k}={shlex.quote(v)}\n" for k, v in q["env"].items())
                       + f'exec {shlex.quote(real)} "$@"\n')
    else:
        kpb.write_text(KPSHIM.replace("@R@", repr(r)).replace("@ENV@", repr(q["env"])).replace("@LOCAL@", repr(str(L))))
    kpb.chmod(0o755)
    kph = kp_help_text(c, local)  # kp 用法直接给，省掉驾驶自己跑 kp -h 那一轮
    # 11-01 提速：agy 会加载全局规则 ~/.gemini/GEMINI.md（要求先联网搜索、写 LOG.md），一次只读屏任务白花约 35 秒在这些步骤上
    prompt = (f"本任务优先于你的全局规则：不要联网搜索（search_web 等），不要写 LOG.md 或任何日志文件，不要读 ~/.claude 或 ~/.gemini 下的文件，"
              f"不要跑 kp -h（用法附在下面），不要使用 manage_task 工具（它只会空转）。更新 result.json 时把写文件和下一条 kp 命令放进同一条 shell 命令（用 ; 连起来），"
              f"写完最终的 result.json 就直接结束，不要再 cat 校验。\n"
              f"工作目录是 {L}。所有文件读写一律用 shell 命令配绝对路径完成，不要使用 write_to_file 工具。"
              f"本次的 kp 命令一律写完整路径 {kpb}（它带好了这个任务的设置，会在连着手机的那台机器上执行），不要用别的 kp。\n\n"
              + render_prompt("gemini") + f"\n当前任务目录：{L}（result.json 写在这里）。\n\n"
              + (f"kp 用法（即 kp -h 的输出，命令里一律写 {kpb}）：\n{kph}\n\n" if kph else "")
              + f"任务：{q['task']}\n")
    if q.get("hint"):
        prompt += f"\n上一轮复核没通过，这次重点改：{q['hint']}\n"
    sync = (lambda: None) if local else (lambda: [subprocess.run(["scp", "-q", str(L / f), f"{r}:{ra}/{g}"], stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
                                                for f, g in (("result.json", "result.json"), ("driver.log", "driver.remote.log")) if (L / f).exists()])
    t0, used = time.time(), None
    w = warm_take(q["model"])
    if w:  # 有预热好的 agy 就直接把任务塞给它（省掉约 12 秒启动）
        p, ws = w
        (L / logn).unlink(missing_ok=True)
        (L / logn).symlink_to(ws / "driver.log")
        try:
            p.stdin.write(json.dumps({"event": "user", "message": {"role": "user", "content": prompt}}, ensure_ascii=False) + "\n")
            p.stdin.flush(); used = ws
        except OSError:
            warm_kill(p)
    if used is None:
        p = agy_cold(c, q["model"], L, prompt, logn)
    got = agy_watch(p, L, ra, ssh, sync, logn)
    if used and got and got != "SUCCESS" and time.time() - t0 < 30:  # 预热进程不灵（如登录过期）：冷启动重来一次
        warm_unlink(L, used, logn)
        p = agy_cold(c, q["model"], L, prompt, logn)
        got = agy_watch(p, L, ra, ssh, sync, logn); used = None
    rc = (0 if got == "SUCCESS" else 1) if got else (p.poll() if p.poll() is not None else 1)
    if used:
        warm_unlink(L, used, logn)
    threading.Thread(target=warm_fill, args=(c, q["model"]), daemon=True).start()
    sync()
    if local:
        (L / "drive.done").write_text(str(rc))
    else:
        subprocess.run(ssh + [f"echo {rc} > {shlex.quote(ra)}/drive.done"], stdin=subprocess.DEVNULL, capture_output=True, timeout=60)


# 本机 ↔ mini 源码一致性：doctor 只读对比，sync 推送
SYNC_ROOT = HERE.parent
SYNC_DIRS = ("rmq", "kvmphone")
SYNC_EXT = (".py", ".mjs", ".md", ".json", ".swift")
SYNC_SKIP_PARTS = {"__pycache__", ".space", "vendor", "openclaw-skill", "bin"}
SYNC_SKIP_FILES = {".glkvm_token", "rmq-bot.py"}   # rmq-bot.py 只在本机跑


def sync_wanted(rel):
    parts = rel.split("/")
    if any(x in SYNC_SKIP_PARTS for x in parts[:-1]) or parts[-1] in SYNC_SKIP_FILES:
        return False
    if ".bak" in parts[-1] or not parts[-1].endswith(SYNC_EXT):
        return False
    if parts[:2] == ["kvmphone", "knowledge"] and rel != "kvmphone/knowledge/README.md":
        return False   # knowledge 下是 App 知识自学的运行时文件
    return True


def sync_diff(c):
    """返回 (不一致列表[(相对路径, 状态)], 错误)；状态：内容不同 / mini 缺。只读。"""
    r = c.get("remote") or "macmini"
    loc = {}
    for d in SYNC_DIRS:
        for f in (SYNC_ROOT / d).rglob("*"):
            rel = f.relative_to(SYNC_ROOT).as_posix()
            if f.is_file() and sync_wanted(rel):
                loc[rel] = hashlib.md5(f.read_bytes()).hexdigest()
    cmd = ("cd ~/rmq1 && find " + " ".join(SYNC_DIRS) + " -type f 2>/dev/null | grep -v -e __pycache__ -e '\\.bak' | "
           "while read f; do md5 -r \"$f\"; done")
    p = subprocess.run(["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", r, cmd],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        return None, (p.stderr.strip() or f"ssh 退出码 {p.returncode}")
    rem = {}
    for ln in p.stdout.splitlines():
        h, _, rel = ln.partition(" ")
        rem[rel.strip()] = h
    out = []
    for rel in sorted(loc):
        if rel not in rem: out.append((rel, "mini 缺"))
        elif rem[rel] != loc[rel]: out.append((rel, "内容不同"))
    return out, None


def sync(args):
    c = cfg()
    r = c.get("remote")
    if not r:
        sys.exit("rmq sync 只在本机跑（配置里要有 remote）")
    dry = "--dry-run" in args
    only = [a for a in args if not a.startswith("-")]
    diff, err = sync_diff(c)
    if diff is None:
        sys.exit(f"取不到 {r} 的文件清单：{err}")
    if only:   # 11-12：本机还有一批没同步的 JEV 评测文件，只推这次改的
        diff = [(rel, st) for rel, st in diff if rel in only]
    if not diff:
        print(f"✓ 本机与 {r} 的 ~/rmq1 源码一致，无需同步"); return
    print(f"{'（dry-run）会推送' if dry else '将推送'} {len(diff)} 个文件到 {r}:~/rmq1/：")
    for rel, st in diff: print(f"  {rel}  [{st}]")
    p = subprocess.run(["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", r,
                        "~/.local/bin/rmq ls -n 50"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        sys.exit(f"取不到 {r} 的任务状态，不敢推：{p.stderr.strip()}")
    busy = [ln for ln in p.stdout.splitlines() if len(ln.split()) > 2 and ln.split()[2] in ("running", "reviewing")]
    if busy:
        print(f"{r} 上有任务在跑，{'真同步会被拒绝' if dry else '拒绝同步'}：")
        for ln in busy: print("  " + ln)
        sys.exit(0 if dry else 1)
    if dry:
        print("（dry-run，未改动 mini）"); return
    stamp = time.strftime("%Y%m%d-%H%M")
    for rel, st in diff:
        rp = "~/rmq1/" + shlex.quote(rel)
        mode = oct((SYNC_ROOT / rel).stat().st_mode & 0o777)[2:]
        pre = f"mkdir -p $(dirname {rp}); [ -f {rp} ] && cp -p {rp} {rp}.bak-{stamp}; true"
        subprocess.run(["ssh", "-n", r, pre], stdin=subprocess.DEVNULL, check=True, timeout=60)
        subprocess.run(["scp", "-p", "-q", str(SYNC_ROOT / rel), f"{r}:~/rmq1/{rel}"], stdin=subprocess.DEVNULL, check=True, timeout=120)
        subprocess.run(["ssh", "-n", r, f"chmod {mode} {rp}"], stdin=subprocess.DEVNULL, check=True, timeout=60)
        print(f"  已推 {rel}（旧版备份 .bak-{stamp}）")
    if any(rel == "rmq/rmq.py" for rel, _ in diff):
        print("rmq.py 变了，worker 要在空窗重启才生效")


def doctor(_):
    c = cfg()
    def chk(name, ok, more=""): print(f"{'✓' if ok else '✗'} {name} {more}")
    chk("配置", True, str(HOME / "config.json"))
    chk("DeepSeek key 文件", Path(c["ds_key_file"]).expanduser().is_file(), c["ds_key_file"])
    if c.get("review_api"):
        chk("复核接口文件", Path(c["review_api"]).expanduser().is_file(), f'{c["review_api"]}（模型 {c["review_model"]}）')
    else:
        print(f"· 复核用 Claude 账号登录态（模型 {c['review_model']}）")
    relay_mode = c.get("deliver") == "relay"
    for k in ("kp", "claude") + (() if relay_mode else ("decide",)):
        p = subprocess.run(["/bin/sh", "-c", f"command -v {c[k]}"], capture_output=True, text=True)
        chk(k, p.returncode == 0, p.stdout.strip())
    if relay_mode:
        print("· 交付由另一台机器的 rmq relay 负责（deliver=relay）")
    else:
        chk("openclaw", Path(c["openclaw"]).exists(), c["openclaw"])
        chk("通知目标", bool(c["notify"]), c["notify"] or "未设，任务完成只推 decide")
    if c.get("remote"):
        print(f"· ask / ls / show / cancel 转发到 {c['remote']}")
    if c.get("remote"):   # 本机才有对照对象；mini 自己跑 doctor 时跳过
        try:
            diff, err = sync_diff(c)
            if diff is None: print(f"✗ 源码一致性 取不到 mini 文件清单：{err}")
            elif not diff: print("✓ 源码一致性 本机与 mini ~/rmq1 一致")
            else:
                print(f"✗ 源码一致性 {len(diff)} 个文件和 mini 不同（rmq sync --dry-run 看清单）：")
                for rel, st in diff: print(f"    {rel} [{st}]")
        except Exception as e:
            print(f"✗ 源码一致性 检查失败：{e}")
    # cc 驾驶（11-10 起）在本机 Mac 上跑：看 Mac 的 Claude 登录态与 relay 是否在线。mini 自己跑 doctor 时只说明分工
    if c.get("remote"):
        try:
            ok = claude_login(c, fresh=True)
            chk(f"本机 Claude 登录态（cc 驾驶用 {c['cc_model']}）", ok, "" if ok else "未登录：cc 驾驶会自动改用 DeepSeek；在本机终端运行 claude auth login")
        except Exception as e:
            print(f"? 本机 Claude 登录态 取不到：{str(e)[:80]}")
        pr = subprocess.run(["pgrep", "-f", "rmq.py relay"], capture_output=True, text=True)
        chk("本机 relay 在线（cc 驾驶接单）", pr.returncode == 0, "" if pr.returncode == 0 else "没在跑：cc 任务 90 秒没人接单就改用 DeepSeek；launchctl kickstart -k gui/$(id -u)/local.rmq1.rmq")
    else:
        print("· cc 驾驶由本机 Mac 的 relay 接手（Claude 登录态与 relay 在线情况在本机 rmq doctor 里看）")
    # 结果送达（11-12）：名单以任务所在机器的 job.json 为准（本机有 remote 时问 mini）。待送达 = 正在自动重试；
    # 没送到 = 自动重试用完、等人补发；历史未送达 = 改版前没送到但当时标成了已送达，只列出来，不自动补发
    try:
        und = remote_json(c["remote"], "undelivered", "--all") if c.get("remote") else \
            [j for j in jobs() if pending_delivery(j) or (j["status"] in FINISHED and j.get("delivery") == "failed")]
        pend = [j for j in und if pending_delivery(j)]
        bad = [j for j in und if not pending_delivery(j)]
        stuck = [j for j in pend if finished_age(j) > 1200]   # 4 次重试约 8 分钟就该有结论，过了 20 分钟还在待送达说明 relay 没在送
        chk("结果送达", not bad and not stuck, "做完的结果都已送达或不需要发" if not und else
            f"没送到 {len(bad)} 条、待送达 {len(pend)} 条" + (f"（其中 {len(stuck)} 条做完超过 20 分钟还没送出，看本机 relay 是否在跑）" if stuck else ""))
        for j in pend:
            print(f"    待送达 {j['id']} {j['title'][:24]} 已结束 {int(finished_age(j)) // 60} 分钟：{clip(j.get('delivery_note') or '还没发', 140)}")
        for j in bad:
            print(f"    没送到 {j['id']} {j['title'][:24]}：{clip(j.get('delivery_note') or '', 140)}；补发 rmq redeliver {j['id']}")
    except Exception as e:
        print(f"? 结果送达 取不到：{str(e)[:120]}")
    if c.get("remote") or c.get("deliver") != "relay":   # 历史记录在发飞书的那台机器上
        leg = legacy_undelivered()
        if leg:
            print(f"· 历史未送达 {len(leg)} 条（11-12 改版前飞书没送到、当时标成了已送达；不自动补发，要发：rmq redeliver <任务号>）：")
            for jid, title, fin in leg: print(f"    {jid} {title[:24]}（{fin[:16]} 结束）")
    else:
        print("· 历史未送达在发飞书的那台机器（本机 Mac）上用 rmq doctor 看")
    q = [j for j in jobs() if j["status"] in ("queued", "running", "reviewing")]
    print(f"队列：{len(q)} 个未完成")


if __name__ == "__main__":
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("-h", [])
    r = cfg().get("remote")
    if r and cmd in ("ask", "ls", "show", "cancel", "say", "stop", "progress") and not os.environ.get("RMQ_LOCAL"):
        if cmd == "ask" and SELFTEST_FLAG in rest:
            selftest_caller_check()   # 外部调用的环境标记到不了 mini，转发前在本机先查
        # 11-03：ssh 的输出直接接到 OpenClaw 网关的 exec 输出流上会被 SIGTERM（经管道 / 文件就正常），
        # 所以不再 exec ssh，而是收齐输出再自己打印；-n 和 ConnectTimeout 防 stdin 不关、连不上时一直挂
        p = subprocess.run(["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", r,
                            " ".join(["~/.local/bin/rmq", cmd, *map(shlex.quote, rest)])],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
        sys.stdout.buffer.write(p.stdout); sys.stdout.flush()
        sys.stderr.buffer.write(p.stderr); sys.stderr.flush()
        sys.exit(p.returncode)
    fn = {"ask": ask, "ls": ls, "show": show, "cancel": cancel, "say": say_, "stop": stop, "worker": worker, "doctor": doctor,
          "undelivered": undelivered, "mark": mark, "note": note, "relay": relay, "handoffs": handoffs, "handoff": handoff_mark,
          "progress": progress, "running": running_json, "rwait": rwait, "drives": drives, "sync": sync,
          "redeliver": redeliver, "jobjson": jobjson, "feishu-selftest": feishu_selftest}.get(cmd)
    if not fn:
        print(__doc__); sys.exit(0 if cmd in ("-h", "--help") else 64)
    fn(rest)
