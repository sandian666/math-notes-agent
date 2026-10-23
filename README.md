# 数学知识库 Agent（math-notes-agent）

把本地数学资料做成知识库，用一个**能自己决定"要不要查资料 / 要不要调工具"**的 Agent 来回答，网页 SSE 流式输出。
个人学习项目，重点练五件事：**RAG 检索** · **多工具 Agent 循环** · **全链路异步 + 容器化** · **成本控制** · **生产化（会话持久化 / 工具健壮性 / 评测与可观测）**。

技术栈一路演进到了：FastAPI + SSE 流式 → MySQL 额度闸 → **Docker Compose 编排** → **全链路异步**（`AsyncOpenAI` + `aiomysql`）→ **LangGraph 编排 Agent** → **MCP 可插拔工具** → **官方 Checkpointer 会话持久化** → **评测跑分器 + 结构化日志**。

> 2026-10-23 更新：知识库从「59 条高中笔记」扩到 **529 条**，覆盖高等数学 / 线性代数 / 概率论与数理统计 / 高中数学，按「概念·定理·例题·易错」四类切块，元数据扩到 8 个字段并带章节溯源。

---

## 它能做什么

1. **数学知识问答**：检索本地知识库片段后作答，并**标明出处**（章节 + 类型）
2. **工具调用**：查天气（wttr.in）、当前时间、四则运算，以及一组**由 MCP server 提供**的字符串工具
3. **会话级多轮 + 持久化**：同一个 `session_id` 记得上文，对话状态落在 SQLite（`sessions_lc.db`），**服务重启也不丢**
4. **SSE 流式**：答案边生成边显示
5. **成本控制**：内存缓存（同问秒回、不记账）+ **每日额度闸落 MySQL**（重启不丢）
6. **入口口令闸**：cookie / 网址 `?k=` / Basic Auth 三种进法
7. **工具健壮性**：单工具 15s / 整轮 120s 超时；**只有幂等工具**才自动退避重试；写操作（`save_note`）执行前**停下来问人**（y/n）
8. **评测与可观测**：`obs.py` 把每轮问答落成一行 JSON（token / 工具 / 延迟 / 重试），`eval/` 跑用例集出报告、并能和上一轮对比"变好还是变差"

---

## 知识库现状

| 项 | 值 |
|---|---|
| 条数 | **529**（生成 470 + 补漏 56，含高中 59） |
| 学科分布 | 高数 201 · 概率 139 · 线代 130 · 高中 59 |
| 切块方式 | 按**类型**切：概念 / 定理 / 例题 / 易错（150~250 字） |
| 平均长度 | 144 字，最短 ≥ 60 字（已清理一句话块） |
| 元数据 | 8 个键：学科 · 章节 · 类型 · 难度 · 来源 · 序号 · 切法 · 卷种 |
| 向量 | 智谱 `embedding-3`，2048 维，ChromaDB cosine |
| 检索 | `n_results=3`，纯向量召回（**未上混合检索 / rerank**，见已知限制） |
| 当前集合 | `math_note_v9`（由 `current_collection.txt` 指定，切换不用改代码） |

### 建库流程

```
考纲章节表 ──▶ build_v8.py ──▶ blocks.jsonl ──┐
                         （概念/定理/例题/易错） │
POINTS 考点清单 ─▶ topup.py ──▶ 补漏的块 ──────┼─▶ blocks_all.jsonl ──▶ embedding ──▶ Chroma
高中老库 59 条 ──────────────────────────────┘        （唯一真源）
```

- `rag_v8/build_v8.py`：按 22 个章节批量生成，每章 20 条，写 `blocks.jsonl`（断点续跑）
- `rag_v8/topup.py`：按 `POINTS` 里的 42 个高频考点**只补缺的**，`done_points.json` 记账防重复
- `blocks_all.jsonl`：合并后的最终语料（529 条），**重建库只认它**

### 重建向量库

```bash
# 1. 先停服务（两个进程同时写 chroma 会写坏索引）
# 2. 读 blocks_all.jsonl → embedding → 写入集合 → 改 current_collection.txt
```

