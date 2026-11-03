#!/usr/bin/env python3
"""rmq · 手机任务队列（每台手机一条队列，不同手机并行）

  rmq ask "任务" [--dev rmq1|esp32] [-t 标题] [-p decide项目] [--notify feishu:ou_xxx|none]   排队，打印任务 id
  rmq ls [-n 20]          最近的任务
  rmq show <id>           状态与结论
  rmq cancel <id>         取消排队中的任务
  rmq say <id> "话"       给跑着的任务插一句（驾驶模型下一步看到「[用户插话]」）
  rmq stop <id> [原因]    叫停跑着的任务：手机不再动，已拿到的照常交付（排队中的等于 cancel）
  rmq handoffs            （mini）列出等本机 relay 代推的接管请求；relay 用 rmq handoff <id> take|answer 回写
                          驾驶中 kp handoff 交给用户的环节，等用户的时间不算进 max_runtime；做完的任务带小结（用时 / 步数 / token）
  rmq worker [--once]     常驻执行（launchd 拉起）；--once 只跑一条就退
                          设备见 kvmphone/devices.json（kp devs）；不写 --dev 用配置 default_dev（默认 rmq1）。
                          开跑前先拿那台手机的 kp 租约，Claude 直控 / OpenClaw 正占着就排队等，不抢。
  rmq doctor              自检：kp、DeepSeek key、claude、decide、openclaw
  rmq relay               （本机）定时从远端 worker 取做完的任务，推 decide 与飞书

两台机分工：配置里写 "remote": "macmini" 的机器，ask / ls / show / cancel 都经 ssh 转到远端；
远端配置 "deliver": "relay"，做完只标记，由本机 relay 负责交付（远端不需要 decide 和 openclaw）。

驾驶：DeepSeek V4.1 Flash（claude -p 指向官方 Anthropic 兼容端点，只放行 kp 命令）。
复核：Opus 5.5 只读 result.json / kp.jsonl / 日志尾，不合格带意见重跑一次。
交付：decide 卡 + 飞书消息。配置 ~/.rmq/config.json，密钥只从文件读，不进脚本。
"""
import fcntl, json, os, re, shlex, shutil, signal, subprocess, sys, threading, time, uuid
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
    "review_model": "claude-opus-5-5",
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
def ask(args):
    c = cfg()
    title, project, notify, task, dev, driver = None, c["decide_project"], c["notify"], [], c["default_dev"], ""
    it = iter(args)
    for a in it:
        if a in ("-t", "--title"): title = next(it)
        elif a == "--dev": dev = next(it)
        elif a in ("-p", "--project"): project = next(it)
        elif a == "--notify": notify = next(it)
        elif a == "--driver": driver = next(it)  # 换驾驶模型：gemini-3.8-flash-low（默认）/ -high / gemini-4-argon-medium 走 agy；cc 等非 gemini 名走原生 Claude Code 驾驶（11-03 用户要求保留）
        elif a in ("-h", "--help") or (a.startswith("-") and not task):  # 11-01 `rmq ask --help` 被当成任务排上了手机
            sys.exit("用法：rmq ask [-t 标题] [--dev 设备] [-p 项目] [--notify 收件人|none] [--driver 模型] 任务内容")
        else: task.append(a)
    task = " ".join(task).strip()
    if not task:
        sys.exit("rmq ask：缺任务内容")
    if DEVF.exists():
        d = json.load(open(DEVF))["devices"].get(dev)
        if not d or d.get("enabled") is False:
            sys.exit(f"rmq ask：设备 {dev} {'不在设备表里' if not d else '还没接好'}")
    jid = time.strftime("%y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    d = JOBS / jid
    d.mkdir(parents=True)
    job = {"id": jid, "task": task, "title": title or task[:30], "project": project, "dev": dev,
           "notify": "" if notify == "none" else notify, **({"quiet": True} if notify == "none" else {}), "status": "queued", "created": now(), **({"driver": driver} if driver else {})}
    jsave(d / "job.json", job)
    ahead = sum(1 for j in jobs() if j["status"] in ("queued", "running", "reviewing") and j["id"] != jid
                and j.get("dev", "rmq1") == dev)
    print(jid)
    print(f"已排进 {dev} 的队列，前面还有 {ahead} 个任务；单个任务通常 10–25 分钟。", file=sys.stderr)


def jobs():
    out = []
    if JOBS.exists():
        for d in sorted(JOBS.iterdir()):
            j = jload(d / "job.json")
            if j: out.append(j)
    return out


def ls(args):
    n = int(args[1]) if len(args) > 1 and args[0] == "-n" else 20
    for j in jobs()[-n:]:
        ans = (j.get("answer") or "")[:60]
        print(f"{j['id']}  {j.get('dev', 'rmq1'):<5} {j['status']:<9} {j['title'][:24]:<24} {ans}")


def show(args):
    j = jload(JOBS / args[0] / "job.json")
    if not j: sys.exit("没有这个任务")
    print(json.dumps(j, ensure_ascii=False, indent=1))
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


# ───────────── 执行 ─────────────
def run_claude(cmd, env, cwd, out_path, timeout, jdir=None):
    """跑一个 claude -p，超时就杀。给了任务目录 jdir 时：kp handoff 等用户的时间不算进超时；
    用户 rmq stop 后 90 秒还没自己结束就杀（退出码 130）。"""
    with open(out_path, "w") as out:
        p = subprocess.Popen(cmd, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out,
                             stderr=subprocess.STDOUT, start_new_session=True)
        deadline, prev, rc = time.time() + timeout, time.time(), 124
        while True:
            try:
                return p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            t = time.time()
            if jdir and (jdir / "handoff.wait").exists():
                deadline += t - prev
            prev = t
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


def drive(job, c, adir, hint):
    drv = job.get("driver") or c.get("driver") or ""
    if drv.startswith("gemini"):
        return drive_remote(job, c, adir, hint, drv)
    key = Path(c["ds_key_file"]).expanduser().read_text().strip()
    env = {k: v for k, v in kenv(job).items() if not k.startswith("ANTHROPIC_")}
    base = nothink_base(c, job) if c.get("ds_nothink") else c["ds_base"]
    env.update(ANTHROPIC_BASE_URL=base, ANTHROPIC_API_KEY=key,
               CLAUDE_CONFIG_DIR=str(HOME / "claude-ds"), CLAUDE_CODE_MAX_CONTEXT_TOKENS="1000000",
               KP_LOG=str(adir / "kp.jsonl"))
    (HOME / "claude-ds").mkdir(exist_ok=True)
    prompt = (HERE / "prompt.md").read_text() + f"\n当前任务目录：{adir}（result.json 写在这里）。\n\n任务：{job['task']}\n"
    if hint:
        prompt += f"\n上一轮复核没通过，这次重点改：{hint}\n"
    a = str(adir)
    allowed = ["Bash(kp:*)", "Bash(sleep:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(grep:*)",
               "Read(//tmp/kvmphone/**)", f"Read(/{a}/**)", f"Edit(/{a}/**)"]
    cmd = [c["claude"], "-p", prompt, "--model", c["ds_model"], "--safe-mode", "--no-session-persistence",
           "--permission-mode", "dontAsk", "--tools", "Bash,Read,Write", "--allowedTools", *allowed,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
           "--max-turns", str(c["max_turns"]), "--output-format", "stream-json", "--verbose"]
    return run_claude(cmd, env, adir, adir / "driver.log", c["max_runtime"], JOBS / job["id"])


def drive_remote(job, c, adir, hint, model):
    """Gemini 驾驶（11-01 用户要试）：agy 只装在本机 Mac，mini 又连不回 Mac。这里留一张驾驶请求，本机 relay 取走后在 Mac 上跑 agy，
    agy 里的 kp 经 ssh 回 mini 执行；mini 这边起个等待进程交给 run_claude，超时、叫停、等用户接手的计时都照旧。"""
    for f in ("drive.done", "drive.kill", "drive.taken", "driver.remote.log"):
        (adir / f).unlink(missing_ok=True)
    env = {**kenv(job), "KP_LOG": str(adir / "kp.jsonl")}
    q = {"job": job["id"], "adir": str(adir), "model": model, "task": job["task"], "hint": hint or "",
         "env": {k: env[k] for k in ("KP_DEV", "KP_OWNER", "KP_JOB_DIR", "KP_JOB_ID", "KP_NOTIFY", "KP_DEVNAME", "KP_LOG")}}
    if c.get("gemini_local"):  # 11-03 起 mini 自己登录了谷歌、走东京节点：agy 就在 mini 上跑，不再绕 Mac
        (adir / "drive.taken").touch()  # 先占住，免得 Mac 的 relay 还来取
        jsave(adir / "drive.req", q)
        threading.Thread(target=relay_drive, args=(c, None, q), daemon=True).start()
        log(job, f"驾驶换成 {model}：在 mini 本机跑 agy")
    else:
        jsave(adir / "drive.req", q)
        log(job, f"驾驶换成 {model}：等本机 Mac 的 relay 接手")
    rc = run_claude([sys.executable, str(HERE / "rmq.py"), "rwait", str(adir)], env, adir, adir / "driver.log",
                    c["max_runtime"], JOBS / job["id"])
    if rc == 2 and not (adir / "drive.taken").exists():  # Mac 没开或 relay 没跑：这一轮退回 DeepSeek，任务照跑
        log(job, "本机两分钟没接手，这一轮改用 DeepSeek 驾驶")
        (adir / "drive.req").unlink(missing_ok=True)
        return drive({**job, "driver": "deepseek"}, c, adir, hint)
    return rc


def rwait(args):
    """mini 上的驾驶占位：等本机跑完（drive.done 里是退出码）。被 run_claude 收走时留 drive.kill，本机那边也停。"""
    adir = Path(args[0])
    def bye(*_):
        (adir / "drive.kill").touch(); sys.exit(124)
    signal.signal(signal.SIGTERM, bye)
    t0 = time.time()
    while not (adir / "drive.done").exists():
        if time.time() - t0 > 120 and not (adir / "drive.taken").exists():
            print("本机 relay 两分钟没来取驾驶请求（Mac 没开或 relay 没跑）", flush=True); sys.exit(2)
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
    s = {"actions": 0, "turns": 0, "in_tok": 0, "out_tok": 0, "cost": 0.0, "waited": 0}
    for a in adirs:
        for line in (a / "kp.jsonl").read_text(errors="ignore").splitlines() if (a / "kp.jsonl").exists() else []:
            try:
                s["actions"] += json.loads(line).get("cmd") in kpx_acts
            except Exception:
                pass
        if (a / "driver.remote.log").exists():  # Gemini（agy stream-json）：每个 agent_response 步算一轮，用量逐步累加
            for line in (a / "driver.remote.log").read_text(errors="ignore").splitlines():
                try:
                    su = json.loads(line).get("step_update") or {}
                except Exception:
                    continue
                u = su.get("usage")
                if su.get("state") == "DONE" and su.get("step_type") == "agent_response" and u:
                    s["turns"] += 1
                    s["in_tok"] += (u.get("input_tokens") or 0) + (u.get("cache_read_tokens") or 0)
                    s["out_tok"] += (u.get("output_tokens") or 0) + (u.get("thinking_tokens") or 0)
            continue
        for line in reversed((a / "driver.log").read_text(errors="ignore").splitlines() if (a / "driver.log").exists() else []):
            if '"type":"result"' in line.replace(" ", ""):
                try:
                    o = json.loads(line)
                except Exception:
                    break
                u = o.get("usage") or {}
                s["turns"] += o.get("num_turns") or 0
                s["in_tok"] += sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                s["out_tok"] += u.get("output_tokens") or 0
                s["cost"] += o.get("total_cost_usd") or 0
                break
    hf = JOBS / job["id"] / "handoff.jsonl"
    for line in hf.read_text().splitlines() if hf.exists() else []:
        s["waited"] += json.loads(line).get("waited", 0)
    try:
        s["secs"] = int(time.time() - time.mktime(time.strptime(job["started"], "%Y-%m-%d %H:%M:%S")))
    except Exception:
        s["secs"] = 0
    k = lambda n: f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"
    s["text"] = (f"用时 {s['secs'] // 60} 分 {s['secs'] % 60} 秒" + (f"（其中等你 {s['waited'] // 60} 分）" if s["waited"] else "")
                 + f" · 手机 {s['actions']} 步 · 模型 {s['turns']} 轮 · token 入 {k(s['in_tok'])} 出 {k(s['out_tok'])}")
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


UNDONE = re.compile(r"正在(前往|比|查|搜|看|进|去|打开|处理|继续)|尚未|还没有?(比|查|搜|看|做|去|进)|待继续|未完成|没做完|准备前往|继续前往|接下来(去|再|要)")


def selfcheck(job, adir, res, rc=0):
    """不复核时（配置 review: false，11-01 用户要「直接一遍过」）：只信驾驶自己写的 result.json，外加付款痕迹、
    超时和「结论还在进行时」三道机械检查（11-01 USB 那单 rc=124、答案写着「正在前往京东秒送比价」却判成办好了）"""
    hit = ""
    for f in ("kp.jsonl", "driver.log", "driver.remote.log"):
        p = adir / f
        if p.exists() and (m := PAID.search(p.read_text(errors="ignore"))):
            hit = m.group(0); break
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
            "items": res.get("候选") if isinstance(res.get("候选"), list) else [],
            "todo": todo, "why": "；".join(issues),
            "leftover": "；".join(side) if isinstance(side, list) else (side or "")}


