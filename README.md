# 高中数学笔记 Agent（math-notes-agent）

把本地高中数学笔记做成知识库，用一个**能自己决定"要不要查资料 / 要不要调工具"**的 Agent 来回答，网页 SSE 流式输出。
个人学习项目，重点练四件事：**RAG 检索** · **多工具 Agent 循环** · **全链路异步 + 容器化** · **成本控制**。

技术栈一路演进到了：FastAPI + SSE 流式 → MySQL 额度闸 → **Docker Compose 编排** → **全链路异步**（`AsyncOpenAI` + `aiomysql`）→ **LangGraph 编排 Agent** → **MCP 可插拔工具**。

---

## 它能做什么

1. **高中数学知识问答**：检索本地笔记片段后作答，并**标明出处**（RAG）
2. **工具调用**：查天气（wttr.in）、当前时间、四则运算，以及一组**由 MCP server 提供**的字符串工具
3. **会话级多轮**：同一个 `session_id` 记得上文
4. **SSE 流式**：答案边生成边显示
5. **成本控制**：内存缓存（同问秒回、不记账）+ **每日额度闸落 MySQL**（重启不丢）
6. **入口口令闸**：cookie / 网址 `?k=` / Basic Auth 三种进法

---

## 架构

```
浏览器 (index.html: EventSource)
        │  SSE
        ▼
┌──────────────────────────────┐
│ FastAPI（main.py）            │  路由 + 入口口令中间件 + lifespan
│  └─ agent.py                  │
│      └─ LangGraph 图          │
│          [model] ──条件边──▶ [tools] ──┐
│             ▲                          │
│             └──────────── 环 ◀─────────┘
│      工具来源：本地 4 个 + MCP 3 个
└──────────────────────────────┘
        │                    │
        ▼                    ▼
  MySQL（额度/日志）    MCP server（独立进程，stdio）
  aiomysql 异步连接池     mcp_server.py
```

| 文件 | 作用 |
|---|---|
| `main.py` | FastAPI 服务：路由 + 入口口令中间件 + **lifespan**（启动时连 MCP server 并并入工具表） |
| `agent.py` | Agent 主体：`LangGraph` 图（model 节点 / tools 节点 / 条件边 / 环）、4 个本地工具、缓存、会话记忆、额度闸调用 |
| `mcp_server.py` | **独立进程**的 MCP server（`reverse_text` / `char_count` / `now`） |
| `mcp_bridge.py` | MCP → LangChain 工具的桥：JSON Schema → pydantic 模型；会话生命周期 |
| `quota_db.py` | 额度账本：`aiomysql` 异步连接池，读写 `daily_quota` / `qa_log` |
| `index.html` | 提问页面：原生 JS + `EventSource`，`session_id` 存 localStorage |
| `init_db.py` / `create_db.py` | 一次性建表 / 建向量库脚本 |
| `Dockerfile` / `docker-compose.yml` | 容器化编排（app + MySQL） |

---

## 快速开始

### 前置：四个环境变量

代码里**不写死任何 key / 密码**，全部读环境变量：

| 变量名 | 用途 | 读不到会怎样 |
|---|---|---|
| `ZHIPU_API_KEY` | 智谱：文本向量化（`embedding-3`） | 建库 / 检索失败 |
| `DEEPSEEK_API_KEY` | DeepSeek 官方：聊天模型（`deepseek-flash`） | 生成回答失败 |
| `S5_PASSWORD` | 网页入口口令 | 不设 = 入口不拦（fail open，**生产必须设**） |
| `S6_DB_PASSWORD` | MySQL root 密码 | `KeyError`（不默默连错库） |

Windows 下永久设置（设置后需**重开终端 / 重启 IDE**，环境变量是**进程启动时的快照**）：

```
setx ZHIPU_API_KEY "你的智谱key"
setx DEEPSEEK_API_KEY "你的DeepSeek官方key"
setx S5_PASSWORD "自定义的页面口令"
setx S6_DB_PASSWORD "你的MySQL密码"
```

### 方式 A：Docker Compose（推荐）

```bash
# 1) 起容器（首次会 build 镜像）
docker compose up -d --build

# 2) 首次运行要建表 + 建向量库（进容器里跑）
docker compose exec app python init_db.py
docker compose exec app python create_db.py

# 3) 打开页面（口令就是你的 S5_PASSWORD）
#    http://127.0.0.1:8000/?k=<S5_PASSWORD>
```

> ⚠️ **关于数据卷**：`docker-compose.yml` 里 MySQL 用的是**外部命名卷**（`external: true` + 固定 `name`），意味着容器删了数据还在。
> 换台机器/别人克隆后**没有这个卷**，会起不来 —— 两种做法任选：
> - 先建卷：`docker volume create <compose 里那个 name>`
> - 或者把 `external: true` / `name:` 两行删掉，让 compose 自己建新卷（**空库，需要跑 `init_db.py`**）

### 方式 B：本机直接跑（不用 Docker）

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
python init_db.py                 # 建 MySQL 表
python create_db.py               # 建向量库
python main.py
```

需要一个可连的 MySQL：设 `S6_DB_HOST`（默认 `127.0.0.1`）和 `S6_DB_PASSWORD`。

---

## 接口

| 方法 | 路径 | 参数 | 说明 |
|---|---|---|---|
| GET | `/` | — | 提问页面（`index.html`） |
| GET | `/hello` | — | 连通性测试 |
| GET | `/ask` | `question`、`session_id` | 问答（非流式），返回 JSON |
| GET | `/ask_stream` | `question`、`session_id` | 问答（SSE 流式），网页用的就是它 |

接口文档：`http://127.0.0.1:8000/docs`（需先过入口口令）

