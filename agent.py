# ===== S5 / agent.py =====
import asyncio, os, datetime, chromadb, requests, quota_db
from openai import OpenAI
from typing import Annotated, Literal, TypedDict
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

# ================= 常量与路径（改造 A） =================
# 【照抄】把"当前文件所在的文件夹"取出来，得到本py文件所在文件夹，作为基准路径，程序以后放哪里都能跑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 拼接出配置文件、向量库目录的绝对路径，防止运行位置改变导致找不到文件
COLLECTION_FILE = os.path.join(BASE_DIR, "current_collection.txt")     # ← 以"文件自己"为基准
CHROMA_DIR      = os.path.join(BASE_DIR, "chroma_db")                  # ← 跟工作目录【无关

with open(COLLECTION_FILE,encoding="utf-8") as f:
    COLLECTION_NAME=f.read().strip()

_answer_cache = {}

# ================= client（改造 B） =================
embed_client = OpenAI(api_key=os.environ.get("ZHIPU_API_KEY"),
                      base_url="https://open.bigmodel.cn/api/paas/v4/")
chat_model = "deepseek-flash"      # 官方登记名，小写 + 短横线；你原来那个是"展示名"，不是登记名

# ================= 向量库 =================
chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
col_b = chroma_client.get_collection(name=COLLECTION_NAME)

# ================= 4 个工具函数 =================

def search_notes(question: str) -> str: # 把问题转换成向量数据，然后取第一个数据的向量
    """根据用户提出的问题检索高中数学笔记，返回最相关的片段和来源小节。用户询问高中数学知识时调用。"""
    qv = embed_client.embeddings.create(model="embedding-3", input=[question]).data[0].embedding
    result= col_b.query(query_embeddings=[qv], n_results=3)  # 问题向量与chunk向量对比相似度，取n_result个结果
    docs, meta = result["documents"][0], result["metadatas"][0]
    result = ""
    for i, (doc,source_meta) in enumerate(zip(docs,meta)):
        result += f"【片段{i + 1}】（来源：{source_meta['小节']}）\n{doc}\n\n"
    return result


def get_weather(city: str) -> str:
    """查询某个城市的当前天气。"""
    url = f"https://wttr.in/{city}?format=j1"
    try:  # json() 先执行 → 对着 500 的错误页解析 → 抛 JSONDecodeError
        # JSONDecodeError 不是 RequestException 的子类 → except requests.exceptions.RequestException 接不住
        # → 异常穿透所有 except → 程序崩
        # 移到前面 → 抛的是 HTTPError（是 RequestException 的子类）→ 接住 → 返回人话 → Agent 活着
        resp = requests.get(url, timeout=10)  # 不设超时，你的程序会永久挂起，线程/请求全被堵住
        resp.raise_for_status()  # 主动抛异常
        data = resp.json()  # resp.json() 本质上就是 json.loads() 的包装
        # —— 它内部帮你做了两件事：从响应里取出正文文本（resp.text）、猜对编码，然后交给 json.loads() 解析。
        temp = data["current_condition"][0]["temp_C"]
        humidity = data["current_condition"][0]["humidity"]
        desc = data["current_condition"][0]["weatherDesc"][0]["value"]
        return f"{city}当前天气：{desc}，气温 {temp}°C，湿度 {humidity}%。"
    except requests.exceptions.RequestException as e:
        return f"查询{city}天气失败：{e}"


def get_time() -> str:
    """获取当前的日期和时间。用户问现在几点、今天几号时调用。"""
    now = datetime.datetime.now()  # 调用datetime模块的datetime类的now方法
    return now.strftime('%Y-%m-%d %H:%M:%S')  # 直接返回字符串，不要花括号


def calculate(a: int, b: int, op: Literal["+", "-", "*", "/"]) -> str:
    """对两个整数执行四则运算。op 只能是 + - * / 之一。"""
    a = int(a)
    b = int(b)
    if op == "+":
        return f"{a} + {b} = {a + b}"
    elif op == "-":
        return f"{a} - {b} = {a - b}"
    elif op == "*":
        return f"{a} * {b} = {a * b}"
    elif op == "/":
        if b == 0:
            return "错误：除数不能为 0"
        return f"{a} / {b} = {a / b}"
    else:
        return f"不支持的运算符：{op}"

# ================= LangGraph 版 Agent 图 =================
SYSTEM_PROMPT = """你是一个严谨的老师，用户询问关于高中数学有关的知识时，
你要根据文本检索相关内容并标明出处回答，若该知识文本没有请回复没有检索到相关内容。
用户询问天气、时间或需要计算时，调用相应的工具。"""

# 把 4 个原始函数"包装成"模型能选用的工具（用 docstring 当描述、类型标注当参数表）
LC_TOOLS = [tool(search_notes), tool(get_weather), tool(get_time), tool(calculate)]
LC_TOOL_MAP = {t.name: t for t in LC_TOOLS}