def feishu(c, target, msg, job=None):
    if not target.startswith("feishu:") or not Path(c["openclaw"]).exists():
        if job: log(job, f"飞书没发：收件人「{target}」不是 feishu:… 或本机没有 openclaw")
        return
    try:
        acct = ["--account", c["feishu_account"]] if c.get("feishu_account") else []
        r = subprocess.run([c["openclaw"], "message", "send", "--channel", "feishu", *acct, "-t", target.split(":", 1)[1], "-m", msg],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)
        if job: log(job, f"飞书推送 rc={r.returncode} {r.stderr.strip()[-200:]}")
    except Exception as e:
        if job: log(job, f"飞书推送失败：{e}")


def brief_parts(job, ok, answer):
    """给人看的汇报：结论在前、候选逐行，复核的内部细节不往外发（11-01 用户嫌飞书那段又长又乱）"""
    b = job.get("brief") or {}
    head = "⏹ 已叫停" if job.get("status") == "stopped" else ("✅ 办好了" if ok else "⚠️ 没做完")
    items = [s.strip() for s in b.get("items") or [] if isinstance(s, str) and s.strip()][:6]
    secs, waited = (job.get("stats") or {}).get("secs", 0), (job.get("stats") or {}).get("waited", 0)
    foot = f"任务 {job['id']}" + (f" · 用时 {secs // 60} 分" if secs else "") + (f"（等你 {waited // 60} 分）" if waited >= 60 else "")
    return head, (b.get("headline") or answer).strip(), b.get("label") or "要点", items, b, foot


