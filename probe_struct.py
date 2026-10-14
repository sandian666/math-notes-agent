"""验收探针：① 假模型验重试② 真模型跑一次。"""
import os
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY_ENABLED", "False")

import asyncio, sys
import agent
from langchain_core.messages import AIMessage
from pydantic import ValidationError


def _force_utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def make_bad_error():
    """故意用坏数据触发一次真的 ValidationError，当'模型返回不合格'的证据"""
    bad = {"answer": 123, "sources": "不是列表", "enough_info": "不是布尔", "confidence": 5}
    try:
        agent.NoteAnswer.model_validate(bad)
    except ValidationError as e:
        return e
    return None


async def main():
    # ① 假模型：前两次不合格、第三次合格 → 验重试循环（不花一分钱）
    err = make_bad_error()
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


if __name__ == "__main__":
    _force_utf8()
    asyncio.run(main())