# ===== S5 / agent.py =====
import os, json, datetime, chromadb, requests,quota_db
from openai import OpenAI

# ================= 常量与路径（改造 A） =================
# 【照抄】把"当前文件所在的文件夹"取出来，得到本py文件所在文件夹，作为基准路径，程序以后放哪里都能跑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 拼接出配置文件、向量库目录的绝对路径，防止运行位置改变导致找不到文件
COLLECTION_FILE = os.path.join(BASE_DIR, "current_collection.txt")     # ← 以"文件自己"为基准
CHROMA_DIR      = os.path.join(BASE_DIR, "chroma_db")                  # ← 跟工作目录【无关

with open(COLLECTION_FILE,encoding="utf-8") as f:
    COLLECTION_NAME=f.read().strip()

_answer_cache = {}
_sessions = {}

# ================= 两个 client（改造 B） =================
embed_client = OpenAI(api_key=os.environ.get("ZHIPU_API_KEY"),
                      base_url="https://open.bigmodel.cn/api/paas/v4/")
chat_client = OpenAI(api_key=os.environ.get("ali-deepseek-api-key"),
         base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",)
chat_model = "deepseek-v4.1-flash"

# ================= 向量库 =================
chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
col_b = chroma_client.get_collection(name=COLLECTION_NAME)

# ================= 4 个工具函数 =================

def search_notes(question):# 把问题转换成向量数据，然后取第一个数据的向量
    qv = embed_client.embeddings.create(model="embedding-3", input=[question]).data[0].embedding
    result= col_b.query(query_embeddings=[qv], n_results=3)  # 问题向量与chunk向量对比相似度，取n_result个结果
    docs, meta = result["documents"][0], result["metadatas"][0]
    result = ""
    for i, (doc,source_meta) in enumerate(zip(docs,meta)):
        result += f"【片段{i + 1}】（来源：{source_meta['小节']}）\n{doc}\n\n"
    return result


def get_weather(city):
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


def get_time():
    now = datetime.datetime.now()  # 调用datetime模块的datetime类的now方法
    return now.strftime('%Y-%m-%d %H:%M:%S')  # 直接返回字符串，不要花括号


def calculate(a, b, op):
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

# ================= tools + tools_map =================
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询某个城市的当前天气。",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "城市名，如 北京"}},
                "required": ["city"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "获取当前的日期和时间。当用户询问现在几点、今天几号时使用。",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "对两个整数执行四则运算并返回结果。当用户需要做加减乘除计算时使用.",
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "integer", "description": "第一个运算数"},
                               "b": {"type": "integer", "description": "第二个运算数"},
                               "op":{"type": "string",
                                     "enum":["+","-","*","/"],   #enum = 枚举，作用是把取值限定在给定的列表里
                                     "description":"要执行的运算类型。"}
                               },
                "required": ["a","b","op"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_notes",
            "description": "根据用户提出的问题检索文本得到答案，当用户询问有关高中数学知识的问题时，调用此函数。",
            "parameters": {
                            "type": "object",
                            "properties": {"question":{
                                               "type":"string",
                                               "description": "从笔记里查询问题答案，答案尽可能科学严谨。"}
                            },
                            "required": ["question"] }
            }
    },
]

tools_map={
    "get_weather":get_weather,
    "get_time":get_time,
    "calculate":calculate,
    "search_notes":search_notes
}                           # 4 个工具的说明书

# ================= 核心：answer() =================
#输入会话号 + 问题 → 返回答案；同时把这一轮对话存进该会话
def answer(session_id,question):
    history=_sessions.get(session_id,[])  # 第一次来 → 拿不到 → 空列表
    messages = [
        {"role": "system", "content": """你是一个严谨的老师，用户询问关于高中数学有关的知识时，
        你要根据文本检索相关内容并标明出处回答，若该知识文本没有请回复没有检索到相关内容。
        用户询问天气、时间或需要计算时，调用相应的工具。"""},
        *history,
        {"role": "user", "content": question},
    ]

    while True:                                   # ← 只留【内层】：管"模型 ↔ 工具"的循环
        resp = chat_client.chat.completions.create(   # 修②：不要引号，不带 s
            model=chat_model,
            messages=messages,
            tools=tools,
        )
        msg = resp.choices[0].message
        messages.append(msg.to_dict())

        if not msg.tool_calls: # 模型不再要工具 → 这就是最终答案
            _sessions[session_id] =messages[1:]       #存历史
            return msg.content                    # 修③：返回，不打印

        for tool_call in msg.tool_calls:          # 修④：下面 4 行必须是【for 里面】的
            function_name = tool_call.function.name
            arg = json.loads(tool_call.function.arguments)
            result = tools_map[function_name](**arg)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

#带缓存的问答：同一个问题问过就不再花钱重算.
def answer_cached(session_id, question):
    cache_key = (session_id, question)
    if cache_key in _answer_cache:                        # ① 答过的题白送、不记账
        return _answer_cache[cache_key]
    if quota_db.used_today() >= quota_db.DAILY_LIMIT:     # ② 查账本：满了就拦
        return "今天额度已用完，明天再来吧"
    result = answer(session_id, question)                 # ③ 放行才答题
    _answer_cache[cache_key] = result
    quota_db.count_one(question, result)                  # ④ 记账
    return result