def brief_msg(job, ok, answer):
    head, line, label, items, b, foot = brief_parts(job, ok, answer)
    out = [f"{head}｜{job['title']}", "", line]
    if items:
        out += ["", f"【{label}】"] + [f"{i}. {s}" for i, s in enumerate(items, 1)]
    for k, name in (("todo", "没做完"), ("why", "为什么不算完成"), ("leftover", "手机上留下的")):
        if b.get(k) and not (k == "why" and ok):
            out += ["", f"【{name}】{b[k]}"]
    return "\n".join(out + ["", f"要接着做或改要求，直接在这里回我。", f"—— {foot}"])


def deliver(job, c, ok, answer, extra):
    head, line, label, items, b, foot = brief_parts(job, ok, answer)
    title = f"{head.split(' ', 1)[1]} · {job['title']}：{line}"[:180]
    facts = [f"{label}{i}={s}" for i, s in enumerate(items, 1)]
    facts += [f"{n}={b[k]}" for k, n in (("todo", "没做完"), ("why", "为什么"), ("leftover", "手机上留下")) if b.get(k)]
    if c.get("decide"):
        try:  # decide 的 --fact 只认「键=值」（11-01 前写成「任务 xxx：…」，推卡全失败且没记日志）
            r = subprocess.run([c["decide"], "q", "-p", job["project"], "--pname", c["decide_pname"],
                                *sum((["--fact", f] for f in facts), []), "--fact", f"任务={job['id']}：{job['task']}",
                                "--fact", f"复核细节={extra[:400]}",
                                title, "收到", "再跑一遍::手机再占 10–25 分钟", "要改::写清哪里要改", "--rec", "1"],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
            log(job, f"decide 推卡 rc={r.returncode} {(r.stdout.strip().splitlines() or [''])[-1][:80]} {r.stderr.strip()[-200:]}")
        except Exception as e:
            log(job, f"decide 推送失败：{e}")
    # 任务在 mini 上排队时收件人可能是空的（mini 配置没写 notify）：交付时退回本机配置的收件人
    if not job.get("quiet"):  # --notify none 的任务不发飞书
        feishu(c, job.get("notify") or c.get("notify") or "", brief_msg(job, ok, answer), job)


def run_job(job, c):
    """返回 "busy" 表示手机正被别的调用方占着，任务留在队列里稍后再试。"""
    hold = int(c["max_runtime"]) * int(c["attempts"]) + 900
    lk = kp(c, job, "lock", str(hold), "--note", f"rmq 任务「{job['title']}」")
    if lk.returncode == 75:
        why = lk.stderr.strip()
        if job.get("waiting") != why:
            log(job, "手机被占用，排队等：" + why); update(job, waiting=why)
        return "busy"
    try:
        run_held(job, c)
    finally:
        kp(c, job, "unlock")


def run_held(job, c):
    d = JOBS / job["id"]
    update(job, status="running", started=now(), waiting=None)
    hint, last = "", None
    try:  # 先确认看得到手机画面，GLKVM 掉登录时立刻报，不白跑二十分钟
        look = kp(c, job, "look", timeout=120).stdout
    except subprocess.TimeoutExpired:
        look = ""
    if not re.search(r"(?m)^\d+,\d+", look):
        msg = ("看不到手机画面：GLKVM 可能掉登录了。请在 ego lite 里打开 https://glkvm.local 登录，能看到手机后再重发任务。"
               if job.get("dev", "rmq1") == "rmq1" else f"看不到 {job['dev']} 的手机画面：查盒子 / 服务端是否在线（kp --dev {job['dev']} health）。")
        log(job, "预检失败 " + msg)
        last = (d, None, {"pass": False, "answer": msg, "issues": [msg]})
    adirs = []
    for k in range(1, (0 if last else int(c["attempts"])) + 1):
        if (d / "stop").exists():
            break
        adir = d / f"attempt{k}"; adir.mkdir(exist_ok=True); adirs.append(adir)
        log(job, f"第 {k} 轮驾驶开始（{job.get('driver') or c.get('driver') or c['ds_model']}）")
        kp(c, job, "key", "Meta+KeyH", "-q")
        t0 = time.time(); rc = drive(job, c, adir, hint)
        res = jload(adir / "result.json")
        log(job, f"驾驶结束 rc={rc} 用时 {int(time.time()-t0)}s result.json={'有' if res else '无'}")
        if not res:
            hint = "上一轮没有写出合法的 result.json，务必在结束前写。"; last = (adir, None, None); continue
        if rc == 124 and k < int(c["attempts"]) and not (d / "stop").exists():  # 到时间被收走：下一轮接着做，不复核、不从头来
            hint = ("上一轮到时间被收走了。下面是它已经拿到的结果，先把这些原样写进你的 result.json，"
                    "再只做还没做完的部分，已经查过的 App 和商品不要再查：" + json.dumps(res, ensure_ascii=False)[:2500])
            log(job, "到时间但有部分结果，下一轮接着做"); last = (adir, res, None); continue
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
        st["text"],
    ]))
    log(job, "小结 " + st["text"])
    brief = {k: (r or {}).get(k) for k in ("headline", "label", "items", "todo", "why", "leftover") if (r or {}).get(k)}
    update(job, status="stopped" if stopped else ("done" if ok else "failed"), finished=now(), answer=answer, detail=extra,
           result=str(adir / "result.json"), delivered=False, stats={k: v for k, v in st.items() if k != "text"}, brief=brief)
    if c.get("deliver") != "relay":
        deliver(job, c, ok, answer, extra); update(job, delivered=True)


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
            update(job, status="failed", finished=now(), detail=f"worker 出错：{e}", answer="worker 出错，任务没跑起来", delivered=False)
            log(job, f"出错：{e}"); import traceback; log(job, traceback.format_exc()[-1500:])


