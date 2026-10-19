# ===== probe_redis.py — 探针：Redis 连通 / 两级缓存 / 挂了会怎样 =====
import os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    # 🔴 不设这一行，中文/符号会按控制台的 GBK 编码 → UnicodeEncodeError（实测崩过）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry
import cache as C


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
os._exit(0)