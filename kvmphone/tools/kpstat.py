#!/usr/bin/env python3
"""kpstat：统计 kp.jsonl（一个或多个文件，或任务目录 / attempt 目录）。

用法：kpstat.py <kp.jsonl | 任务目录> [...]
输出：各命令次数与耗时（中位 / p90）、各阶段占比、JEV 屏类型分布与 other 占比、JEV 时延、「点完屏幕文字没变」比例。

日志结构（kp.py klog）：cmd 行 = 一条命令开始；screen 行 = 这屏 OCR 文字；jev_screen 行 = JEV 判屏；
act_done 行 = 点击类命令的分段耗时（K0 之后才有，旧日志没有就只用 ts 间隔退化统计）。
命令耗时 = cmd 行到该命令最后一条记录的 ts 差（旧日志的唯一来源）；思考间隔 = 该命令最后记录到下一条 cmd 的 ts 差。
"""
import json, os, sys
from collections import Counter, defaultdict

ACTS = {"tap", "type", "paste", "key", "up", "down", "back", "open", "handoff"}
STAGES = ["dur_pre_read", "dur_pace", "dur_act", "dur_settle", "dur_ocr", "dur_jev"]


def files_of(arg):
    if os.path.isfile(arg):
        return [arg]
    out = []
    for root, _, fs in os.walk(arg):
        if "kp.jsonl" in fs:
            out.append(os.path.join(root, "kp.jsonl"))
    return sorted(out)


def load(path):
    rows = []
    for line in open(path, errors="ignore"):
        try:
            r = json.loads(line)
            if isinstance(r, dict) and "ts" in r:
                rows.append(r)
        except Exception:
            pass
    return rows


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]


def med(xs):
    return pct(xs, 0.5)


def fmt(v, unit="s"):
    if v is None:
        return "-"
    return f"{v:.2f}{unit}" if unit == "s" else f"{v:.0f}{unit}"


def words(screen):
    its = [x.strip() for x in screen.split(" | ")]
    return set(its[2:])  # 去掉状态栏时间 / 电量


def same(a, b):
    if not a and not b:
        return True
    return len(a & b) >= 0.9 * max(len(a | b), 1)


def split_cmds(rows):
    """按 cmd 行切成命令段：每段 = [cmd 行, 后续记录...]；cmd 之前的记录丢弃。"""
    segs = []
    for r in rows:
        if "cmd" in r:
            segs.append([r])
        elif segs:
            segs[-1].append(r)
    return segs


