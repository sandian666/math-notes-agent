"""验收探针：直接喂假 tool_calls 给 lc_run_tools，考重试策略（不花模型钱）。"""
import os
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY_ENABLED", "False")

import asyncio, sys, time
import agent
from langchain_core.messages import AIMessage
from langchain_core.tools import tool as lc_tool


def _force_utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def fake_state(name, args):
    """造一个"模型刚要求调用某工具"的假状态"""
    msg = AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "call-1", "type": "tool_call"}])
    return {"messages": [msg]}


def make_flaky(fail_times, delay=0.0, result="OK"):
    """假工具：前 fail_times 次抛异常，之后成功"""
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


def make_flaky_write():
    calls = {"n": 0}

    def impl(note: str) -> str:
        """测试用假写操作工具（不在幂等白名单里）。"""
        calls["n"] += 1
        raise ConnectionError("模拟写库失败")

    return lc_tool(impl), calls


async def main():
    # ① 幂等工具 + 异常 → 应重试到成功（期望：OK，调用 3 次，两条 🔁）
    agent.LC_TOOL_MAP["get_time"], calls = make_flaky(2)
    r = await agent.lc_run_tools(fake_state("get_time", {}))
    print(f"① 幂等·异常 → {r['messages'][0].content} ｜调用次数={calls['n']}（期望 3）")

    # ② 幂等工具 + 超时 → 不重试（期望：工具超时，调用 1 次）
    agent.LC_TOOL_MAP["get_time"], calls = make_flaky(9, delay=0.5)
    old = agent.TOOL_TIMEOUT
    agent.TOOL_TIMEOUT = 0.1
    r = await agent.lc_run_tools(fake_state("get_time", {}))
    agent.TOOL_TIMEOUT = old
    print(f"② 幂等·超时 → {r['messages'][0].content[:28]} ｜调用次数={calls['n']}（期望 1）")

    # ③ 非幂等工具 + 异常 → 不重试（期望：工具出错，调用 1 次）
    agent.LC_TOOL_MAP["risky_write"], calls = make_flaky_write()
    r = await agent.lc_run_tools(fake_state("risky_write", {"note": "x"}))
    print(f"③ 非幂等·异常 → {r['messages'][0].content[:34]} ｜调用次数={calls['n']}（期望 1）")


if __name__ == "__main__":
    _force_utf8()
    asyncio.run(main())