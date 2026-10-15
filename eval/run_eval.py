# -*- coding: utf-8 -*-
"""
run_eval.py —— 批③ 评测跑分器：一条命令出一份报告。

用法（在 S5 目录下，用 eval.cmd 包一层，不用手动激活 venv）：
    eval.cmd                          跑全部用例
    eval.cmd --limit 5                只跑前 5 条（省钱试水）
    eval.cmd --cat rag                只跑某一类
    eval.cmd --tag before             给这轮起名，方便两轮对比
    eval.cmd --compare eval\\report_xxx.json   和上一轮比"变好还是变差"
    eval.cmd --stats                  不出模型，只看 logs\\run.jsonl 的汇总

三条设计原则：
  1. 每条用例一个【全新 session_id】。有记忆的系统里复用 session，上一题的工具结果
     会留在历史里 → 模型直接照抄、根本不调工具（批②已踩过这个坑：测试铁律）。
  2. 判分先用【规则】（工具断言 / 关键词 / 拒答口径），不上 LLM 评委 ——
     规则判分零成本、可复现；LLM 评委要花钱，真需要时再说。
  3. 故障注入：把某个工具临时换成"慢桩 / 报错桩"，让"超时 / 重试"变成
     【可复现】的实验，而不是"等哪天网络自己挂掉"。
"""
import argparse
import asyncio
import json
import os
import sys
import time
import unicodedata
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # = S5 目录
sys.path.insert(0, BASE_DIR)          # 保证 `import agent` 找得到
# 前置检查：agent 一 import 就会读这几个环境变量，缺了会抛 KeyError，很难看懂 → 先说清楚
_NEEDED_ENV = ["ZHIPU_API_KEY", "DEEPSEEK_API_KEY", "S6_DB_PASSWORD"]
_missing = [k for k in _NEEDED_ENV if not os.environ.get(k)]
if _missing:
    raise SystemExit("缺少环境变量：" + "、".join(_missing)
                     + "\n请先在本机把它们设好（setx ... 之后必须重开终端 / 重启 IDE）。")

import agent                          # noqa: E402
import obs                            # noqa: E402

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
CASES_FILE = os.path.join(EVAL_DIR, "cases.jsonl")

# 拒答口径：SYSTEM_PROMPT 定的是"没有检索到相关内容"，这里把常见变体都算上
REFUSE_MARKERS = ["没有检索到", "没检索到", "未检索到", "没有相关",
                  "查不到", "资料里没有", "没有这方面"]


# ============ ① 故障注入：慢桩 / 报错桩 ============
class SlowStub:
    """慢桩：不睡够不返回。用来把"单工具超时"变成可复现的实验。"""
    def __init__(self, sleep_s):
        self.sleep_s = sleep_s

    async def ainvoke(self, args):
        await asyncio.sleep(self.sleep_s)
        return f"慢桩睡够了 {self.sleep_s}s"


class ErrorStub:
    """报错桩：一调就抛。用来把"幂等重试"变成可复现的实验。"""
    def __init__(self, msg):
        self.msg = msg

    async def ainvoke(self, args):
        raise RuntimeError(self.msg)


class Injector:
    """临时替换 LC_TOOL_MAP 里的工具 / TOOL_TIMEOUT，用完还原。

    为什么替换 LC_TOOL_MAP 而不是 LC_TOOLS：模型只负责"挑工具名"，真正执行
    永远走 LC_TOOL_MAP[name].ainvoke(args)（见 agent.py 的 _call_tool_once）。
    所以只要换掉 map 里的执行体，注入就生效，且不用碰工具声明的 schema。
    """
    def __init__(self):
        self._saved = {}

    def apply(self, inject):
        if not inject:
            return
        name = inject.get("slow_tool") or inject.get("error_tool")
        if name:
            self._saved[name] = agent.LC_TOOL_MAP.get(name)
            if inject.get("error_tool"):
                agent.LC_TOOL_MAP[name] = ErrorStub(f"注入故障：{name} 挂了")
            else:
                agent.LC_TOOL_MAP[name] = SlowStub(inject.get("sleep_s", 30))
        if inject.get("timeout_s") is not None:
            self._saved["__TOOL_TIMEOUT__"] = agent.TOOL_TIMEOUT
            agent.TOOL_TIMEOUT = inject["timeout_s"]

    def restore(self):
        for k, v in self._saved.items():
            if k == "__TOOL_TIMEOUT__":
                agent.TOOL_TIMEOUT = v
            else:
                agent.LC_TOOL_MAP[k] = v
        self._saved.clear()