lc_llm = ChatOpenAI(
    model=chat_model,
    base_url="https://api.deepseek.com/v1",
    api_key=os.environ["DEEPSEEK_API_KEY"],
)
lc_llm_with_tools = lc_llm.bind_tools(LC_TOOLS)


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
#每个节点返回的新消息，要追加到旧消息列表后面，而不是覆盖原列表。

async def lc_call_model(state: AgentState):
    print(f"🔵 [图] call_model：现有 {len(state['messages'])} 条消息")
    resp = await lc_llm_with_tools.ainvoke(state["messages"])  # 把完整消息历史交给模型，异步等待模型返回结果
    return {"messages": [resp]}  # 只返回新消息，靠 reducer 追加


async def lc_run_tools(state: AgentState):
    out = []
    for tc in state["messages"][-1].tool_calls:
        print(f"🔧 [图] 执行工具 {tc['name']}({tc['args']})")
        # 工具里有同步的 chromadb / embedding 网络请求 → 丢线程池（批② 的结论）
        result = await asyncio.to_thread(LC_TOOL_MAP[tc["name"]].invoke, tc["args"])
        out.append(ToolMessage(content=result, tool_call_id=tc["id"]))
    return {"messages": out}


def lc_should_continue(state: AgentState) -> str:
    return "tools" if state["messages"][-1].tool_calls else END


_lc_builder = StateGraph(AgentState)   # 创建构建器
_lc_builder.add_node("model", lc_call_model)
_lc_builder.add_node("tools", lc_run_tools)
_lc_builder.add_edge(START, "model")
_lc_builder.add_conditional_edges("model", lc_should_continue, {"tools": "tools", END: END})
_lc_builder.add_edge("tools", "model")             # 🔴 环
agent_graph = _lc_builder.compile()    # 生成可执行图

_sessions_lc = {}        # 图版会话历史（存 LangChain 消息对象）


async def graph_answer(session_id, question):
    """跟老版 answer() 同样的活，但走图"""
    history = _sessions_lc.get(session_id, [])
    msgs = [SystemMessage(SYSTEM_PROMPT), *history, HumanMessage(question)] #  *history 的作用是：把历史消息逐条插入到当前消息列表中。
    out = await agent_graph.ainvoke({"messages": msgs})
    _sessions_lc[session_id] = out["messages"][1:]      # 去掉 system，跟老版一致，下一次调用时，函数会重新添加最新的系统提示词
    return out["messages"][-1].content   # 返回模型最终生成的回答

#带缓存的问答：同一个问题问过就不再花钱重算.
async def answer_cached(session_id, question):
    cache_key = (session_id, question)
    if cache_key in _answer_cache:                        # ① 答过的题白送、不记账
        return _answer_cache[cache_key]
    if await quota_db.used_today() >= quota_db.DAILY_LIMIT:     # ② 查账本：满了就拦
        return "今天额度已用完，明天再来吧"
    result = await graph_answer(session_id, question)                 # ③ 放行才答题
    _answer_cache[cache_key] = result
    await quota_db.count_one(question, result)                  # ④ 记账
    return result


async def stream_answer(session_id, question):
    """图版流式：把 while True 换成 agent_graph.astream"""
    cache_key = (session_id, question)
    if cache_key in _answer_cache:                       # ① 白送
        yield f"data: {_answer_cache[cache_key]}\n\n"
        return
    if await quota_db.used_today() >= quota_db.DAILY_LIMIT:   # ② 拦
        yield f"data: 今天额度已用完，明天再来吧\n\n"
        return

    history = _sessions_lc.get(session_id, [])           # ← 换成图版历史
    msgs = [SystemMessage(SYSTEM_PROMPT), *history, HumanMessage(question)]

    text_buf = ""          # 攒这一轮的正文（用来存缓存 + 写 qa_log）
    n_text = 0
    final_state = None
    try:
        print(f"🔎 [图-流] 开始：session={session_id} question={question!r}")
        async for mode, payload in agent_graph.astream(
            {"messages": msgs}, stream_mode=["messages", "values"]
        ):
            if mode == "messages":
                chunk, meta = payload
                if meta.get("langgraph_node") == "model" and chunk.content:
                    n_text += 1
                    piece = chunk.content.replace("\n", " ")     # 换行会把 SSE 切坏
                    text_buf += piece
                    yield f"data: {piece}\n\n"
            else:                                            # mode == "values"
                final_state = payload                        # 最后一次就是最终状态
        print(f"🔎 [图-流] 结束：正文片数={n_text}")
    except Exception as e:
        print("❌ [图-流] stream_answer 出错：", repr(e))
        yield f"data: 【流式中断】{type(e).__name__}: {e}\n\n"

    if final_state is not None:                              # ③ 存历史
        _sessions_lc[session_id] = final_state["messages"][1:]
    if text_buf:                                             # ④ 答成才记账
        _answer_cache[cache_key] = text_buf
        await quota_db.count_one(question, text_buf)

# ================= 自测（改造 D） =================
if __name__ == "__main__":
    print("自检：", col_b.name, col_b.count())          # 期望：math_note_V7 59
    print(asyncio.run(graph_answer("t1","奇变偶不变是什么口诀？")))
