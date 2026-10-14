# ===== S5 / agent.py =====
import asyncio, os, time, datetime, chromadb, requests, quota_db, aiosqlite
from openai import OpenAI
from typing import Annotated, Literal, TypedDict
from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage,AIMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import interrupt, Command

# ================= 常量与路径（改造 A） =================
# 把"当前文件所在的文件夹"取出来，得到本py文件所在文件夹，作为基准路径，程序以后放哪里都能跑
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


NOTES_FILE = os.path.join(BASE_DIR, "my_notes.txt")

def save_note(note: str) -> str:
    """把一条内容追加保存到本地笔记文件 my_notes.txt。这是写操作，重复调用会多出一条。"""
    with open(NOTES_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {note}\n")
    return f"已保存 1 条笔记（文件 {os.path.basename(NOTES_FILE)}）"
# ================= LangGraph 版 Agent 图 =================
SYSTEM_PROMPT = """你是一个严谨的老师，用户询问关于高中数学有关的知识时，
你要根据文本检索相关内容并标明出处回答，若该知识文本没有请回复没有检索到相关内容。
用户询问天气、时间或需要计算时，调用相应的工具。用户要求把某句话存成笔记时，调用 save_note。"""

# 把 4 个原始函数"包装成"模型能选用的工具（用 docstring 当描述、类型标注当参数表）
LC_TOOLS = [tool(search_notes), tool(get_weather), tool(get_time), tool(calculate), tool(save_note)]
LC_TOOL_MAP = {t.name: t for t in LC_TOOLS}
TOOL_TIMEOUT = 15    # 单个工具最多等 15 秒
RUN_TIMEOUT  = 120   # 一整轮问答最多 2 分钟
# 🔴 幂等白名单：只有列在这里的工具才允许自动重试。
#    "默认不重试"= 安全默认 → 以后 add_mcp_tools 并进来的新工具不会悄悄被重试。
RETRYABLE_TOOLS = {"search_notes", "get_weather", "get_time", "calculate"}
# 🔴 危险工具：有副作用（写文件/改库/发消息）→ 执行前人工确认，且永不自动重试。
DANGEROUS_TOOLS = {"save_note"}
MAX_TOOL_RETRY = 2      # 最多重试 2 次（总共 3 次尝试）
RETRY_BACKOFF  = 0.5    # 退避基数：第 1 次失败等 0.5s，第 2 次等 1.0s


def add_mcp_tools(extra):
    """把 MCP 来的工具并进工具表（服务启动时调用一次）"""
    global lc_llm_with_tools
    LC_TOOLS.extend(extra)
    LC_TOOL_MAP.update({t.name: t for t in extra})  # 更新工具名字映射表
    lc_llm_with_tools = lc_llm.bind_tools(LC_TOOLS) # 重新绑定模型工具
    print(f"🔌 [MCP] 已并入 {len(extra)} 个工具，当前共 {len(LC_TOOLS)} 个")

lc_llm = ChatOpenAI(
    model=chat_model,
    base_url="https://api.deepseek.com/v1",
    api_key=os.environ["DEEPSEEK_API_KEY"],
    timeout=60,        # 单次请求最多等 60 秒（默认 600！）
    max_retries=1,     # 只重试 1 次（默认 2）
)
lc_llm_with_tools = lc_llm.bind_tools(LC_TOOLS)


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
# Annotated给类型附加额外元信息，add_messages是LangGraph专门的reducer函数，能把新消息追加到老列表里

async def lc_call_model(state: AgentState):
    print(f"🔵 [图] call_model：现有 {len(state['messages'])} 条消息")
    # 🔴 系统提示词只在这一刻拼上去，不写进 state。
    # 因为 state 现在会被 checkpointer 永久存下来，要是每轮都塞一条，库里就会越攒越多 system。
    msgs = [SystemMessage(SYSTEM_PROMPT)] + [
        m for m in state["messages"] if not isinstance(m, SystemMessage)
    ]  # 遍历当前状态中的所有消息，只保留不是 SystemMessage 的消息。
    resp = await lc_llm_with_tools.ainvoke(msgs)
    return {"messages": [resp]}


def _is_idempotent(name: str) -> bool:  # 判断这个工具是否允许按照当前策略重试。
    """幂等 = 做 1 次和做 N 次结果一样 → 只有这种工具才敢自动重试。"""
    return name in RETRYABLE_TOOLS


class ToolTimeout(Exception):
    """单独定一个异常类型：好让上层区分'超时'和'异常'——这两条路走法不一样。"""


async def _call_tool_once(name: str, args: dict):
    """真实执行一次工具。只负责'把超时翻译成 ToolTimeout'，不做任何重试。"""
    try:
        return await asyncio.wait_for(LC_TOOL_MAP[name].ainvoke(args), timeout=TOOL_TIMEOUT)
    except asyncio.TimeoutError as e:
        # wait_for 只是"我不再等了"，线程还在后台跑
        raise ToolTimeout(f"{name} 超过 {TOOL_TIMEOUT} 秒没返回") from e


async def _run_with_retry(name: str, args: dict) -> str:
    """重试策略全在这：幂等才重试 · 超时不重试 · 异常才重试。任何情况都返回字符串，绝不抛。"""
    tries = MAX_TOOL_RETRY + 1 if _is_idempotent(name) else 1  # _is_idempotent(name)：幂等
    for i in range(1, tries + 1):
        try:
            return await _call_tool_once(name, args)
        except ToolTimeout as e:
            # 🔴 超时通常是"对方挂了"，重试只是再干等一遍 → 直接放弃
            print(f"⏰ [重试] {name} 超时 → 不重试（超时≠异常）")
            return f"工具超时：{e}，请换关键词或直接告诉用户稍后再试"
        except Exception as e:   # 捕获其他普通异常。
            if i < tries:
                wait = RETRY_BACKOFF * i   # 退避方式计算等待重试时间
                print(f"🔁 [重试] {name} 第 {i} 次失败（{type(e).__name__}: {e}）→ {wait}s 后再试")
                await asyncio.sleep(wait)  # 异步等待
                continue
            print(f"❌ [重试] {name} 试了 {tries} 次仍失败 → 放弃")
            return f"工具出错：{name} 执行失败（{type(e).__name__}: {e}）"


async def lc_run_tools(state: AgentState):
    out = []
    for tc in state["messages"][-1].tool_calls:
        name, args = tc["name"], tc["args"]

        # ① 危险工具：先把图停下来问人，答 y 才继续（LangGraph 的 interrupt）
        if name in DANGEROUS_TOOLS:
            answer = interrupt({"tool": name, "args": args,
                                "ask": f"即将执行写操作 {name}({args})，确认吗？(y/n)"})
            if str(answer).strip().lower() not in ("y", "yes"):
                print(f"🚫 [确认] 用户拒绝 {name} → 不执行")
                out.append(ToolMessage(content=f"用户拒绝执行 {name}，本次未做任何改动",
                                       tool_call_id=tc["id"]))
                continue
            print(f"✅ [确认] 用户同意 {name} → 执行")

        print(f"🔧 [图] 执行工具 {name}({args}) 上限 {TOOL_TIMEOUT}s "
              f"{'（幂等·可重试）' if _is_idempotent(name) else '（非幂等·不重试）'}")
        t0 = time.monotonic()
        result = await _run_with_retry(name, args)   # ← 这里绝不抛异常
        print(f"⏱️ [图] {name} 耗时 {time.monotonic() - t0:.2f}s")
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
# 🔴 懒加载：模块导入时还没有事件循环，AsyncSqliteSaver 构造会直接报错；
#    所以等到"第一次真的要用图"（也就是第一次请求）时才连库 + 装配。
_CHECKPOINT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions_lc.db")
_graph_mem = None      # 装配好的"带记忆的图"
_saver = None          # 留个引用，别被垃圾回收
_conn = None           # 新增：留个引用，关停时要用它 close()

async def _get_graph():
    global _graph_mem, _saver, _conn   # 🔴 _conn 必须在这里：漏了它，下面的赋值就变成局部变量，
                                       #    模块级 _conn 永远是 None → shutdown_graph() 静默不干活
    if _graph_mem is None:  # 第一次调用时：_graph_mem is None条件成立，于是执行初始化，连接数据库
        _conn = await aiosqlite.connect(_CHECKPOINT_DB)
        _saver = AsyncSqliteSaver(_conn)        # 创建一个能把 LangGraph 状态保存到 SQLite 的异步存档器。
        await _saver.setup()     # 初始化 checkpoint 数据库表。它通常会创建 LangGraph 所需的表和索引。
        _graph_mem = _lc_builder.compile(checkpointer=_saver) # 编译图时，把 saver 挂载到图上，用 _saver 管理 checkpoint
        print(f"🧠 [记忆] checkpointer 就绪：{_CHECKPOINT_DB}")
    return _graph_mem


async def shutdown_graph():
    """服务关停时调用：关掉 sqlite 连接 → 不再刷 'Event loop is closed'。"""
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None
        print("🧠 [记忆] checkpointer 连接已关闭")
    else:
        # 🔴 静默的分支最容易骗人：这里必须出声，否则"漏了 global"这种错你永远看不见
        print("🧠 [记忆] 没有需要关闭的连接（图还没装配过？）")


async def thread_info(session_id):  # 只是查看历史，没有新回答
    """打印某个 thread 在 checkpointer 里的状态：几条消息、都是什么消息。"""
    graph = await _get_graph()
    snap = await graph.aget_state({"configurable": {"thread_id": session_id}}) # 某个对话的状态快照
    msgs = snap.values.get("messages", [])
    print(f"thread={session_id} 消息数={len(msgs)} 下一步={snap.next}")
    for m in msgs:
        print("   -", type(m).__name__, str(getattr(m, "content", ""))[:50])
    return len(msgs)


async def graph_answer(session_id, question=None, resume=None):
    """question = 新问题；resume = 人工确认的答复（y/n）。两者二选一。"""
    graph = await _get_graph()   # 执行函数后，graph是已经连接 SQLite 记忆功能的 LangGraph
    # 创建本次运行的配置，🔴 thread_id = 我们的 session_id，checkpointer 靠它区分"谁的历史"
    cfg = {"configurable": {"thread_id": session_id}}
    payload = Command(resume=resume) if resume is not None else {"messages": [HumanMessage(question)]}
    # 有 resume：恢复之前被 interrupt() 暂停的图，并把resume作为人工确认结果传回去；没有 resume：这是一个新问题，把它作为新消息交给图。
    try:  # 超时不当异常抛，而是当"一条工具结果"喂回模型 → 模型自己改道
        out = await asyncio.wait_for(graph.ainvoke(payload, config=cfg), timeout=RUN_TIMEOUT)
    except asyncio.TimeoutError:
        return f"抱歉，这一轮超过 {RUN_TIMEOUT} 秒还没算完，请把问题拆小一点再问我。"
    # 🔴 图"停在半路等人"时，返回的字典里会多一个 __interrupt__ 键
    pending = out.get("__interrupt__")
    if pending:  # 有值说明图停在了人工点
        return "【等待人工确认】" + pending[0].value["ask"]
    return out["messages"][-1].content


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

    graph = await _get_graph()
    cfg = {"configurable": {"thread_id": session_id}}

    text_buf = ""          # 攒这一轮的正文（用来存缓存 + 写 qa_log）
    n_text = 0
    try:
        print(f"🔎 [图-流] 开始：session={session_id} question={question!r}")
        async for mode, payload in graph.astream(
                {"messages": [HumanMessage(question)]}, config=cfg,
                stream_mode=["messages", "values"],
        ):
            if mode == "messages":
                chunk, meta = payload
                if meta.get("langgraph_node") == "model" and chunk.content:
                    n_text += 1
                    piece = chunk.content.replace("\n", " ")     # 换行会把 SSE 切坏
                    text_buf += piece
                    yield f"data: {piece}\n\n"
        print(f"🔎 [图-流] 结束：正文片数={n_text}")
    except Exception as e:
        print("❌ [图-流] stream_answer 出错：", repr(e))
        yield f"data: 【流式中断】{type(e).__name__}: {e}\n\n"

    if text_buf:                                             # ④ 答成才记账
        _answer_cache[cache_key] = text_buf
        await quota_db.count_one(question, text_buf)


# ================= 结构化输出（Pydantic + with_structured_output） =================
STRUCTURED_RETRY = 2   # 校验不合格最多再问 2 次（总共 3 次）

class NoteAnswer(BaseModel):
    """最终答案的结构化形态：字段说明会当"说明书"一起发给模型。"""
    answer: str = Field(description="给用户看的中文最终回答")
    sources: list[str] = Field(default_factory=list, description="引用到的小节名列表；没引用就给空列表")
    enough_info: bool = Field(description="检索到的资料是否足以回答这个问题")
    confidence: float = Field(ge=0, le=1, description="0 到 1 之间的置信度")
    # default_factory=list 而不是 = []：每个 NoteAnswer 实例都会得到一个独立的新列表，避免不同实例之间意外共享同一个列表。）

STRUCT_PROMPT = """你是一个严谨的数学老师。请只填结构化字段，不要任何额外解释。
若资料里没有依据，把 enough_info 设为 false，并在 answer 里写明"没有检索到相关内容"。只输出一个 JSON 对象。
"""

# 创建结构化模型，之后，模型会被要求输出符合 NoteAnswer 的结构，保留原始结果和解析错误
_STRUCT_LLM = lc_llm.with_structured_output(NoteAnswer, include_raw=True, method="json_mode")
# LangChain 会返回一个外层字典：
# out = {
#     "raw": ...,
#     "parsed": ...,
#     "parsing_error": ...
# }

async def _invoke_structured(msgs):
    """只负责"问模型一次"。单独拆出来 → 探针可以换成假模型，零成本验重试。"""
    return await _STRUCT_LLM.ainvoke(msgs)


async def structured_answer(question: str):
    """结构化回答：不合格就把 pydantic 的报错原文喂回模型，重问。全部用完还不合格 → 返回 None。"""
    msgs = [SystemMessage(STRUCT_PROMPT), HumanMessage(question)]
    for i in range(1, STRUCTURED_RETRY + 2):
        try:
            out = await _invoke_structured(msgs)
        except Exception as e:
            # 调用本身失败（网络/额度/模型报错）≠ 格式不合格 → 不重试，直接出声（跟上一刀"超时≠异常"同一思路）
            print(f"❌ [结构] 调用失败：{type(e).__name__}: {e}")
            return None

        err = out.get("parsing_error")
        if out.get("parsed") is not None and err is None: # 已经成功解析成 NoteAnswer并且没有解析错误
            print(f"✅ [结构] 第 {i} 次就拿到合格对象")
            return out["parsed"]

        print(f"🔁 [结构] 第 {i} 次不合格：{type(err).__name__}: {str(err)[:150]}…")
        # 🔴 关键：把"报错原文 + 模型上次的错误输出"一起带上，它才知道错在哪，如果没有，就回一个空的 AIMessage
        msgs = msgs + [
            out.get("raw") or AIMessage(content=""),
            HumanMessage(f"你上一次的输出不符合 schema，报错如下：\n{err}\n请重新只输出符合 schema 的结果，不要解释。"),
        ]
    print(f"❌ [结构] 重试 {STRUCTURED_RETRY} 次仍不合格 → 放弃")
    return None
# ================= 自测（改造 D） =================
if __name__ == "__main__":
    print("自检：", col_b.name, col_b.count())          # 期望：math_note_V7 59
    print(asyncio.run(graph_answer("t1","奇变偶不变是什么口诀？")))