> ⚠️ 踩过的坑：在 `main.py` 运行中执行写入 `chroma_db` 的脚本，会导致 HNSW 索引文件残缺
> （`Error loading hnsw index`），且 `get()` 也读不出来。**任何写库脚本必须先停服务**。
> 恢复方式：用 `blocks_all.jsonl` 重新 embedding 建一个新集合（本次即 `math_note_v8` → `math_note_v9`）。

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
        │
        ▼
  SQLite（会话存档）＋ logs/run.jsonl（结构化日志）
  sessions_lc.db（官方 Checkpointer）／ obs.py
```

| 文件 | 作用 |
|---|---|
| `main.py` | FastAPI 服务：路由 + 入口口令中间件 + **lifespan**（启动时连 MCP server 并并入工具表） |
| `agent.py` | Agent 主体：`LangGraph` 图（model / tools 节点 / 条件边 / 环）、4 个本地工具、缓存、会话记忆、额度闸调用；**工具超时/重试/人工确认** + **Pydantic 结构化输出** |
| `mcp_server.py` / `mcp_bridge.py` | 独立进程的 MCP server（`reverse_text` / `char_count` / `now`）与其桥接 |
| `quota_db.py` | 额度账本：`aiomysql` 异步连接池，读写 `daily_quota` / `qa_log` |
| `obs.py` | 结构化日志：每轮问答追加一行 JSON 到 `logs/run.jsonl` |
| `index.html` | 提问页面：原生 JS + `EventSource`；对话头像 / 网格背景 / 首页按钮 |
| `current_collection.txt` | **当前生效的向量集合名**（改这个文件即可切库，`agent.py` 启动时读） |
| `blocks_all.jsonl` | 529 条语料真源（重建库、审计内容都靠它） |
| `rag_v8/build_v8.py` / `topup.py` | 批量生成 / 按考点补漏 |
| `create_db.py` / `init_db.py` | 旧版建向量库脚本 / 建 MySQL 表 |
| `fix_quota.py` | 一次性修复：重建 `daily_quota` 表（补 `user_key` 列） |
| `reset_quota.py` | 把**今天**的额度计数清零，方便演示前重置 |
| `selfcheck.py` | 15 题离线抽检（直接调 `graph_answer`，绕过 HTTP 与额度） |
| `eval/run_eval.py` / `eval/cases.jsonl` | 评测跑分器 + 用例集：规则判分、故障注入、两轮对比 |
| `probe_*.py` | 零成本探针：验证工具重试 / 结构化输出 / 会话存档 |

---

## 快速开始

### 前置：四个环境变量

代码里**不写死任何 key / 密码**，全部读环境变量：

| 变量名 | 用途 | 读不到会怎样 |
|---|---|---|
| `ZHIPU_API_KEY` | 智谱：文本向量化（`embedding-3`）+ 建库时的生成模型 | 建库 / 检索失败 |
| `DEEPSEEK_API_KEY` | DeepSeek 官方：聊天模型 | 生成回答失败 |
| `S5_PASSWORD` | 网页入口口令 | 不设 = 入口不拦（fail open，**生产必须设**） |
| `S6_DB_PASSWORD` | MySQL root 密码 | `KeyError`（不默默连错库） |

Windows 下永久设置（设置后需**重开终端 / 重启 IDE**，环境变量是**进程启动时的快照**）：

```
setx ZHIPU_API_KEY "你的智谱key"
setx DEEPSEEK_API_KEY "你的DeepSeek官方key"
setx S5_PASSWORD "自定义的页面口令"
setx S6_DB_PASSWORD "你的MySQL密码"
```

### 方式 A：Docker Compose

```bash
docker compose up -d --build
# 打开 http://127.0.0.1:8000/?k=<S5_PASSWORD>
```

容器启动时自动跑 `init_db.py`（幂等）；向量库 `chroma_db` 随镜像提供。

### 方式 B：本机直接跑

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python init_db.py          # 建 MySQL 表
python main.py             # 默认不带热重载；开发时设 S5_RELOAD=1
```

需要一个可连的 MySQL：设 `S6_DB_HOST`（默认 `127.0.0.1`）和 `S6_DB_PASSWORD`。