# 🔴 判分第一步永远是"归一化"：别去枚举模型的措辞变体，先把"看起来一样"的字符统一。
#    踩过的坑：模型写 "n−1"（U+2212 数学减号），词表里写 "n-1"（ASCII 连字符）→ 判失败。
#    枚举变体永远追不上措辞；归一化一次解决一整类。
_MINUS_FAMILY = "−–—‐‑‒―ー"


def _norm(s):
    s = unicodedata.normalize("NFKC", str(s))     # 全角 → 半角（１ → 1、－ → -）
    for ch in _MINUS_FAMILY:
        s = s.replace(ch, "-")                    # 横线/减号一族 → ASCII 连字符
    return "".join(s.split()).lower()             # 去掉所有空白 + 统一大小写


# ============ ② 判分：只看"够不够格"，不评价文采 ============
def judge(text, meta, exp):
    """返回问题清单；空列表 = 这条用例这个 turn 通过。"""
    problems = []
    called = meta.get("tools") or []
    ntext = _norm(text)                                # 归一化后的回答，所有文字判据都拿它比

    for t in exp.get("tools", []):                     # 必须调的工具
        if t not in called:
            problems.append(f"没调用 {t}")
    for t in exp.get("forbid_tools", []):              # 不许调的工具（过度调用）
        if t in called:
            problems.append(f"不该调用 {t}")

    # 🔴 判分分层：机器足迹（meta）优先，模型的话（text）只作辅助。
    #    keywords_soft=True = "这句话怎么复述不影响判分"——用于行为已被 tools / interrupt
    #    这些撒不了谎的证据证明过的用例（模型把工具返回改写成自己的话，不是错）。
    soft = bool(exp.get("keywords_soft"))

    def _kw(note):
        if soft:
            print(f"      · 软提示（不判失败）：{note}")
        else:
            problems.append(note)

    pool = exp.get("keywords_any_pool")                # 池子里命中够 N 个即可
    if pool:
        need = exp.get("keywords_min", 1)
        got = [k for k in pool if _norm(k) in ntext]
        if len(got) < need:
            _kw(f"关键词命中 {len(got)}/{need}（命中：{'/'.join(got) or '无'}）")

    ka = exp.get("keywords_any", [])                   # 至少命中一个
    if ka and not any(_norm(k) in ntext for k in ka):
        _kw("关键词一个都没命中：" + "/".join(ka))

    for k in exp.get("keywords_all", []):              # 全部必须命中
        if _norm(k) not in ntext:
            _kw(f"缺关键词 {k}")

    # 副作用（file_contains）不在这里判：它必须看"跑之前 → 跑之后"的差量，
    # 那是 run_turn 的活（见 _snapshot_files / _check_side_effects）。

    refused = any(_norm(m) in ntext for m in REFUSE_MARKERS)   # 拒答口径出现了没

    if exp.get("refuse"):                              # 该拒答就必须拒答
        if not refused:
            problems.append("该拒答却没拒答（没出现『没有检索到』这类口径）")

    # 🔴 拒答成立 = "没编造"这件事已经被证明了。此时再出现的违禁词，多半是模型在说
    #    "我没检索到罗尔定理 / 拉格朗日定理" —— 那是**否定句里的词**，不是幻觉。
    #    所以：拒答成立时，违禁词表降级为软提示，不许二次判罪。
    refuse_proved = bool(exp.get("refuse")) and refused
    for k in exp.get("not_contains", []):              # 出现了就是串味/幻觉
        if _norm(k) in ntext:
            if refuse_proved:
                print(f"      · 软提示（拒答已成立，词表只提醒）：出现了『{k}』")
            else:
                problems.append(f"出现了不该出现的内容：{k}")

    if exp.get("retries_min") is not None:
        n = len(meta.get("retries") or [])
        if n < exp["retries_min"]:
            problems.append(f"重试次数 {n} < 期望 {exp['retries_min']}")

    if exp.get("interrupt") is not None:
        if bool(meta.get("interrupt")) != bool(exp["interrupt"]):
            problems.append(f"人工确认点期望 {exp['interrupt']}，实际 {meta.get('interrupt')}")

    return problems


