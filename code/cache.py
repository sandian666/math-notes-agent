# ===== S8 / cache.py =====
"""两级缓存（内存 → Redis）+ Redis 挂了自动降级。

🔴 三条原则（面试考点）：
 1) 缓存键必须由【所有影响答案的输入】派生：提示词/模型版本 + 问题(归一化) + 历史指纹。
    漏一个字段 → 脏读（旧答案冒充新答案 / 串会话）。
 2) 有副作用（写类工具）或停在人工确认的轮次，绝不写缓存（那等于"下次不再真的执行"）。
 3) Redis 是加速器，不是依赖：所有调用 try/except + 硬超时；连不上就退化成纯内存，服务照答。
"""
import hashlib, os, threading, time, unicodedata
from collections import OrderedDict

try:
    import redis as _redis          # 第三方库：pip install redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry
except ImportError:
    _redis = None

# ---------------- 可调参数（实验点都在这） ----------------
PROMPT_VERSION  = "p4-v1"   # 🔴 缓存版本号：改过提示词 / 模型 / 工具列表 → 必须修改，否则旧答案会冒充新答案
MODEL_TAG       = os.getenv("S8_MODEL", "deepseek") # 模型标签
REDIS_URL       = os.getenv("S8_REDIS_URL", "redis://127.0.0.1:6379/0")  # Redis地址
TTL_SECONDS     = 3600      # 实验：改成 5 → Redis 那层 5 秒后就过期
L1_MAX          = 256       # 内存层最多存几条（防内存泄漏）
SOCKET_TIMEOUT  = 0.3       # 🔴 降级不死的命门：Redis 是黑洞时，最坏只等 0.3 秒
RECONNECT_AFTER_SECONDS = 5.0  # 降级后每 5 秒试着重连一次（自愈）—— 按【时间】比按【次数】更贴合"依赖随时可能回来"


def enabled():
    """🔴 结果缓存开关：安全默认 = 关（读环境变量 S8_RESULT_CACHE，每次现读，避免"导入时快照"的坑）。

    为什么默认关：结果缓存命中的时候，跳过的不只是模型调用，还有【工具执行】和【checkpoint 状态写入】。
      · 足迹类判据（调了哪个工具 / 重试几次）→ 命中即判失败（2026-10-19 实测：tool-08/09）
      · 多轮上下文 → 命中即断（实测：mt-05 答"没看到您上一条提到的数字"）
    什么时候开：过程无人关心的通道 —— 纯问答 / 演示 / 并发压测。
    什么时候必须关：评测、回归、审计、计费、多轮会话。
    """
    return os.getenv("S8_RESULT_CACHE", "0") == "1"


def _say(msg):
    """观测代码不许成为故障源：打印失败（编码 / 管道已关）绝不许把业务搞崩。"""
    try:
        print(msg, flush=True)
    except Exception:
        pass


def normalize(text):
    """文字归一化先行：NFKC（全角→半角、统一符号）+ 压空白 + 小写。"""
    t = unicodedata.normalize("NFKC", text or "")
    return " ".join(t.split()).lower()


def make_key(question, session_id, n_history, extra=""):
    """🔴 键 = 版本 + 模型 + 历史指纹 + 问题(归一化) + extra。
    历史指纹变了 → 键就变 → 不会拿"干净会话"的答案冒充"聊了三轮"的答案。"""
    scope = "fresh" if n_history == 0 else f"thread:{session_id}@{n_history}"
    raw = "|".join([PROMPT_VERSION, MODEL_TAG, scope, normalize(question), extra])
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    #对原始字符串计算 SHA-1 哈希，哈希就是把一段可能很长的输入转换成固定格式的短指纹；这里用它来生成短、稳定、便于 Redis 存取的缓存键
    return f"{PROMPT_VERSION}|{scope}|{digest}"       # 可读前缀，方便在 Redis 里查


