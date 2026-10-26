# ===== S5 / s5_probes.py =====
"""S5 三条"改完代码验一下"的探针，合成一个入口。
（原来是 probe_retry.py / probe_struct.py / probe_redis.py 三个根目录文件）

用法（在 S5 目录，用项目 venv 的 python）：
    python code/s5_probes.py retry     # 工具重试策略：假 tool_calls，不花模型钱
    python code/s5_probes.py struct    # 结构化输出：先假模型演重试，再真模型跑一次（花 1 次钱）
    python code/s5_probes.py redis     # 两级缓存：连通 / 降级 / 键里必须含历史指纹

为什么合在一个文件：三条都是"验证用"的一次性动作，加起来 300 行，
散在根目录会让人误以为是服务的一部分（它们不参与启动）。合成一个入口 + 子命令，
服务代码和验证代码就分得开了。
"""
import os
import sys
import time

# 🔴 必须在 import agent 之前设：关掉 chromadb 遥测（否则每次运行都往外发包）
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY_ENABLED", "False")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    # 不设这一行，中文/符号会按控制台的 GBK 编码 → UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ============================================================
# 一、retry：工具重试策略（幂等才重试 / 超时不重试 / 写操作不重试）
# ============================================================
def _fake_state(name, args):
    """造一个"模型刚要求调用某工具"的假状态。"""
    from langchain_core.messages import AIMessage
    msg = AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "call-1", "type": "tool_call"}])
    return {"messages": [msg]}


def _make_flaky(fail_times, delay=0.0, result="OK"):
    """假工具：前 fail_times 次抛异常，之后成功。"""
    from langchain_core.tools import tool as lc_tool

    calls = {"n": 0}

    def impl() -> str:
        """测试用假工具。"""
        calls["n"] += 1
        if delay:
            time.sleep(delay)          # 同步函数 → ainvoke 丢线程池跑 → wait_for 照样能超时
        if calls["n"] <= fail_times:
            raise ConnectionError(f"模拟网络抖动(第{calls['n']}次)")
        return result

    return lc_tool(impl), calls


def _make_flaky_write():
    from langchain_core.tools import tool as lc_tool

    calls = {"n": 0}

    def impl(note: str) -> str:
        """测试用假写操作工具（不在幂等白名单里）。"""
        calls["n"] += 1
        raise ConnectionError("模拟写库失败")

    return lc_tool(impl), calls


async def case_retry():
    import agent

    # ① 幂等工具 + 异常 → 应重试到成功（期望：OK，调用 3 次）
    agent.LC_TOOL_MAP["get_time"], calls = _make_flaky(2)
    r = await agent.lc_run_tools(_fake_state("get_time", {}))
    print(f"① 幂等·异常 → {r['messages'][0].content} ｜调用次数={calls['n']}（期望 3）")

    # ② 幂等工具 + 超时 → 不重试（期望：工具超时，调用 1 次）
    agent.LC_TOOL_MAP["get_time"], calls = _make_flaky(9, delay=0.5)
    old = agent.TOOL_TIMEOUT
    agent.TOOL_TIMEOUT = 0.1
    r = await agent.lc_run_tools(_fake_state("get_time", {}))
    agent.TOOL_TIMEOUT = old
    print(f"② 幂等·超时 → {r['messages'][0].content[:28]} ｜调用次数={calls['n']}（期望 1）")

    # ③ 非幂等工具 + 异常 → 不重试（期望：工具出错，调用 1 次）
    agent.LC_TOOL_MAP["risky_write"], calls = _make_flaky_write()
    r = await agent.lc_run_tools(_fake_state("risky_write", {"note": "x"}))
    print(f"③ 非幂等·异常 → {r['messages'][0].content[:34]} ｜调用次数={calls['n']}（期望 1）")


# ============================================================
# 二、struct：结构化输出（Pydantic 校验 + 不合格就把报错喂回去重试）
# ============================================================
def _bad_validation_error():
    """故意用坏数据触发一次真的 ValidationError，当"模型返回不合格"的证据。"""
    import agent
    from pydantic import ValidationError

    bad = {"answer": 123, "sources": "不是列表", "enough_info": "不是布尔", "confidence": 5}
    try:
        agent.NoteAnswer.model_validate(bad)
    except ValidationError as e:
        return e
    return None