> 若 `daily_quota` 报 `Unknown column 'user_key'`，跑一次 `python fix_quota.py` 重建该表。

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
| 语言 / 运行时 | Python 3.14（镜像 `python:3.14-slim`） |
| Web | FastAPI + Uvicorn（ASGI） |
| Agent 编排 | **LangGraph**（State + reducer + 条件边 + 环） |
| 工具协议 | **MCP**（官方 `mcp` SDK，stdio 传输，工具跑在独立进程） |
| LLM 调用 | OpenAI SDK（兼容模式）/ `langchain-openai` 的 `ChatOpenAI` |
| 模型 | 聊天 `deepseek-flash`；向量 `embedding-3`（智谱）；建库生成 `glm-4-flash` |
| 向量库 | ChromaDB（PersistentClient，cosine，2048 维） |
| 数据库 | MySQL 8 + **`aiomysql` 异步连接池** |
| 会话持久化 | LangGraph **官方 Checkpointer**（`AsyncSqliteSaver`，存 `sessions_lc.db`） |
| 评测 / 可观测 | 自写 `eval/` + `obs.py`（一行一 JSON） |
| 前端 | 原生 HTML + JS（`EventSource`），无框架 |

---

## 关键技术点（为什么这么写）

### 1. 全链路异步：不许有人堵住事件循环

`async def` 的**壳** + 同步的**芯**是两路里最差的一档：同步调用会在**事件循环那条线程**上原地等，**一个请求能把所有请求挡在门外**。

- 聊天：`AsyncOpenAI` + `await`；数据库：`aiomysql` 连接池
- 流式：异步生成器 + `agent_graph.astream(...)`
- 没有异步版的库（`chromadb`、`requests`）：`await asyncio.to_thread(...)` 兜住（**绷带，不是终点**）

**实测**（3 个并发 `/ask`）：

| 指标 | 改造前 | 改造后 |
|---|---|---|
| 3 个并发总耗时 | 5.34 s（逐个相加） | **2.49 s** |
| `/hello` 最大延迟 | **5.34 s** | **0.10 s** |

### 2. LangGraph：把 `while True` 换成图

手写循环 = `while True`（调模型 → 看要不要工具 → 执行 → 塞回 messages）+ 一个 `counted` 开关防重复记账。
上图之后：`State`（`messages` 用 `add_messages` reducer **追加**）+ `model`/`tools` 两个节点 + 条件边 + `tools → model` 的**环**。

- 流式用 `astream(stream_mode=["messages", "values"])`：token 逐片推前端，最终状态存历史
- 好处：循环边界由框架管，`counted` 开关可以删掉
- 代价：多三个依赖、黑盒更厚、要懂 State/reducer/边

### 3. MCP：工具从"写死在代码里"变成"可插拔"

`mcp_server.py` 是**独立进程**，用自己的 docstring + 类型标注生成工具描述；`mcp_bridge.py` 把 JSON Schema 翻成 pydantic 模型，生命周期挂在 FastAPI 的 lifespan 上。

- 加工具只改 `mcp_server.py`，`agent.py` 一行不动
- 代价：多一层进程/协议、生命周期要自己管

### 4. 容器化 + 数据持久化

`docker compose down` 删容器和网络但**留卷**（`down -v` 才真删）。
`depends_on: {condition: service_healthy}` 是必需的：app 启动就要连库。

### 5. 成本控制

- **缓存**：`(session_id, question)` 严格相等才命中，命中即秒回且**不记账**
- **额度闸落库**：`daily_quota` 用 `UNIQUE(user_key, quota_date)` 保证**一天一人一行**，写入用 `INSERT ... ON DUPLICATE KEY UPDATE used = used + 1`；**被拦住的那次不写 `qa_log`**
- 内存版一重启就归零 = 等于没闸

### 6. 安全

- 全部密钥/密码走环境变量，**仓库里没有字面量**
- SQL 全用参数化占位符（`%s`）
- 网页入口有口令闸（cookie / `?k=` / Basic Auth）

### 7. RAG：从"按段落切"到"按类型切"

早期记录：500 字硬切（相似度 0.4175）→ 按段落切（~130 字）**0.5996**。结论：**切太长会语义稀释**。

扩库后改成**按类型切**：