class TwoLevelCache:
    """第一级 = 进程内 dict（快，重启即丢）；第二级 = Redis（跨进程、重启还在）。"""

    def __init__(self, url=None, prefix="s8:cache"):
        self.url = url or REDIS_URL
        self.prefix = prefix          # Redis key 前缀
        self._l1 = OrderedDict()      # OrderedDict = 顺便能实现"淘汰最久没用到的"
        self._lock = threading.Lock() # 创建线程锁。
        self._c = None                # Redis 客户端；None = 还没连 / 连不上
        self.mode = "unprobed"    # 当前缓存模式：unprobed（未连接） → redis / memory-only（只使用内存）
        self._retry_at = 0.0      # 下次允许重连的时刻（time.monotonic 的秒数，不是次数）

    # ---------------- 第二级：Redis（每个动作都兜异常） ----------------
    def _client(self):
        if self._c is not None:
            return self._c
        if _redis is None:
            self._degrade("没装 redis 库（pip install redis）")
            return None
        if time.monotonic() < self._retry_at:   # 降级期间别每次都白等 0.3 秒，隔几秒探一次
            return None
        try:
            # 🔴 光设 socket_timeout 不够：redis-py 默认还有【内部重试 + 退避】，
            #    0.3 秒会被放大成 7.8 秒（2026-10-17 实测）。必须显式关掉它的重试。
            c = _redis.Redis.from_url(self.url, socket_timeout=SOCKET_TIMEOUT,
                                      socket_connect_timeout=SOCKET_TIMEOUT,
                                      retry=Retry(NoBackoff(), 0),
                                      decode_responses=True)
            c.ping()
        except Exception as e:
            self._degrade(f"{type(e).__name__}: {e}")
            self._retry_at = time.monotonic() + RECONNECT_AFTER_SECONDS   # 隔 5 秒再试着重连
            return None
        self._c = c
        if self.mode != "redis":  # 只在以下情况打印：第一次连接成功/Redis 从故障中恢复
            _say(f"[cache] Redis 已连上（{self.url}）→ 两级缓存生效")
        self.mode = "redis"
        return self._c

    def _degrade(self, why):
        """🔴 静默的分支要出声：降级必须打印，不然"没执行"和"执行了没效果"分不清。"""
        if self.mode != "memory-only":
            _say(f"[cache] Redis 不可用（{why}）→ 降级为纯内存缓存，服务继续")
        self.mode = "memory-only"

    @property  # 这是一个装饰器，把下面的方法变成“属性”。
    # 可以像读取普通变量一样写cache.degraded 自动执行：degraded(self)
    def degraded(self):
        return self.mode != "redis"

    # ---------------- 读 / 写 ----------------
    def get(self, key):
        """返回 (value, where)，where ∈ {"memory","redis",None}。任何异常都不许抛出去。"""
        with self._lock:
            if key in self._l1:
                self._l1.move_to_end(key)
                return self._l1[key], "memory"
        c = self._client()
        if c is None:
            return None, None                    # 降级：内存层没有就是没有，不报错
        try:
            raw = c.get(self.prefix + ":" + key)
        except Exception as e:
            self._degrade(f"get 失败 {type(e).__name__}: {e}")
            self._c = None
            return None, None
        if raw is None:
            return None, None
        with self._lock:                         # 命中 Redis 就回填内存层（下次更快）
            self._l1[key] = raw
            self._l1.move_to_end(key)      # 把这个 key 移动到 OrderedDict 的末尾，表示最近刚访问过
            self._trim()         # 检查内存缓存是否超过最大数量。
        return raw, "redis"

    def set(self, key, value, ttl=TTL_SECONDS):
        """只缓存字符串。返回 True = 也写进了 Redis。"""
        with self._lock:
            self._l1[key] = value
            self._l1.move_to_end(key)
            self._trim()
        c = self._client()
        if c is None:
            return False
        try:
            c.setex(self.prefix + ":" + key, ttl, value)
            return True
        except Exception as e:
            self._degrade(f"set 失败 {type(e).__name__}: {e}")
            self._c = None
            return False

    def _trim(self):
        while len(self._l1) > L1_MAX:
            self._l1.popitem(last=False)          # 淘汰最久没被用到的

    def status(self):
        return {"mode": self.mode, "l1_size": len(self._l1), "redis_url": self.url,
                "ttl": TTL_SECONDS, "prompt_version": PROMPT_VERSION}


_DEFAULT = None


def default():
    """懒加载单例：import 时不连 Redis，第一次真正用到才连。"""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = TwoLevelCache()
    return _DEFAULT