---

## 技术栈

| 类别 | 用了什么 |
|---|---|
| 语言 / 运行时 | Python 3.13（镜像 `python:3.13-slim`） |
| Web | FastAPI + Uvicorn（ASGI） |
| Agent 编排 | **LangGraph**（State + reducer + 条件边 + 环） |
| 工具协议 | **MCP**（官方 `mcp` SDK，stdio 传输，工具跑在独立进程） |
| LLM 调用 | OpenAI SDK（兼容模式）/ `langchain-openai` 的 `ChatOpenAI` |
| 模型 | 聊天 `deepseek-flash`（DeepSeek 官方）；向量 `embedding-3`（智谱） |
| 向量库 | ChromaDB（PersistentClient，cosine） |
| 数据库 | MySQL 8 + **`aiomysql` 异步连接池** |
| 前端 | 原生 HTML + JS（`EventSource`），无框架 |
| 部署 | Dockerfile + docker compose（app + MySQL 两服务） |

---

## 关键技术点（为什么这么写）

### 1. 全链路异步：不许有人堵住事件循环

`async def` 的**壳** + 同步的**芯**是两路里最差的一档：同步调用会在**事件循环那条线程**上原地等，**一个请求能把所有请求挡在门外**。

- 聊天：`AsyncOpenAI` + `await`
- 数据库：`aiomysql` 连接池 + `await`
- 流式：`async def` 异步生成器 + `agent_graph.astream(...)`
- 没有异步版的库（`chromadb`、`requests`）：`await asyncio.to_thread(...)` 兜住（**绷带，不是终点**：线程池上限 `min(32, CPU+4)`，CPU 密集该用进程池）

**实测**（3 个并发 `/ask`，同一个服务同时被探活）：

| 指标 | 改造前 | 改造后 |
|---|---|---|
| 3 个并发总耗时 | 5.34 s（逐个相加） | **2.49 s**（≈ 单个耗时） |
| `/hello` 最大延迟 | **5.34 s**（事件循环被占死） | **0.10 s** |
| `qa_log` 三条时间戳 | 3:32:57 → 59 → 33:01（串行） | **4:29:52 ×3（同一秒，并发）** |

### 2. LangGraph：把 `while True` 换成图

原来的手写循环 = `while True`（调模型 → 看要不要工具 → 执行 → 塞回 messages）+ 一个 `counted` 开关防重复记账。
上图之后：`State`（`messages` 用 `add_messages` reducer **追加**而不是覆盖）+ `model`/`tools` 两个节点 + 条件边 `should_continue` + `tools → model` 的**环**。

- 流式用 `astream(stream_mode=["messages", "values"])`：**`messages` 拿 token 逐片推给前端**，`values` 拿最终状态存历史；
- 好处：循环边界由框架管，**`counted` 开关可以删掉**；节点只返回"要改的字段"，合并交给 reducer；
- 代价也要说清：多三个依赖、黑盒更厚（`ToolNode`/`tools_condition` 内部行为随版本变）、需要额外懂 State/reducer/边这些概念。

### 3. MCP：工具从"写死在代码里"变成"可插拔"

`mcp_server.py` 是**独立进程**，用自己的 docstring + 类型标注自动生成工具描述与参数表；`mcp_bridge.py` 负责把 MCP 的 JSON Schema 翻成 pydantic 模型，并把会话生命周期挂在 FastAPI 的 **lifespan** 上。

- **加一个工具只需要改 `mcp_server.py`**，`agent.py` 一行不动；
- 工具从此可以和 agent **不同进程、不同依赖**；
- 代价：多一层进程/协议（延迟、跨进程错误、调试要两头看）、生命周期要自己管、**远程 MCP server 会放大安全面**。

### 4. 容器化 + 数据持久化

`docker compose down` 会删容器和网络但**留卷**，所以 MySQL 数据还在（`down -v` 才是真删）。
`depends_on: {condition: service_healthy}` 是必需的：app 一启动就要连库（模块级建池），MySQL 没就绪会直接崩。

### 5. 成本控制

- **缓存**：`(session_id, question)` 严格相等才命中，命中即秒回且**不记账**；
- **额度闸落库**：`daily_quota` 用 `quota_date DATE NOT NULL UNIQUE` 保证**一天一行**，写入用 `INSERT ... ON DUPLICATE KEY UPDATE used = used + 1`（加数不加行）；**被拦住的那次不写 `qa_log`**。
  内存版一重启就归零 = 等于没闸，落库后"今天答了几次"才是重启带不走的账。

### 6. 安全

- 全部密钥/密码走环境变量，**仓库里没有任何字面量**；
- SQL 全用参数化占位符（`%s`），不拼字符串 → 从根上防注入；
- 网页入口有口令闸（cookie / `?k=` / Basic Auth）。

### 7. RAG 的一处调参记录

切片粒度：500 字硬切（相似度 0.4175）→ 按段落切（~130 字）**0.5996**。结论：**切太长会语义稀释**，段落切片更优。
system 提示词要求"基于给定材料回答 + 必须标明出处" → 抑制幻觉、答案可追溯。

---

## 已知限制 / 下一步

- 单知识库、单资料文件；`DAILY_LIMIT` 仍写死在 `quota_db.py`
- 缓存不持久（重启清空）、无失效机制
- `S5_PASSWORD` 不设置时入口**完全不拦**（fail open）
- 日志还是 `print`，没换成 `logging`
- MCP server 是子进程，**没有崩溃重启**机制
- 公网部署版本落后于本仓库（本仓库是最新版）

---

## 部署

已部署到云服务器（Ubuntu + `systemd` 常驻自启 + 入口口令闸）。公网演示地址与口令不放在本仓库里。