def merge_meta(a, b):
    """两次调用（先停车 → 再 resume）的统计合起来算作同一个 turn。"""
    ua, ub = a.get("usage") or {}, b.get("usage") or {}
    return {
        "usage": {k: (ua.get(k, 0) or 0) + (ub.get(k, 0) or 0) for k in ("input", "output", "total")},
        "tools": (a.get("tools") or []) + (b.get("tools") or []),
        "n_tool_calls": (a.get("n_tool_calls") or 0) + (b.get("n_tool_calls") or 0),
        "retries": (a.get("retries") or []) + (b.get("retries") or []),
        "timeout": bool(b.get("timeout")),
        # 🔴 这里必须是"或"：a 停车等人（interrupt=True）、b 续跑完（False），
        #    整条用例确实发生过人工确认，用 b 的值会把这件事抹掉。
        "interrupt": bool(a.get("interrupt")) or bool(b.get("interrupt")),
    }


# ============ ③ 跑一个 turn ============
# 对端抽风的特征串：命中 = 环境的锅，不记在系统头上
_ENV_HINTS = ("503", "service_unavailable", "service is too busy",
              "429", "rate limit", "apiconnectionerror", "connection error", "timed out")


def _is_env_error(e):
    s = f"{type(e).__name__}: {e}".lower()
    return any(h in s for h in _ENV_HINTS)


async def run_turn_retry(case, turn, session_id, attempts=3):
    """环境的锅就等一会儿再来；非环境错误立刻抛出（真 bug 不该被重试掩盖）。"""
    for i in range(1, attempts + 1):
        try:
            return await run_turn(case, turn, session_id)
        except Exception as e:
            if not _is_env_error(e) or i == attempts:
                raise
            wait = 3 * i                       # 实验：改成 1 会怎样？改成 10 呢？
            print(f"      · 环境异常第 {i} 次：{type(e).__name__} → 等 {wait}s 重试")
            await asyncio.sleep(wait)


def _abs(p):
    return p if os.path.isabs(p) else os.path.join(os.path.dirname(EVAL_DIR), p)


def _read_or_empty(p):
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _snapshot_files(exp):
    """🔴 跑之前先量一遍：只认"这次新增的"，不认"上次留下的"。
    （tool-07 的笔记文件会被历次运行反复追加 —— 不量差量就等于永远判通过。）"""
    return {chk["path"]: _read_or_empty(_abs(chk["path"]))
            for chk in exp.get("file_contains", [])}


def _check_side_effects(snap, exp):
    """副作用验证：文件必须真的**多出**一条包含期望内容的新记录。"""
    problems = []
    for chk in exp.get("file_contains", []):
        p = chk["path"]
        before = snap.get(p, "")
        after = _read_or_empty(_abs(p))
        if not after.startswith(before):                 # 旧内容被改写/清空，不是追加
            problems.append(f"副作用验证失败：{p} 不是追加式改动（旧内容被动过）")
            continue
        new_part = after[len(before):]                   # 只看本次新增的那一段
        if chk["text"] not in new_part:
            problems.append(f"副作用验证失败：{p} 本次新增的内容里没有『{chk['text']}』")
    return problems


async def run_turn(case, turn, session_id):
    q = turn["q"]
    exp = turn.get("expect", {})
    t0 = time.monotonic()
    snap = _snapshot_files(exp)                          # 🔴 先量，再跑

    text, meta = await agent.graph_answer(session_id, q, with_meta=True)

    if meta.get("interrupt") and turn.get("resume"):    # 危险工具停车 → 评测机替人回 y
        print(f"      · 停在人工确认 → 自动回 {turn['resume']!r}")
        text2, meta2 = await agent.graph_answer(session_id, resume=turn["resume"], with_meta=True)
        text, meta = text2, merge_meta(meta, meta2)

    latency_ms = round((time.monotonic() - t0) * 1000)
    problems = judge(str(text), meta, exp)
    problems += _check_side_effects(snap, exp)           # 只认"这次新增的"那一段

    obs.log_event(kind="eval_turn", case=case["id"], cat=case["cat"], q=q,
                  session_id=session_id, ok=not problems, problems=problems,
                  latency_ms=latency_ms, usage=meta.get("usage"),
                  tools=meta.get("tools"), n_tool_calls=meta.get("n_tool_calls"),
                  retries=meta.get("retries"), timeout=meta.get("timeout"),
                  interrupt=meta.get("interrupt"), text=str(text)[:500])

    return {
        "q": q, "ok": not problems, "problems": problems,
        "text": str(text)[:300],
        "tools": meta.get("tools") or [], "retries": len(meta.get("retries") or []),
        "latency_ms": latency_ms, "tokens": (meta.get("usage") or {}).get("total", 0),
    }