| 类型 | 作用 | 长度 |
|---|---|---|
| 概念 | 是什么、为什么 | 150~250 字 |
| 定理 | 条件 + 结论（条件必须写全） | 150~250 字 |
| 例题 | 典型小题 + 关键步骤 | 150~250 字 |
| 易错 | 常见错误 + 正确做法 | 150~250 字 |

好处：检索命中一条就是**完整的一个语义单元**，不会像段落切那样把定理的条件和结论切散。
system 提示词要求"基于给定材料回答 + 必须标明出处" → 抑制幻觉、答案可追溯。

**元数据 8 键**：学科 / 章节 / 类型 / 难度 / 来源 / 序号 / 切法 / 卷种。
`卷种`（数一 / 数二 / 数三 / 高中）用逗号串存储，方便按"数二不考概率"这类规则过滤。

### 8. 会话持久化：官方 Checkpointer

`thread_id = session_id`，状态存 `sessions_lc.db`（`AsyncSqliteSaver`）：

- **重启不丢**；🔴 **懒加载**：模块导入时没有事件循环，等第一次真要跑图才连库
- 🔴 **SystemMessage 每轮现拼、不入库**：否则库里越攒越多
- 早期手写过 `session_store.py`，并发/部分写/格式演进全得自己扛 → 已删

### 9. 工具健壮性：超时 / 幂等重试 / 危险工具人工确认

- **两层超时**：单工具 15s、整轮 120s
- 🔴 **超时 ≠ 异常**：超时直接放弃；其他异常才退避重试（0.5s → 1.0s）
- 🔴 **只有幂等工具才敢重试**：`search_notes` / `get_weather` / `get_time` / `calculate`；写操作只试 1 次
- **危险工具人工确认**：`save_note` 走 LangGraph `interrupt()`，回 `y` 才继续
- 工具**任何情况都返回字符串、绝不抛**

### 10. 结构化输出：Pydantic 校验 + 把报错原文喂回去

`NoteAnswer`（`answer` / `sources` / `enough_info` / `confidence`）用 `with_structured_output(..., include_raw=True, method="json_mode")`：

- 不合格时把 pydantic 报错原文 + 上次错误输出一起塞回去，最多重试 2 次
- 🔴 **调用失败 ≠ 格式不合格**：网络/额度问题不重试
- 目前只在探针 `probe_struct.py` 验证过，**未接进 `/ask` 主链路**

### 11. 评测与可观测

- `obs.py` 每轮追加一行 JSON（`cache_hit` / `usage` / `tools` / `retries` / `timeout` / `latency_ms`）
- 🔴 只统计"本轮新增"消息：图返回的是整段历史，直接求和会重复计入
- `eval/` 规则判分零成本可复现；🔴 **每条用例一个全新 `session_id`**
- 故障注入：把工具临时换成慢桩/报错桩，让超时可复现
- `selfcheck.py`：15 题离线抽检，直接调 `graph_answer`，不受额度限制

---

## 已知限制 / 下一步

- **纯向量检索**：529 条时 top-3 已偶有噪音（问"洛必达"会带出线代块），计划上 **BM25 混合检索 + rerank**
- **评测集未扩**：现有 `eval/cases.jsonl` 针对旧知识库，扩库后还没做 60 题（单章节 / 跨章节 / 负样本 / 易混淆）的分学科报告
- 语料由 LLM 按考纲生成 + 人工抽检，**不是逐条对源核对**；面试级严谨度不够
- 缓存不持久（重启清空）、无失效机制
- `S5_PASSWORD` 不设置时入口**完全不拦**（fail open）
- `print` 和 `obs.py` 并存，还没统一到 `logging`
- MCP server 是子进程，**没有崩溃重启**机制
- 结构化输出还没接进 `/ask`

---

## 部署

已部署到云服务器（Ubuntu + `systemd` 常驻自启 + 入口口令闸）：

```
服务名    s5.service
项目目录  /home/ubuntu/rag-qa-agent
环境变量  /home/ubuntu/s5.env（systemd 通过 run_s5.sh 注入）
启动      systemctl restart s5
切库      echo "math_note_v9" > current_collection.txt && systemctl restart s5
```

公网演示地址与口令不放在本仓库里。
