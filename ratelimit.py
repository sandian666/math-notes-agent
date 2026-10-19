# ===== S8 / ratelimit.py =====
"""限流：Redis 固定窗口（Lua 原子）+ Redis 挂了降级成进程内窗口。

为什么必须放 Redis（不能放进程内存）：
    多进程 / 多实例各自计数 → 实际允许量 = 进程数 × 阈值 → 等于没限流（经典面试陷阱）。
为什么用 Lua 而不是 INCR + EXPIRE 两条命令：
    两条命令之间进程崩掉 → key 永不过期 → 用户被永久封禁。Lua 一次往返、原子执行。
为什么降级成"本地窗口"而不是直接放行或报错：
    限流是【成本闸门】，不是【安全边界】→ Redis 挂了宁可放宽，也不能让整个服务不可用；
    但"多进程各算各的"必须打日志，不能假装还在正确限流。
什么时候真的需要限流：对外收费的 API、有爬虫/白嫖风险、对端有 TPM/RPM 配额、单机内存有限。
什么时候别上：内部单用户工具、流量本来就远低于额度（只会多一个故障面）。
"""
import os, threading, time

try:
    import redis as _redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry
except ImportError:
    _redis = None

REDIS_URL      = os.getenv("S8_REDIS_URL", "redis://127.0.0.1:6379/0")
SOCKET_TIMEOUT = 0.3
KEY_PREFIX     = "s8:rate"
DEFAULT_LIMIT  = int(os.getenv("S8_RATE_LIMIT_N", "20"))     # 窗口内允许次数
DEFAULT_WINDOW = int(os.getenv("S8_RATE_WINDOW_S", "60"))    # 窗口秒数

# 固定窗口的原子实现：计数、首次设过期、读剩余时间，一次往返全做完
_LUA = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return {n, redis.call('TTL', KEYS[1])}
"""


def enabled():
    """🔴 安全默认 = 关（现读环境变量，避免"导入时快照"的坑）。"""
    return os.getenv("S8_RATE_LIMIT", "0") == "1"


def _say(msg):
    """观测代码不许成为故障源：打印失败绝不许把业务搞崩。"""
    try:
        print(msg, flush=True)
    except Exception:
        pass


class RateLimiter:
    def __init__(self, url=None, limit=None, window=None):
        self.url = url or REDIS_URL
        self.limit = limit or DEFAULT_LIMIT
        self.window = window or DEFAULT_WINDOW
        self._c = None
        self._retry_at = 0.0
        self.mode = "unprobed"          # unprobed → redis / local
        self._local = {}                # 降级用：key -> [count, expire_at]
        self._lock = threading.Lock()
        self.blocked = 0                # 累计拦截数（观测 / 压测用）

    # ---------------- Redis 客户端（每个动作都兜异常） ----------------
    def _client(self):
        if self._c is not None:
            return self._c
        if _redis is None:
            self._degrade("没装 redis 库")
            return None
        if time.monotonic() < self._retry_at:
            return None
        try:
            # 同样要显式关掉 redis-py 内部重试，否则 0.3 秒被放大成好几秒
            c = _redis.Redis.from_url(self.url, socket_timeout=SOCKET_TIMEOUT,
                                      socket_connect_timeout=SOCKET_TIMEOUT,
                                      retry=Retry(NoBackoff(), 0), decode_responses=True)
            c.ping()
        except Exception as e:
            self._degrade(f"{type(e).__name__}: {e}")
            self._retry_at = time.monotonic() + 5.0
            return None
        self._c = c
        if self.mode != "redis":
            _say(f"[rate] Redis 已连上（{self.url}）→ 集中式限流生效（{self.limit} 次 / {self.window}s）")
        self.mode = "redis"
        return self._c

    def _degrade(self, why):
        """静默的分支要出声：降级必须打印，否则"没执行"和"执行了没效果"分不清。"""
        if self.mode != "local":
            _say(f"[rate] Redis 不可用（{why}）→ 降级为【进程内】限流：多进程下额度会被放大，仅作兜底")
        self.mode = "local"

    def _local_check(self, key):
        now = time.time()
        with self._lock:
            n, exp = self._local.get(key, [0, now + self.window])
            if now >= exp:                      # 窗口过期 → 重新开始计数
                n, exp = 0, now + self.window
            n += 1
            self._local[key] = [n, exp]
        return n, int(exp - now)

    # ---------------- 对外接口 ----------------
    def check(self, uid):
        """返回 (allowed, remaining, retry_after, mode)。任何情况都不抛异常。"""
        bucket = int(time.time() // self.window)      # 🔴 固定窗口：同一窗口共用一个计数器
        key = f"{KEY_PREFIX}:{uid}:{bucket}"
        c = self._client()
        if c is None:
            n, retry_after = self._local_check(key)
            mode = "local"
        else:
            try:
                n, ttl = c.eval(_LUA, 1, key, self.window)
                n, retry_after = int(n), int(ttl)
                mode = "redis"
            except Exception as e:
                self._degrade(f"eval 失败 {type(e).__name__}: {e}")
                self._c = None
                n, retry_after = self._local_check(key)
                mode = "local"
        allowed = n <= self.limit
        if not allowed:
            self.blocked += 1
        return allowed, max(0, self.limit - n), max(0, retry_after), mode

    def status(self):
        return {"mode": self.mode, "limit": self.limit, "window_s": self.window,
                "blocked": self.blocked, "redis_url": self.url}


_RL = None


def default():
    """懒加载单例：第一次真正用到才连 Redis。"""
    global _RL
    if _RL is None:
        _RL = RateLimiter()
    return _RL