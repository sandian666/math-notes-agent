# -*- coding: utf-8 -*-
"""
obs.py —— 结构化日志（一步一行 JSON）

【为什么要它】
    print       → 给人看的，屏幕一滚就没了，出事只能靠回忆
    run.jsonl   → 给程序看的，一行一个 JSON：能统计、能排序、能两轮对比

【设计上故意的两点】
    1. log_event() 把异常全吞掉 —— 日志写失败绝不许影响一次正常问答（日志是配角）。
    2. 本文件不 import agent —— 谁都能 import 它，不会绕成循环导入。

【字段一览】（一条 kind="answer" 的记录长这样）
    ts           时间戳
    kind         "answer" / "eval_turn" / ...
    session_id   哪个会话
    question     问了什么
    text         答了什么
    cache_hit    是不是缓存命中（命中=没花钱）
    usage        {"input":..,"output":..,"total":..}  token 用量
    tools        ["search_notes", ...]  本轮调了哪些工具
    n_tool_calls 工具调用次数
    retries      本轮的重试/超时记录
    timeout      整轮是不是被 RUN_TIMEOUT 砍了
    interrupt    是不是停在了人工确认点
    latency_ms   这一轮耗时（毫秒）
"""
import json
import os
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
RUN_LOG = os.path.join(LOG_DIR, "run.jsonl")


def log_event(**fields):
    """追加写一行 JSON。返回写进去的那条记录；写失败返回 None（绝不抛）。"""
    rec = {"ts": datetime.now().isoformat(timespec="seconds")}
    rec.update(fields)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(RUN_LOG, "a", encoding="utf-8") as f:      # "a" = 追加，不是覆盖
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec
    except Exception as e:
        print(f"[obs] 写日志失败（不影响问答）：{type(e).__name__}: {e}")
        return None


def read_events(path=None):
    """把 jsonl 读回成列表。坏行直接跳过 —— 一行脏数据不许毁掉整次统计。"""
    path = path or RUN_LOG
    if not os.path.exists(path):
        return []
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue          # 比如写了一半就被 Ctrl+C 砍断的那一行
    return events


def _pct(sorted_nums, q):
    """自己算分位数（不引 numpy）：sorted_nums 必须已经排好序。"""
    if not sorted_nums:
        return 0
    idx = min(len(sorted_nums) - 1, int(round(q * (len(sorted_nums) - 1))))
    return sorted_nums[idx]


def summarize(events, kind="answer"):
    """把一堆事件汇总成报告要的那几个数：条数 / 延迟 / token / 工具分布 / 重试。"""
    evs = [e for e in events if e.get("kind") == kind]
    lat = sorted(int(e.get("latency_ms") or 0) for e in evs)
    tokens = [int((e.get("usage") or {}).get("total") or 0) for e in evs]
    tool_count = {}
    for e in evs:
        for t in (e.get("tools") or []):
            tool_count[t] = tool_count.get(t, 0) + 1
    n = len(evs) or 1                      # 防止除以 0
    return {
        "kind": kind,
        "n": len(evs),
        "cache_hits": sum(1 for e in evs if e.get("cache_hit")),
        "avg_latency_ms": round(sum(lat) / n),
        "p95_latency_ms": _pct(lat, 0.95),
        "max_latency_ms": lat[-1] if lat else 0,
        "avg_total_tokens": round(sum(tokens) / n),
        "sum_total_tokens": sum(tokens),
        "tool_calls": dict(sorted(tool_count.items(), key=lambda kv: -kv[1])),
        "retries": sum(len(e.get("retries") or []) for e in evs),
        "timeouts": sum(1 for e in evs if e.get("timeout")),
        "interrupts": sum(1 for e in evs if e.get("interrupt")),
    }


def print_summary(stats):
    """把 summarize() 的结果打印成人能看的一坨。"""
    print(f"--- 日志汇总（kind={stats['kind']}，共 {stats['n']} 条）---")
    print(f"  缓存命中      {stats['cache_hits']}")
    print(f"  平均延迟      {stats['avg_latency_ms']} ms")
    print(f"  p95 延迟      {stats['p95_latency_ms']} ms   （最大 {stats['max_latency_ms']} ms）")
    print(f"  平均 token    {stats['avg_total_tokens']}（合计 {stats['sum_total_tokens']}）")
    print(f"  重试 / 超时   {stats['retries']} / {stats['timeouts']}")
    print(f"  工具调用分布  {stats['tool_calls']}")