# ============ ④ 报告 ============
def _pct(nums, q):
    nums = sorted(nums)
    if not nums:
        return 0
    return nums[min(len(nums) - 1, int(round(q * (len(nums) - 1))))]


def build_report(run_id, tag, results):
    turns = [t for c in results for t in c["turns"]]
    lat = [t["latency_ms"] for t in turns]
    tok = [t["tokens"] for t in turns]
    scored = [c for c in results if not c.get("env")]     # 🔴 环境异常不进分母
    n_env = len(results) - len(scored)
    n_pass = sum(1 for c in scored if c["ok"])
    by_cat = {}
    for c in results:
        b = by_cat.setdefault(c["cat"], {"n": 0, "pass": 0, "env": 0, "lat": [], "tok": []})
        if c.get("env"):
            b["env"] += 1
        else:
            b["n"] += 1
            b["pass"] += 1 if c["ok"] else 0
        b["lat"] += [t["latency_ms"] for t in c["turns"]]
        b["tok"] += [t["tokens"] for t in c["turns"]]
    for cat, b in by_cat.items():
        b["rate"] = round(100.0 * b["pass"] / max(1, b["n"]), 1)
        b["avg_latency_ms"] = round(sum(b.pop("lat")) / max(1, b["n"]))
        b["avg_tokens"] = round(sum(b.pop("tok")) / max(1, b["n"]))
    return {
        "run_id": run_id, "tag": tag, "ts": datetime.now().isoformat(timespec="seconds"),
        "n_cases": len(results), "n_pass": n_pass, "n_env": n_env,  # 新
        "pass_rate": round(100.0 * n_pass / max(1, len(scored)), 1),  # 分母换成 scored
        "summary": {
            "n_turns": len(turns),
            "avg_latency_ms": round(sum(lat) / max(1, len(lat))),
            "p95_latency_ms": _pct(lat, 0.95),
            "avg_total_tokens": round(sum(tok) / max(1, len(tok))),
            "sum_total_tokens": sum(tok),
            "retries": sum(t["retries"] for t in turns),
        },
        "by_cat": by_cat,
        "cases": results,
    }


def save_report(rep):
    base = os.path.join(EVAL_DIR, f"report_{rep['run_id']}")
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    lines = [
        f"# 评测报告 {rep['run_id']}" + (f"（tag={rep['tag']}）" if rep["tag"] else ""),
        "",
        f"- 用例 **{rep['n_cases']}** · 通过 **{rep['n_pass']}**（{rep['pass_rate']}%）",
        f"- 平均延迟 **{rep['summary']['avg_latency_ms']} ms** · p95 **{rep['summary']['p95_latency_ms']} ms**",
        f"- 平均 token **{rep['summary']['avg_total_tokens']}**（合计 {rep['summary']['sum_total_tokens']}）",
        f"- 重试 {rep['summary']['retries']} 次 · turn 数 {rep['summary']['n_turns']}",
        f"- 环境异常（不计分）**{rep.get('n_env', 0)}** 条",
        "",
        "| 类别 | 用例 | 通过 | 通过率 | 平均延迟 | 平均 token |",
        "|---|---|---|---|---|---|",
    ]
    for cat, b in sorted(rep["by_cat"].items()):
        lines.append(f"| {cat} | {b['n']} | {b['pass']} | {b['rate']}% | {b['avg_latency_ms']} ms | {b['avg_tokens']} |")
    bad = [c for c in rep["cases"] if not c["ok"] and not c.get("env")]
    if bad:
        lines += ["", "## 没过的用例（每条都值得去看一眼）", ""]
        for c in bad:
            for i, t in enumerate(c["turns"], 1):
                if not t["ok"]:
                    lines.append(f"- **{c['id']}**（{c['cat']}）第 {i} 问：{c['note']}")
                    lines.append(f"  - 问题：{'；'.join(t['problems'])}")
                    lines.append(f"  - 答：{t['text'][:150]}")
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return base + ".json", base + ".md"


