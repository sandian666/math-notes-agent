# ===== S5 / main.py =====
from fastapi import FastAPI
from agent import answer_cached,stream_answer
from fastapi.responses import HTMLResponse,StreamingResponse
import uvicorn,os,base64,asyncio,time

S5_USER = os.environ.get("S5_USER", "demo") #用户名；没配就默认 demo
S5_PASSWORD = os.environ.get("S5_PASSWORD")  # 口令不给默认值（拿不到就是 None）

# 引导页 + cookie
COOKIE_NAME = "s5_ok"        # 口令通过后发给浏览器的"小凭证"的名字

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>数学笔记助手</title>
<style>
  :root{
    --bg:#f4f6fb; --glow-1:rgba(99,102,241,.22); --glow-2:rgba(14,165,233,.20);
    --card:rgba(255,255,255,.88); --line:rgba(15,23,42,.10);
    --text:#0f172a; --muted:#7c8aa5; --accent:#4f46e5; --accent-2:#0ea5e9;
    --accent-soft:rgba(79,70,229,.10); --err:#ef4444;
    --shadow:0 30px 70px -30px rgba(15,23,42,.42);
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#080c18; --glow-1:rgba(99,102,241,.32); --glow-2:rgba(34,211,238,.24);
      --card:rgba(19,26,46,.82); --line:rgba(255,255,255,.11);
      --text:#e8eeff; --muted:#8394b8; --accent:#6366f1; --accent-2:#22d3ee;
      --accent-soft:rgba(99,102,241,.18);
      --shadow:0 34px 80px -34px rgba(0,0,0,.9);
    }
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{
    margin:0; display:grid; place-items:center; padding:24px; position:relative; overflow:hidden;
    font-family:var(--sans); color:var(--text); background:var(--bg);
    -webkit-font-smoothing:antialiased;
  }
  body::before,body::after{
    content:""; position:fixed; width:56vw; height:56vw; border-radius:50%;
    filter:blur(92px); pointer-events:none;
  }
  body::before{background:var(--glow-1); top:-20vw; left:-12vw}
  body::after{background:var(--glow-2); bottom:-26vw; right:-14vw}
  .card{
    position:relative; width:100%; max-width:396px; padding:34px 30px 26px; text-align:center;
    background:var(--card); border:1px solid var(--line); border-radius:24px;
    box-shadow:var(--shadow); backdrop-filter:blur(20px);
    animation:rise .34s ease both;
  }
  @keyframes rise{from{opacity:0; transform:translateY(14px)}to{opacity:1; transform:none}}
  .logo{
    width:52px; height:52px; margin:0 auto 16px; border-radius:17px; display:grid; place-items:center;
    background:linear-gradient(135deg,var(--accent),var(--accent-2)); color:#fff;
    font-weight:800; font-size:20px; letter-spacing:.5px;
  }
  h1{margin:0 0 8px; font-size:19px; letter-spacing:-.2px}
  .sub{margin:0 0 22px; font-size:13px; line-height:1.7; color:var(--muted)}
  form{display:flex; flex-direction:column; gap:11px}
  input{
    font:inherit; font-size:15px; width:100%; padding:13px 15px; color:var(--text);
    background:var(--accent-soft); border:1px solid var(--line); border-radius:14px;
    outline:none; transition:.18s; text-align:center; letter-spacing:2px;
  }
  input::placeholder{color:var(--muted); letter-spacing:normal}
  input:focus{border-color:var(--accent); box-shadow:0 0 0 4px var(--accent-soft)}
  button{
    font:inherit; font-size:15px; font-weight:600; cursor:pointer; padding:13px 18px; color:#fff;
    border:none; border-radius:14px; background:linear-gradient(135deg,var(--accent),var(--accent-2));
    transition:.18s; box-shadow:0 12px 26px -14px var(--accent);
  }
  button:hover{filter:brightness(1.09)}
  button:active{transform:translateY(1px)}
  .err{margin:0 0 4px; font-size:12.5px; color:var(--err); min-height:0}
  .err:empty{display:none}
  .foot{margin:20px 0 0; padding-top:16px; border-top:1px solid var(--line); font-size:11.5px; line-height:1.8; color:var(--muted)}
</style>
</head>
<body>
  <div class="card">
    <div class="logo">∑</div>
    <h1>数学笔记助手</h1>
    <p class="sub">请输入访问口令</p>
    <form method="get" action="/">
      <p class="err">__ERR__</p>
      <input type="password" name="k" placeholder="访问口令" autofocus autocomplete="current-password">
      <button type="submit">进入</button>
    </form>
    <p class="foot">如无法进入，请向分享者索取口令。</p>
  </div>