def stream_answer(session_id, question):
    history=_sessions.get(session_id,[])
    messages = [
        {"role": "system", "content": """你是一个严谨的老师，用户询问关于高中数学有关的知识时，
        你要根据文本检索相关内容并标明出处回答，若该知识文本没有请回复没有检索到相关内容。
        用户询问天气、时间或需要计算时，调用相应的工具。"""},
        *history,
        {"role": "user", "content": question},
    ]
    Today = datetime.date.today()
    cache_key = (session_id, question)
    if cache_key in _answer_cache:
        yield f"data: {_answer_cache[cache_key]}\n\n"
        return
    if quota_db.used_today() >= quota_db.DAILY_LIMIT:
        yield f"data: 今天额度已用完，明天再来吧\n\n"
        return
    counted = False  # 一轮回答可能转几圈 while，只许记一次账
    while True:
        tool_buf = {}    # 收集箱：{index: {"id":..., "name":..., "arguments": ""}}
        #作用：把模型流式返回的工具调用碎片拼接成完整工具调用。
        text_buf = ""    # 这一轮吐出来的正文（边流边攒，用来存历史）
        try:
            print(f"🔎 开始流式：session={session_id} question={question!r}")
            n_text = 0          # 收到多少片"正文"
            n_tool = 0          # 收到多少片"工具调用"（正文里的工具碎片不算字）
            resp = chat_client.chat.completions.create(
                model=chat_model,
                messages=messages,
                tools=tools,
                stream=True,
            )
            for chunk in resp:                    # ← 一片片取
                d = chunk.choices[0].delta    # 拿第一条候选，delta表示这一片新增了什么
                if d.tool_calls:  # 如果有工具调用信息，就进入分支
                    n_tool += 1    # 工具调用碎片＋1
                    for tc in d.tool_calls:   # 遍历这一批工具调用片段
                        # 通过 tc.index 找到这个工具调用的“槽位”，setdefault 的作用：有就复位，没有就创造。
                        slot = tool_buf.setdefault(tc.index, {"id": None,
                                                              "name": None, "arguments": ""})
                        # 判空再赋值
                        if tc.id:      # 如果这段工具调用包含 id，就设置进去
                            slot["id"] = tc.id
                        if tc.function and tc.function.name:  # 如果有函数名字段，就保存它
                            slot["name"] = tc.function.name
                        if tc.function and tc.function.arguments:  # 如果函数参数片段存在，就拼接到参数字符串里
                            slot["arguments"] += tc.function.arguments  # 由于流式返回可能分成很多段，所以不能直接覆盖，要累加。
                piece = d.content         # ← 取出这次增量返回的正文内容。
                if piece:                 # ← None 就跳过
                    n_text += 1           # ← 正文片段计数＋1
                    piece = piece.replace("\n", " ")    # 把正文里的换行先换掉，防止浏览器识别成消息结束
                    text_buf += piece
                    yield f"data: {piece}\n\n"   # 直接把这段正文以 SSE 格式发给前端。
            print(f"🔎 流结束：正文片数={n_text} 工具片数={n_tool}")
        except Exception as e:
            # 别再静默断流：把错误原样推到页面上，看得见才好查
            print("❌ stream_answer 出错：", repr(e))
            yield f"data: 【流式中断】{type(e).__name__}: {e}\n\n"  #把错误内容发给前端，让页面显示“流式中断”。
        if not tool_buf:        # 这一轮没有工具调用 = 这就是最终答案 → 存历史、收工
            if text_buf:
                messages.append({"role": "assistant", "content": text_buf})
            _sessions[session_id] = messages[1:]   # 保留会话历史，并去掉系统提示词
            if text_buf:
                _answer_cache[cache_key] = text_buf
                if not counted:  # 答成才记账
                    quota_db.count_one(question, text_buf)
                    counted = True
            break
        print(f"🔎 第 1 轮拼好的工具调用 = {tool_buf}")
        # —— 有工具要调：先把碎片拼成一条完整的 assistant 消息 ——
        calls = []   # 新建 calls 列表，准备装完整的工具调用对象
        for idx, slot in sorted(tool_buf.items()):    # 按工具调用的索引排序
            #把一个完整工具调用加入列表
            calls.append({
                "id": slot["id"],
                "type": "function",
                "function": {"name": slot["name"], "arguments": slot["arguments"]},
            })
            # 表明这是函数类型工具调用
        messages.append({"role": "assistant", "content": None, "tool_calls": calls})

        # —— 执行工具
        for call in calls:
            function_name = call["function"]["name"]
            arg = json.loads(call["function"]["arguments"])  # ← 拼完了才敢 loads
            result = tools_map[function_name](**arg)
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": result,
            })
        # 注意：这里【不要 break】→ 回到 while 顶部，再问模型一次，那一轮才吐正文

# ================= 自测（改造 D） =================
if __name__ == "__main__":
    print("自检：", col_b.name, col_b.count())          # 期望：math_note_V7 59
    print(answer("t1","奇变偶不变是什么口诀？"))