def print_report(rep):
    print()
    print(f"===== 评测结果 {rep['run_id']} =====")
    print(f"通过 {rep['n_pass']}/{rep['n_cases']}  ({rep['pass_rate']}%)")
    print(f"平均延迟 {rep['summary']['avg_latency_ms']} ms · p95 {rep['summary']['p95_latency_ms']} ms "
          f"· 平均 token {rep['summary']['avg_total_tokens']} · 重试 {rep['summary']['retries']}")
    print(f"{'类别':<10}{'用例':>6}{'通过':>6}{'通过率':>9}{'平均延迟':>12}{'平均token':>11}")
    for cat, b in sorted(rep["by_cat"].items()):
        print(f"{cat:<10}{b['n']:>6}{b['pass']:>6}{str(b['rate'])+'%':>9}{str(b['avg_latency_ms'])+'ms':>12}{b['avg_tokens']:>11}")


def compare_with(old_path, rep):
    with open(old_path, "r", encoding="utf-8") as f:
        old = json.load(f)
    print()
    print(f"===== 对比：{old['run_id']} → {rep['run_id']} =====")

    def delta(label, a, b, unit="", lower_better=True):
        d = b - a
        if d == 0:
            verdict = "一样"
        elif (d < 0) == lower_better:
            verdict = "✅ 变好"
        else:
            verdict = "🔴 变差"
        print(f"  {label:<12} {a}{unit} → {b}{unit}   ({d:+})  {verdict}")

    delta("通过率", old["pass_rate"], rep["pass_rate"], "%", lower_better=False)
    delta("平均延迟", old["summary"]["avg_latency_ms"], rep["summary"]["avg_latency_ms"], "ms")
    delta("平均token", old["summary"]["avg_total_tokens"], rep["summary"]["avg_total_tokens"])

    old_ok = {c["id"]: c["ok"] for c in old["cases"]}
    new_bad = [c["id"] for c in rep["cases"] if not c["ok"] and old_ok.get(c["id"])]
    new_ok = [c["id"] for c in rep["cases"] if c["ok"] and old_ok.get(c["id"]) is False]
    if new_bad:
        print(f"  🔴 由通过变失败：{', '.join(new_bad)}")
    if new_ok:
        print(f"  ✅ 由失败变通过：{', '.join(new_ok)}")
    if not new_bad and not new_ok:
        print("  （每条用例的通过/失败状态都没有变）")


