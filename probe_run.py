"""S5 探针脚本：跑一轮问答，打印后立刻硬退出（不用按 Ctrl+C）。

用法（在 S5 目录）：
    python probe_run.py "北京今天天气怎么样"         # session_id 自动生成，绝不撞历史
    python probe_run.py "现在几点" --sid probe-x   # 指定 session_id（想复用同一会话时）
    python probe_run.py --info probe-103045        # 只看该会话历史，不产生新回答

这个脚本自带三件"环境准备"，所以命令里不需要任何 $env: 前缀：
  1) 关掉 chromadb 遥测（必须在 import agent 之前设置）
  2) stdout/stderr 强制 UTF-8（中文不乱码）
  3) 末尾 os._exit(0) 硬退出（绕开非守护后台线程，不卡提示符）

🔴 默认每次生成全新 session_id：历史里有答案时模型会直接照抄、根本不调工具，
   你会误判成"改动没生效"。
"""
import os

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")      # 🔴 必须在 import agent 之前
os.environ.setdefault("CHROMA_TELEMETRY_ENABLED", "False")

import asyncio
import datetime
import sys

import agent


def _force_utf8():
    """中文输出走 UTF-8，不受控制台代码页影响。"""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _new_sid():
    return "probe-" + datetime.datetime.now().strftime("%H%M%S")


async def main():
    argv = sys.argv[1:]
    if not argv:
        print('用法: probe_run.py "<问题>" [--sid 会话id]')
        print('      probe_run.py --info <会话id>')
        return

    if argv[0] == "--info":
        if len(argv) < 2:
            print("要给会话 id：probe_run.py --info probe-103045")
            return
        await agent.thread_info(argv[1])
    else:
        q = argv[0]
        sid = argv[argv.index("--sid") + 1] if "--sid" in argv else _new_sid()
        # --yes / --no = 非交互模式（自动回答）；都不给 = 真的停下来等你敲键盘
        fixed = "y" if "--yes" in argv else ("n" if "--no" in argv else None)
        print(f"🆔 session_id = {sid}")
        text = await agent.graph_answer(sid, q)
        print(text)

        # 🔴 while 不是 if：图可能停好几次，每次都得重新问一遍
        while text.startswith("【等待人工确认】"):
            if fixed is not None:
                answer = fixed
            else:
                try:
                    answer = input("⌨️  你的回答（y = 执行 / n = 放弃）：")
                except EOFError:  # 没有可交互键盘（被管道/重定向）→ 保守拒绝
                    print("⌨️  读不到键盘输入 → 按 n 处理")
                    answer = "n"
                if not answer.strip():
                    answer = "n"  # 直接回车 = 不执行：危险操作默认拒绝（fail-safe）
            print(f"💬 [人工] 我回答：{answer}")
            text = await agent.graph_answer(sid, resume=answer)
            print(text)

    await agent.shutdown_graph()          # 顺手验批① 收尾：连接能干净关掉
    print("✅ 探针结束")


if __name__ == "__main__":
    _force_utf8()
    try:
        asyncio.run(main())
    finally:
        sys.stdout.flush()
        os._exit(0)     # 🔴 硬退出：绕开非守护后台线程