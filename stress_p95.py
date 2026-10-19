# ===== S8 / stress_p95.py — 并发压测：p50/p95/p99 + 失败率 + 拦截/命中统计 =====
"""两种模式，两种用途：

  --mock   只压【框架开销】（两级缓存 + 限流闸门），不打模型 → 零成本，看闸门本身加了几个毫秒
  默认     压【端到端】真实问答 → 花钱，样本要小（默认 6 条）

三条压测纪律（不改就别比）：
  1. 同分母：并发数 / 缓存开关 / 限流开关 / 对端状态，一次只动一个，否则 delta 是假的。
  2. 看 p95/p99，不看平均值：平均值会被大量快请求洗白。
  3. 每条请求一个全新 session_id：有记忆的系统里复用会话 → 历史越滚越大，压的是 token 不是并发。

用法（在 S5 目录下，用 venv 解释器）：
  .venv\\Scripts\\python.exe stress_p95.py --mock --n 200 --conc 20
  .venv\\Scripts\\python.exe stress_p95.py --n 6 --conc 1
  .venv\\Scripts\\python.exe stress_p95.py --n 6 --conc 6
"""
import argparse, asyncio, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # 不设会按 GBK 编崩
except Exception:
    pass

QUESTIONS = [
    "拉格朗日中值定理讲的是什么？",
    "柯西中值定理和拉格朗日中值定理什么关系？",
    "泰勒公式里的余项是干什么的？",
    "定积分的几何意义是什么？",
    "什么是矩阵的秩？",
    "罗尔定理的三个条件是什么？",
]


def pct(nums, q):
    """分位数：先排序，再按位置取（n 小时 p95 就是最慢那一两个）。"""
    if not nums:
        return 0
    s = sorted(nums)
    i = min(len(s) - 1, int(round((len(s) - 1) * q)))
    return s[i]


async def one_real(i, sem, out, uid):
    import agent                      # 延迟导入：--mock 模式不必拉起 agent 的一堆依赖
    async with sem:
        sid = f"stress-{time.strftime('%H%M%S')}-{i}"    # 🔴 每条一个全新会话
        q = QUESTIONS[i % len(QUESTIONS)]
        t0 = time.perf_counter()
        try:
            # 🔴 user_id 是【限流维度】，和 session_id 分开传：所有请求共用一个身份，限流才数得起来
            text, meta = await agent.graph_answer(sid, q, with_meta=True, user_id=uid)
            out.append({"i": i, "ms": (time.perf_counter() - t0) * 1000,
                        "ok": True, "meta": meta, "text": (text or "")[:60]})
        except Exception as e:
            out.append({"i": i, "ms": (time.perf_counter() - t0) * 1000,
                        "ok": False, "meta": {}, "err": f"{type(e).__name__}: {e}"})


async def one_mock(i, sem, out):
    import cache, ratelimit
    async with sem:
        t0 = time.perf_counter()
        k = cache.make_key(QUESTIONS[i % len(QUESTIONS)], f"mock-{i}", 0)
        cache.default().get(k)                       # 只测"查缓存"这一跳
        if ratelimit.enabled():
            ratelimit.default().check(f"mock-user-{i % 3}")
        out.append({"i": i, "ms": (time.perf_counter() - t0) * 1000, "ok": True, "meta": {}})


async def show_threads():
    """零成本排查退出噪音：把 agent 的依赖全拉起来，列出进程内线程。
    判读：非 daemon 且 alive 的线程 = 解释器退出时要等它，等不到就刷
    'Exception ignored while joining a thread ... KeyboardInterrupt'。"""
    import threading
    import agent
    await agent.warmup()
    await agent.shutdown_graph()
    print("== 进程内线程（daemon=False 且 alive=True = 退出噪音的嫌疑犯）==")
    for t in threading.enumerate():
        print(f"  daemon={t.daemon}  alive={t.is_alive()}  name={t.name}")
    print("== join 2s 测试（alive_after_2s=True = 退出噪音元凶）==")
    for t in threading.enumerate():
        if t is threading.current_thread() or t.daemon:
            continue
        t.join(2.0)
        print(f"  JOIN {t.name}: alive_after_2s={t.is_alive()}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6, help="请求总数（真实模式默认 6，省钱）")
    ap.add_argument("--conc", type=int, default=1, help="并发数")
    ap.add_argument("--mock", action="store_true", help="只压缓存+限流闸门，不打模型（零成本）")
    ap.add_argument("--user", default="stress-user",
                    help="限流维度：所有请求共用这个身份。🔴 别用 session_id，每次新会话 = 全新额度")
    ap.add_argument("--threads", action="store_true",
                    help="只拉起依赖、列出进程内线程（零成本，排查退出噪音）")
    args = ap.parse_args()

    if args.threads:
        await show_threads()
        return 0

    sem = asyncio.Semaphore(args.conc)
    out = []
    t_wall = time.perf_counter()
    if args.mock:
        await asyncio.gather(*[one_mock(i, sem, out) for i in range(args.n)])
    else:
        await asyncio.gather(*[one_real(i, sem, out, args.user) for i in range(args.n)])
    wall_ms = (time.perf_counter() - t_wall) * 1000

    ok = [r for r in out if r["ok"]]
    bad = [r for r in out if not r["ok"]]
    limited = [r for r in ok if (r["meta"] or {}).get("rate_limited")]
    # 🔴 分位数只统计【被真正服务】的请求：被限流秒拒的是另一种分布，
    #    混进来会把 p50 洗白（2026-10-19 实测：混算 p50=45ms，真正被服务的 p50≈3s）。
    served = [r for r in ok if not (r["meta"] or {}).get("rate_limited")]
    ms = [r["ms"] for r in served]
    hit_mem = [r for r in served if (r["meta"] or {}).get("cache_where") == "memory"]
    hit_redis = [r for r in served if (r["meta"] or {}).get("cache_where") == "redis"]

    print("===== 压测结果 =====", flush=True)
    print(f"模式         {'mock(闸门开销)' if args.mock else 'real(端到端)'}")
    print(f"请求 / 并发   {args.n} / {args.conc}")
    print(f"被服务/失败/被拦  {len(served)} / {len(bad)} / {len(limited)}")
    print(f"p50 / p95 / p99   {pct(ms, .50):.0f} / {pct(ms, .95):.0f} / {pct(ms, .99):.0f} ms  ← 只算被服务的")
    print(f"总墙钟时间    {wall_ms:.0f} ms（含进程启动/依赖导入；对比时两轮都要扣掉它）")
    print(f"限流拦截      {len(limited)}")
    print(f"缓存命中      memory={len(hit_mem)} redis={len(hit_redis)}")
    for r in bad[:5]:
        print(f"  失败样本 #{r['i']}: {r.get('err')}")
    print("提示：与上一轮对比时，并发数 / 缓存开关 / 限流开关必须完全相同 —— 不同分母的 delta 没有意义。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))