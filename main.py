# ===== S5 / main.py =====
# 门面：负责"谁会来敲门、敲哪个门、把话递给谁"。
#流程：
# ①  别人在浏览器敲： http://127.0.0.1:8000/hello
#                     └ 找 127.0.0.1 这台机器的 8000 端口
#                                    ↓
# ②  uvicorn 正在这个端口上【听】 → 听到敲门，接下这个请求
#                                    ↓
# ③  uvicorn 把请求交给 FastAPI： "有人找 /hello"
#                                    ↓
# ④  FastAPI 查【注册表】 →  查到： /hello 归 hello 函数管
#                                    ↓
# ⑤  调用 hello() → 执行函数体 → 拿到返回值 "你好…"
#                                    ↓
# ⑥  FastAPI 把返回值转成 JSON（正文带引号）
#                                    ↓
# ⑦  uvicorn 把它作为 HTTP 响应发回去
#                                    ↓
# ⑧  浏览器显示： 你好，这是 S5 的第一个接口。

from fastapi import FastAPI    #← 从 fastapi 这个库里，取出 FastAPI 这个"类"，（库名小写 fastapi；类名大写 FastAPI，别写混）
                 #← 整个库都要用，所以直接 import 库名
from agent import answer_cached,stream_answer        #← 从我自己的 agent.py 里，把 answer 函数拿进来
from fastapi.responses import HTMLResponse,StreamingResponse
import uvicorn,os,base64

S5_USER = os.environ.get("S5_USER", "demo") #用户名；没配就默认 demo
S5_PASSWORD = os.environ.get("S5_PASSWORD")  # 口令不给默认值（拿不到就是 None）

# ===== gate v2：引导页 + cookie（2026-09-21 加）=====
COOKIE_NAME = "s5_ok"        # 口令通过后发给浏览器的"小凭证"的名字

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>演示口令</title></head>
<body style="font-family: sans-serif; max-width: 420px; margin: 80px auto; text-align: center;">
    <h2>S5 数学笔记 Agent · 演示入口</h2>
    <p>请输入演示口令</p>
    <form method="get" action="/">
        <input type="password" name="k" placeholder="演示口令" autofocus
               style="padding:8px; width: 220px; font-size: 16px;">
        <button type="submit" style="padding:8px 16px; font-size: 16px;">进入</button>
    </form>
</body>
</html>
"""

app = FastAPI()   #   造一个 app 对象。（括号不能省；名字就叫 app，后面两处都要用这个名字

# ===== 第 3 件东西：接口函数
#   注意顺序：装饰器必须在【函数定义的上一行】，紧挨着，中间不能空行、不能有别的东西
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
    return HTMLResponse(content=LOGIN_PAGE, status_code=401)


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
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True,
                reload_dirs=[os.path.dirname(os.path.abspath(__file__))])


