"""诊断：deepseek 端点支持哪种结构化输出方式（3 次调用，很便宜）。"""
import os
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY_ENABLED", "False")

import asyncio, sys
import agent
from langchain_core.messages import SystemMessage, HumanMessage

MSGS = [
    SystemMessage('只输出一个 JSON 对象，不要任何解释。字段：answer(str) / sources(list[str]) / '
                  'enough_info(bool) / confidence(float 0~1)'),
    HumanMessage("奇变偶不变是什么口诀？"),
]


def _force_utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


async def main():
    for m in ("function_calling", "json_mode", "json_schema"):
        try:
            llm = agent.lc_llm.with_structured_output(agent.NoteAnswer, include_raw=True, method=m)
            out = await llm.ainvoke(MSGS)
            err = out.get("parsing_error")
            print(f"{'✅' if out.get('parsed') else '⚠️'} method={m} → parsed={out.get('parsed')} "
                  f"｜parsing_error={type(err).__name__ if err else None}")
        except Exception as e:
            print(f"❌ method={m} → {type(e).__name__}: {str(e)[:180]}")


if __name__ == "__main__":
    _force_utf8()
    asyncio.run(main())