# ============ ⑤ 主流程 ============
def load_cases():
    if not os.path.exists(CASES_FILE):
        raise SystemExit(f"找不到用例文件：{CASES_FILE}")
    cases = []
    with open(CASES_FILE, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(f"cases.jsonl 第 {i} 行不是合法 JSON：{e}")
    return cases


def print_stability(results):
    """🔴 同一道题重复跑 N 轮：结果不一致的题就是 flaky，
    它意味着"改前改后各跑一次然后比"这种做法在这套评测上不成立。"""
    seq = {}
    for c in results:
        mark = "P" if c["ok"] else ("E" if c.get("env") else "F")
        seq.setdefault(c["id"], []).append(mark)
    unstable = [cid for cid, s in seq.items() if len(set(s)) > 1]
    print("\n===== 稳定性（同一道题重复跑）=====")
    for cid, s in seq.items():
        flag = "⚠️ 不稳定" if len(set(s)) > 1 else "✅ 稳定"
        print(f"  {cid:<12} {' '.join(s)}   {flag}")
    if unstable:
        print(f"\n抖动率：{len(unstable)}/{len(seq)} 道题结果不一致 → {'、'.join(unstable)}")
        print("⚠️ 这些题的差异，小于这份评测自身的噪声，别拿它们当改好/改坏的证据。")
    else:
        print(f"\n抖动率：0/{len(seq)} → 这次没抓到翻转")
        n = len(next(iter(seq.values())))
        if n < 30:           # n≥30 时，失败率的 95% 上界才降到 10% 以下
            print(f"⚠️ 但每题只跑了 {n} 次：0 次失败时，失败率的 95% 上界 ≈ 3/n ≈ {round(300 / n)}% ——")
            print("   '稳定'这个词，样本量不够就不能用（n=3 的上界是 100%，等于什么都没说）。")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条")
    ap.add_argument("--cat", default="", help="只跑某一类")
    ap.add_argument("--only", default="", help="只跑指定用例 id，逗号分隔（如 rag-10,tool-04）")
    ap.add_argument("--repeat", type=int, default=1, help="同一批用例重复跑 N 轮，用来看抖动")
    ap.add_argument("--tag", default="", help="给这轮报告起名")
    ap.add_argument("--compare", default="", help="跟上一份 report_*.json 对比")
    ap.add_argument("--stats", action="store_true", help="只看日志汇总，不调模型")
    args = ap.parse_args()

    if args.stats:                                   # 零成本模式
        obs.print_summary(obs.summarize(obs.read_events()))
        sys.stdout.flush()
        os._exit(0)

    cases = load_cases()
    if args.cat:
        cases = [c for c in cases if c["cat"] == args.cat]
    if args.only:                                    # 只跑点名的几条（用来量化抖动）
        want = {x.strip() for x in args.only.split(",") if x.strip()}
        cases = [c for c in cases if c["id"] in want]
    if args.limit:
        cases = cases[:args.limit]
    if not cases:
        raise SystemExit("没有选中任何用例（检查 --cat / --limit）")

    agent._answer_cache.clear()      # 干净环境：别让上一轮的缓存混进来
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + (f"-{args.tag}" if args.tag else "")
    rounds = max(1, args.repeat)
    plan = [(r, c) for r in range(1, rounds + 1) for c in cases]   # 重复跑：每轮独立 session
    print(f"===== 开跑 {run_id} · {len(cases)} 条用例 × {rounds} 轮 = {len(plan)} 次 =====")

    results = []
    try:
        for i, (rnd, case) in enumerate(plan, 1):
            inj = Injector()
            inj.apply(case.get("inject"))
            sid = f"eval-{run_id}-{case['id']}-r{rnd}"
            turns = []
            try:
                for ti, turn in enumerate(case["turns"], 1):
                    if turn.get("new_session"):
                        sid = f"eval-{run_id}-{case['id']}-r{rnd}-s{ti}"
                    turns.append(await run_turn_retry(case, turn, sid))
            except Exception as e:
                env = _is_env_error(e)  # 503/429/断连 = 环境问题，不是我们的 bug
                label = "环境异常（不计分）" if env else "脚本异常"
                turns.append({"q": case["turns"][0]["q"], "ok": False, "env": env,
                              "problems": [f"{label}：{type(e).__name__}: {e}"],
                              "text": "", "tools": [], "retries": 0,
                              "latency_ms": 0, "tokens": 0})
            finally:
                inj.restore()

            is_env = any(t.get("env") for t in turns)
            ok = all(t["ok"] for t in turns) and not is_env
            lat = sum(t["latency_ms"] for t in turns)
            tools = [x for t in turns for x in t["tools"]]
            results.append({"id": case["id"], "cat": case["cat"], "note": case.get("note", ""),
                            "ok": ok, "env": is_env, "round": rnd, "latency_ms": lat, "turns": turns})
            verdict = "ENV " if is_env else ("PASS" if ok else "FAIL")
            print(f"[{i:>2}/{len(plan)}] {verdict}  {case['id']:<12} {case['cat']:<9} "
                  f"{lat:>6}ms  tools={tools or '-'}")
            for t in turns:
                if not t["ok"]:
                    print("        └─ " + "；".join(t["problems"]))
                    print("           答：" + t["text"][:120].replace("\n", " "))
    finally:
        await agent.shutdown_graph()            # 关 sqlite 连接 → 不再刷 Event loop is closed

    rep = build_report(run_id, args.tag, results)
    json_path, md_path = save_report(rep)
    print_report(rep)
    print(f"\n报告：{json_path}")
    print(f"     {md_path}")
    if rounds > 1:
        print_stability(results)
    if args.compare:
        compare_with(args.compare, rep)

    sys.stdout.flush()
    os._exit(0)     # 硬退出：别让残留的异步任务/慢桩把进程吊住（跟 p.cmd 一个套路）


if __name__ == "__main__":
    asyncio.run(main())