</body>
</html>
"""


def login_page(bad: bool = False) -> str:
    """口令页：口令填错了就在框上方给一句提示，而不是默默重画一遍。"""
    return LOGIN_PAGE.replace("__ERR__", "口令不对，再试一次" if bad else "")

from contextlib import asynccontextmanager

import agent
import mcp_bridge
import cache            # 缓存层（Redis 是可选依赖，连不上会自己降级成内存缓存）
import quota_db         # 额度账本（MySQL，硬依赖）


async def _startup_selfcheck():
    """启动自检：依赖能不能用，在【启动那一刻】就说清楚，而不是等用户提问才炸。

    🔴 为什么需要它：MySQL 连不上时接口本身不崩，异常只在「用户提问」的那一刻抛出 ——
       排查时你看到的现象只是"页面没反应"，真正的原因要翻控制台才知道。把检查提到启动阶段 = fail fast。
    ⚠️ 自检只【报告】、不拦启动：依赖挂了服务照样起来，用户在页面上会收到一句人话错误。
    """
    # ① MySQL：额度账本（硬依赖 —— 连不上就答不了题）
    # 🔴【等待就绪】：开机后 Docker Desktop 要 30~60 秒才把容器带起来。
    #    如果他比 Docker 先跑 main.py，这里不该立刻判死刑，而要等一等 ——
    #    这正是"只运行 main.py 就能用"的关键：启动顺序不再重要。
    wait_s = int(os.environ.get("S5_WAIT_DB", "60"))      # 可用环境变量调；设 0 = 不等，立刻报
    deadline = time.time() + wait_s
    attempt = 0
    while True:
        attempt += 1
        try:
            used = await asyncio.wait_for(quota_db.used_today(), timeout=5)   # 单次 5 秒兜底
            waited = f"（第 {attempt} 次才通）" if attempt > 1 else ""
            print(f"✅ [自检] MySQL 通了：{quota_db.DB['host']}:{quota_db.DB['port']}/{quota_db.DB['db']}"
                  f" · 今日额度 {used}/{quota_db.DAILY_LIMIT}{waited}")
            break
        except UnicodeEncodeError as e:
            # 永久性错误：密码里有非 ASCII 字符 → 等多久都没用，立刻停下来说清楚（不自欺地空等）
            print(f"❌ [自检] 密码不合法：{type(e).__name__}: {e}")
            print("          → MySQL 驱动握手用 latin-1 编码密码，中文/全角字符发不出去：换成纯 ASCII 密码")
            break
        except Exception as e:
            if time.time() >= deadline:
                print(f"❌ [自检] 等了 {wait_s} 秒仍连不上 MySQL：{type(e).__name__}: {e}")
                print("          → 查容器：docker ps（没有就 docker start s5-mysql）")
                print("          → 查地址/密码：S6_DB_HOST / S6_DB_PASSWORD（密码必须纯 ASCII）")
                break
            if attempt == 1:      # 只提示一次，别刷屏
                print(f"⏳ [自检] MySQL 还没就绪（{type(e).__name__}）—— 最多等 {wait_s} 秒，"
                      f"Docker Desktop 开机后通常需要 30~60 秒")
            await asyncio.sleep(2)

    # ② Redis：缓存（软依赖 —— 连不上会自动降级成内存缓存，不影响出答案）
    try:
        cache.default().get("__selfcheck__")        # 触发一次探测；get 内部保证不抛异常
        mode = cache.default().status()["mode"]
        if mode == "redis":
            print(f"✅ [自检] Redis 通了：{cache.default().url}")
        else:
            print(f"⚠️ [自检] 缓存降级为 {mode}（Redis 没连上，服务照常可用）")
    except Exception as e:
        print(f"⚠️ [自检] 缓存层异常：{type(e).__name__}: {e}")


@asynccontextmanager
async def app_lifespan(app):
    await _startup_selfcheck()              # 启动先把依赖状态打出来（fail fast）
    await agent.warmup()                    # 🔴 批④ 收尾：预建图 + 预握手缓存/限流，
                                            #    别让"第一个用户"背 6 秒冷启动税（并发来还会变成 N 份）
    tools = await mcp_bridge.startup()      # 启动：连 MCP，拿工具
    agent.add_mcp_tools(tools)              # 并进图里那张工具表
    yield  # lifespan() 暂停，并把控制权交还给 FastAPI，当停止 FastAPI 服务时，程序会继续执行 yield 后面的代码
    await mcp_bridge.shutdown()             # 关闭：收尾

app = FastAPI(lifespan=app_lifespan)   # 创建一个 FastAPI 应用，并告诉它使用我定义的 lifespan 函数来管理启动和关闭过程。


@app.middleware("http")  #注册中间件 = "每个 HTTP 请求先过这一关"
async def require_password(request, call_next):
    # ① 之前输对过 → 浏览器带着 cookie 来了 → 直接放行
    if S5_PASSWORD and request.cookies.get(COOKIE_NAME) == S5_PASSWORD:
        return await call_next(request)

    # ② 网址里带了口令（?k=口令：点开"口令进链接"或用引导页提交）
    #    → 放行，并顺手发一个 cookie，让页面里的 fetch 之后也认得出他
    if S5_PASSWORD and request.query_params.get("k") == S5_PASSWORD:
        response = await call_next(request)
        response.set_cookie(COOKIE_NAME, S5_PASSWORD, httponly=True)
        return response

    # ③ 老路：Basic 认证头（浏览器弹框那条）—— 保留兼容，简历口径不废
    expected = "Basic " + base64.b64encode(f"{S5_USER}:{S5_PASSWORD}".encode()).decode()
    if S5_PASSWORD and request.headers.get("authorization") == expected:
        return await call_next(request)

    # ④ 都没有 → 返回"只有口令框"的引导页（不再返回 401 弹框）
    #    带了 k 还是走到这里 = 口令填错了 → 页面上给一句提示
    return HTMLResponse(content=login_page(request.query_params.get("k") is not None),
                        status_code=401)


@app.get("/hello")    #登记一条路由：告诉 app 这张表 —— "网址 /hello 归下面那个函数管"
async def hello():    # ← 定义当一个请求打到 /hello 时，要执行哪个函数体
    return "你好，这是 S5 的第一个接口。"

@app.get("/ask")  #函数签名：接收两个参数，名字叫question和session_id,类型是 str
async def ask(question: str,session_id:str):
    resp = await answer_cached(session_id,question)
    return {"answer": resp}

@app.get("/ask_stream")
async def ask_stream(question:str,session_id:str):
    print(f"📥 /ask_stream 收到请求：question={question!r} session={session_id!r}")
    return StreamingResponse(stream_answer(session_id,question),
      media_type="text/event-stream",)   # 告诉浏览器“我在推SSE”


@app.get("/",response_class=HTMLResponse)
async def index():
    current_dir=os.path.dirname(os.path.abspath(__file__))  #得到本文件夹路径去
    html_path=os.path.join(current_dir,"index.html")   #把本文件夹路径和网页路径拼在一起
    with open(html_path,"r", encoding="utf-8" ) as f:
        return f.read()


#   缩进 4 空格，里面调用 uvicorn.run(...)
if __name__ == "__main__":
    # 🔴 热重载（reload）是【开发用】的：它会在后台多起一个 WatchFiles 监视进程，
    #    盯着代码文件，一改动就重启。线上 / 容器里不该带（多一个进程 + 无故重启 = 风险面）。
    #    所以默认【关】；本地改代码想自动重启时：设 S5_RELOAD=1 再跑 python main.py
    _reload = os.environ.get("S5_RELOAD") == "1"
    _reload_dirs = [os.path.dirname(os.path.abspath(__file__))] if _reload else None

    # 🔴 别点 uvicorn 自己打印的那个 http://0.0.0.0:8000 —— 0.0.0.0 是"绑到本机所有网卡"的
    #    绑定地址，不是能访问的地址，浏览器点开必然"无法访问此页面"。
    #    host 保持 0.0.0.0 不动（改成 127.0.0.1 手机和 Docker 容器就都访问不到了），
    #    这里额外打印一行"点了就能进"的本机地址给 PyCharm 控制台。
    print("=" * 52)
    print("  本机请打开这个：  http://127.0.0.1:8000/")
    print("  （手机 / 局域网：http://<你电脑的IP>:8000/ ，用 ipconfig 里的 192.168.x.x）")
    print("  上面那行 Uvicorn running on 0.0.0.0 是绑定日志，点不开，忽略它")
    print("=" * 52)

    uvicorn.run("main:app", host="0.0.0.0", port=8000,
                reload=_reload, reload_dirs=_reload_dirs)