def main(argv):
    if not argv:
        print(__doc__); return 2
    paths = [p for a in argv for p in files_of(a)]
    if not paths:
        print("没找到 kp.jsonl"); return 1
    dur = defaultdict(list)        # cmd -> 耗时（秒）
    think = []                     # 命令结束到下一条命令开始
    stage = defaultdict(list)      # 阶段 -> ms 列表
    stage_tot = []                 # 同一步的 dur_total
    stage_sum = defaultdict(float)
    jev_scr, jev_ms, jev_url = Counter(), [], Counter()
    unchanged = [0, 0]             # 点完文字没变 / 点完有屏
    steer = guard = 0
    new_fields = 0
    for p in paths:
        rows = load(p)
        segs = split_cmds(rows)
        prev_screen = None
        for i, seg in enumerate(segs):
            c = seg[0]["cmd"]
            end = seg[-1]["ts"]
            dur[c].append(end - seg[0]["ts"])
            if i + 1 < len(segs):
                think.append(segs[i + 1][0]["ts"] - end)
            screens = [r for r in seg if "screen" in r]
            # 点完文字没变：这条命令之后的第一屏 vs 命令之前最后一屏
            if c in ACTS and screens and prev_screen is not None:
                unchanged[1] += 1
                unchanged[0] += same(words(screens[0]["screen"]), words(prev_screen))
            if screens:
                prev_screen = screens[-1]["screen"]
            for r in seg[1:]:
                if "jev_screen" in r:
                    if r["jev_screen"] is not None:
                        jev_scr[r["jev_screen"]] += 1
                    if r.get("jev_ms") is not None:
                        jev_ms.append(r["jev_ms"])
                    elif r.get("dur_jev") is not None and r["jev_screen"] is not None:
                        jev_ms.append(r["dur_jev"])
                    if r.get("jev_url"):
                        jev_url[r["jev_url"]] += 1
                    elif "dur_jev" in r:
                        jev_url["(不可用)"] += 1
                if "steer_ack" in r:
                    steer += 1
                if "guard" in r:
                    guard += 1
                if r.get("act_done"):
                    new_fields += 1
                    tot = r.get("dur_total")
                    if tot:
                        stage_tot.append(tot)
                    for k in STAGES:
                        if r.get(k) is not None:
                            stage[k].append(r[k]); stage_sum[k] += r[k]
                    # dur_jev 在 jev_screen 行里；并进同一步的阶段统计
                    jr = next((x for x in seg if "dur_jev" in x), None)
                    if jr and jr.get("dur_jev") is not None and "dur_jev" not in r:
                        stage["dur_jev"].append(jr["dur_jev"]); stage_sum["dur_jev"] += jr["dur_jev"]
    n_cmd = sum(len(v) for v in dur.values())
    print(f"文件 {len(paths)} 个，命令 {n_cmd} 条，带分段计时的动作 {new_fields} 条\n")
    print("== 各命令耗时（cmd 行到该命令最后一条记录；无输出的命令为 0）==")
    print(f"{'命令':<10}{'次数':>6}{'中位':>9}{'p90':>9}{'最大':>9}")
    for c, v in sorted(dur.items(), key=lambda kv: -len(kv[1])):
        print(f"{c:<10}{len(v):>6}{fmt(med(v)):>9}{fmt(pct(v, .9)):>9}{fmt(max(v)):>9}")
    print(f"\n命令之间的间隔（模型思考 + 排队）：中位 {fmt(med(think))}，p90 {fmt(pct(think, .9))}，共 {len(think)} 段")
    print("\n== 各阶段占比（需要 K0 之后的日志）==")
    if stage_tot:
        tsum = sum(stage_tot)
        print(f"{'阶段':<14}{'条数':>6}{'中位':>9}{'p90':>9}{'占 dur_total':>14}")
        for k in STAGES:
            v = stage.get(k)
            if v:
                print(f"{k:<14}{len(v):>6}{fmt(med(v), 'ms'):>9}{fmt(pct(v, .9), 'ms'):>9}{stage_sum[k] / tsum * 100:>13.1f}%")
        print("（dur_ocr 是 dur_settle 里的一部分，占比不要相加；dur_pace 是节奏停留）")
        print(f"{'dur_total':<14}{len(stage_tot):>6}{fmt(med(stage_tot), 'ms'):>9}{fmt(pct(stage_tot, .9), 'ms'):>9}")
    else:
        print("（日志里没有 act_done 分段字段，旧日志只能看上面的 ts 间隔）")
    print("\n== JEV 屏类型 ==")
    tot = sum(jev_scr.values())
    for k, v in jev_scr.most_common():
        print(f"{k:<16}{v:>6}{v / tot * 100:>8.1f}%")
    if tot:
        print(f"other 占比 {jev_scr.get('other', 0) / tot * 100:.1f}%（共 {tot} 次判屏）")
    print(f"JEV 时延：中位 {fmt(med(jev_ms), 'ms')}，p90 {fmt(pct(jev_ms, .9), 'ms')}，样本 {len(jev_ms)}")
    if jev_url:
        print("JEV 地址：" + "，".join(f"{k} × {v}" for k, v in jev_url.items()))
    print("\n== 点完屏幕文字没变 ==")
    if unchanged[1]:
        print(f"{unchanged[0]} / {unchanged[1]} = {unchanged[0] / unchanged[1] * 100:.1f}%（动作类命令后第一屏与前一屏文字重合 ≥ 90%）")
    else:
        print("没有可比较的动作前后屏")
    print(f"\n用户插话转达 {steer} 次，护栏拦截 {guard} 次")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