def worker(args):
    once = "--once" in args
    JOBS.mkdir(parents=True, exist_ok=True); Path("/tmp/kvmphone").mkdir(parents=True, exist_ok=True)
    # 重启后把卡在 running / reviewing 的任务标成失败，防止永远挂着
    for j in jobs():
        if j["status"] in ("running", "reviewing"):
            j["status"] = "failed"; j["detail"] = "worker 重启时任务中断"; j["answer"] = "worker 重启，任务中断"; j["delivered"] = False; jsave(JOBS / j["id"] / "job.json", j)
            for req in (JOBS / j["id"]).glob("attempt*/drive.req"):  # Gemini 在本机还开着：叫它停，别接着动手机（11-01 重启后它又独自跑了半小时）
                if not (req.parent / "drive.done").exists():
                    (req.parent / "drive.kill").touch()
    running, retry = {}, {}   # 设备 → 线程；任务 id → 下次重试时间（手机被占时）
    c = cfg()
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
                return run_one(j, retry)
            running[dev] = threading.Thread(target=run_one, args=(j, retry), daemon=True)
            running[dev].start()
        if once:
            return
        time.sleep(5)


def undelivered(_):
    print(json.dumps([j for j in jobs() if j["status"] in ("done", "failed", "stopped") and j.get("delivered") is False],
                     ensure_ascii=False))