async def case_struct():
    import agent
    from langchain_core.messages import AIMessage

    # ① 假模型：前两次不合格、第三次合格 → 验重试循环（不花一分钱）
    err = _bad_validation_error()
    print("① 假模型的报错长这样 →", str(err).splitlines()[0])
    calls = {"n": 0}

    async def fake(msgs):
        calls["n"] += 1
        if calls["n"] < 3:
            return {"raw": AIMessage(content="我随便说说"), "parsed": None, "parsing_error": err}
        return {"raw": AIMessage(content="{}"),
                "parsed": agent.NoteAnswer(answer="合格答案", sources=["三角函数"],
                                           enough_info=True, confidence=0.9),
                "parsing_error": None}

    real = agent._invoke_structured          # 先存真身
    agent._invoke_structured = fake          # 换上假的
    r = await agent.structured_answer("奇变偶不变是什么口诀？")
    agent._invoke_structured = real          # 换回来
    print(f"① 假失败 → {r} ｜调用次数={calls['n']}（期望 3）")

    # ② 真模型：正常一次就该合格
    r = await agent.structured_answer("奇变偶不变是什么口诀？")
    print(f"② 真模型 → answer={(r.answer[:40] + '…') if r else None!r} ｜sources={r.sources if r else None} "
          f"｜enough_info={r.enough_info if r else None} ｜confidence={r.confidence if r else None}")


# ============================================================
# 三、redis：两级缓存（连通 / 降级 / 键含历史指纹 / 内存淘汰）
# ============================================================
def case_redis():
    import cache as C
    import redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    def line(tag, msg=""):
        print(f"[{tag}] {msg}", flush=True)

    def try_conn(label, **kw):
        t0 = time.perf_counter()
        try:
            r = redis.Redis(**kw)
            pong = r.ping()
            r.set("s8:probe", "hello", ex=60)
            got = r.get("s8:probe")
        except Exception as e:
            ms = int((time.perf_counter() - t0) * 1000)
            line(label, f"FAILED after {ms}ms -> {type(e).__name__}: {e}")
        else:
            ms = int((time.perf_counter() - t0) * 1000)
            line(label, f"ping={pong} set/get={got!r} ok in {ms}ms")

    line("1-RAW", "== 直连 Redis：同一个'连不上'，差 3 个数量级 ==")
    try_conn("GOOD", host="127.0.0.1", port=6379, socket_timeout=0.5)
    # 实验：把 6379 改成 6380，或把下面两行的 retry 参数删掉，看耗时怎么变
    try_conn("DOWN-默认重试", host="127.0.0.1", port=6399, socket_timeout=0.3, socket_connect_timeout=0.3)
    try_conn("DOWN-关重试", host="127.0.0.1", port=6399, socket_timeout=0.3, socket_connect_timeout=0.3,
             retry=Retry(NoBackoff(), 0))
    # 黑洞 IP：不拒绝也不回应 → 只有超时参数能救你（"降级不死"的命门）
    try_conn("BLACKHOLE-关重试", host="10.255.255.1", port=6379, socket_timeout=0.3,
             socket_connect_timeout=0.3, retry=Retry(NoBackoff(), 0))

    line("2-CACHE", "== 走 cache.py：两级缓存 ==")
    c = C.default()
    k = C.make_key("罗尔定理是什么", "probe-a", 0)
    line("2-CACHE", f"key = {k}")
    c.set(k, "ANSWER-1")
    v1, w1 = c.get(k)
    line("2-CACHE", f"第一遍 get       -> where={w1} value={v1!r}")
    c._l1.clear()          # 清掉内存层 = 模拟"另一个进程 / 刚重启"
    v2, w2 = c.get(k)
    line("2-CACHE", f"清掉内存层后 get -> where={w2} value={v2!r}   ← 期望 redis")
    line("2-CACHE", f"status = {c.status()}")

    line("3-HISTORY", "== 键必须含历史指纹：同问、不同轮次 = 不同键 ==")
    line("3-HISTORY", f"n_history=0 -> {C.make_key('罗尔定理是什么', 'probe-a', 0)}")
    line("3-HISTORY", f"n_history=3 -> {C.make_key('罗尔定理是什么', 'probe-a', 3)}")

    line("4-DEGRADE", "== 模拟 Redis 挂了：服务不许死 ==")
    bad = C.TwoLevelCache(url="redis://127.0.0.1:6399/0")   # 实验：改成 10.255.255.1 会怎样？
    t0 = time.perf_counter()
    bad.set("kk", "vv")                                     # 不许抛异常
    v3, w3 = bad.get("kk")
    ms = int((time.perf_counter() - t0) * 1000)
    line("4-DEGRADE", f"set/get 都活着 -> where={w3} value={v3!r} mode={bad.mode} 用时 {ms}ms")
    line("4-DEGRADE", "降级是【行为】不是口号：不抛异常、不干等几十秒、答得慢但答得出")

    line("5-EVICT", f"== 内存层不是无底洞：L1_MAX={C.L1_MAX}，满了淘汰最久未用的 ==")
    sys.stdout.flush()
    os._exit(0)            # 🔴 硬退出：redis 连接池有后台线程，正常退出会卡住提示符


def main():
    case = sys.argv[1] if len(sys.argv) > 1 else ""
    if case == "retry":
        import asyncio
        asyncio.run(case_retry())
    elif case == "struct":
        import asyncio
        asyncio.run(case_struct())
    elif case == "redis":
        case_redis()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