def mark(args):
    j = jload(JOBS / args[0] / "job.json")
    if j: j["delivered"] = True; jsave(JOBS / args[0] / "job.json", j)


def relay(args):
    c = cfg(); r = c.get("remote")
    if not r: sys.exit("relay 要在配置里写 remote")
    JOBS.mkdir(parents=True, exist_ok=True)
    taken = set()
    threading.Thread(target=jev_tunnel, args=(r,), daemon=True).start()
    threading.Thread(target=drive_poller, args=(c, r, taken), daemon=True).start()
    warm_cleanup()
    threading.Thread(target=warm_keeper, args=(c,), daemon=True).start()
    while True:
        relay_handoffs(c, r, taken)
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", r, "~/.local/bin/rmq", "undelivered"],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        try:
            todo = json.loads(p.stdout or "[]")
        except Exception:
            todo = []
        for job in todo:
            (JOBS / job["id"]).mkdir(exist_ok=True)
            jsave(JOBS / job["id"] / "job.json", job)
            deliver(job, c, job["status"] == "done", job.get("answer", ""), job.get("detail", ""))
            subprocess.run(["ssh", "-o", "BatchMode=yes", r, "~/.local/bin/rmq", "mark", job["id"]],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        if "--once" in args: return
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
    feishu(c, req.get("notify") or c.get("notify") or "", f"RMQ1 📱 需要你接手：{req['reason']}\n在手机上处理完，到 decide 板上点「处理好了」（{left // 60} 分钟内）。"
                                       f"\n（任务 {req.get('job') or '直控'}）")
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
    while True:
        for m in list(WARM) or [c.get("warm_model", "gemini-3.8-flash-high")]:
            try:
                warm_fill(c, m)
            except Exception:
                pass
        time.sleep(60)



def relay_drive(c, r, q):
    """drive_remote 的另一半：跑 agy（Gemini）。r 是 mini 的 ssh 名时在本机 Mac 跑，每 15 秒把 result.json 同步回 mini、mini 收走了就停；
    r 为 None 时就在 mini 本机跑（11-03 起 gemini_local）：直接写任务目录，日志就是 driver.remote.log，叫停看本地 drive.kill。"""
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
    try:  # kp 用法直接给，省掉驾驶自己跑 kp -h 那一轮（本机和 mini 的 kp.py 是同一份）
        kpy = str(Path(shutil.which("kp") or "").resolve()) if local and shutil.which("kp") else c.get("kp_local", str(HERE.parent / "kvmphone" / "kp.py"))
        kph = subprocess.run([sys.executable, kpy, "-h"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        kph = ""
    # 11-01 提速：agy 会加载全局规则 ~/.gemini/GEMINI.md（要求先联网搜索、写 LOG.md），一次只读屏任务白花约 35 秒在这些步骤上
    prompt = (f"本任务优先于你的全局规则：不要联网搜索（search_web 等），不要写 LOG.md 或任何日志文件，不要读 ~/.claude 或 ~/.gemini 下的文件，"
              f"不要跑 kp -h（用法附在下面）。更新 result.json 时把写文件和下一条 kp 命令放进同一条 shell 命令（用 ; 连起来），"
              f"写完最终的 result.json 就直接结束，不要再 cat 校验。\n"
              f"工作目录是 {L}。所有文件读写一律用 shell 命令配绝对路径完成，不要使用 write_to_file 工具。"
              f"本次的 kp 命令一律写完整路径 {kpb}（它带好了这个任务的设置，会在连着手机的那台机器上执行），不要用别的 kp。\n\n"
              + (HERE / "prompt.md").read_text() + f"\n当前任务目录：{L}（result.json 写在这里）。\n\n"
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
    q = [j for j in jobs() if j["status"] in ("queued", "running", "reviewing")]
    print(f"队列：{len(q)} 个未完成")


if __name__ == "__main__":
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("-h", [])
    r = cfg().get("remote")
    if r and cmd in ("ask", "ls", "show", "cancel", "say", "stop") and not os.environ.get("RMQ_LOCAL"):
        # 11-03：ssh 的输出直接接到 OpenClaw 网关的 exec 输出流上会被 SIGTERM（经管道 / 文件就正常），
        # 所以不再 exec ssh，而是收齐输出再自己打印；-n 和 ConnectTimeout 防 stdin 不关、连不上时一直挂
        p = subprocess.run(["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", r,
                            " ".join(["~/.local/bin/rmq", cmd, *map(shlex.quote, rest)])],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
        sys.stdout.buffer.write(p.stdout); sys.stdout.flush()
        sys.stderr.buffer.write(p.stderr); sys.stderr.flush()
        sys.exit(p.returncode)
    fn = {"ask": ask, "ls": ls, "show": show, "cancel": cancel, "say": say_, "stop": stop, "worker": worker, "doctor": doctor,
          "undelivered": undelivered, "mark": mark, "relay": relay, "handoffs": handoffs, "handoff": handoff_mark,
          "rwait": rwait, "drives": drives}.get(cmd)
    if not fn:
        print(__doc__); sys.exit(0 if cmd in ("-h", "--help") else 64)
    fn(rest)
