# 高中数学笔记 Agent（S5）

把本地数学笔记做成知识库，用一个能"自己决定要不要查资料"的 Agent 来回答，并通过网页访问。
个人学习项目：练 **RAG 检索**、**多工具 Agent 循环**、**流式输出**，以及 **成本控制**（缓存 + 每日额度闸 + 额度落 MySQL）。

## 它能做什么

1. 查询当天天气（调用 wttr.in），并给出出行建议
2. 获取当前日期时间
3. 进行四则运算
4. 回答高中数学有关知识点，并**标明出处**（RAG 检索本地笔记）
5. **会话级多轮**：同一浏览器（同一个 `session_id`）能记住上文
6. **SSE 流式输出**：答案边生成边显示，不用干等
7. **成本控制**：内存缓存（同一问题秒回）+ 每日额度闸（`daily_quota` **落 MySQL，重启不丢**）

## 快速开始

### 0. 前置

- Python 3.10+（开发环境用 3.14）
- MySQL 8（本机安装，或用 Docker 一条命令起一个）：

      docker run -d --name mysql-s5 -e MYSQL_ROOT_PASSWORD=你的密码 -p 3306:3306 mysql:8.0

### 1. 装依赖

    python -m venv .venv
    .venv\Scripts\activate           # Windows
    pip install -r requirements.txt

### 2. 配置环境变量

四个变量，全部通过环境变量读取，**代码里不写死任何 key / 密码**：

| 变量名 | 用途 | 读不到会怎样 |
|---|---|---|
| `ZHIPU_API_KEY` | 智谱：文本向量化（`embedding-3`） | 建库 / 检索失败 |
| `DEEPSEEK_API_KEY` | DeepSeek 官方：聊天模型（`deepseek-flash`） | 生成回答失败 |
| `S5_PASSWORD` | 网页入口口令（**不给默认值**） | 不设 = **入口不拦**（fail open，生产必须设） |
| `S6_DB_PASSWORD` | MySQL 的 root 密码 | 直接 `KeyError`，不默默连错库 |

`S5_USER` 不设时默认为 `demo`。Windows 下永久设置：

    setx ZHIPU_API_KEY "你的智谱key"
    setx DEEPSEEK_API_KEY "你的DeepSeek官方key"
    setx S5_PASSWORD "自定义的页面口令"
    setx S6_DB_PASSWORD "你的MySQL密码"

设置后需要**重开终端 / 重启 IDE** 才会生效（环境变量是**进程启动时的快照**）。验证：

    python -c "import os; print(bool(os.environ.get('ZHIPU_API_KEY')), bool(os.environ.get('DEEPSEEK_API_KEY')), bool(os.environ.get('S5_PASSWORD')), bool(os.environ.get('S6_DB_PASSWORD')))"

应输出：`True True True True`

### 3. 建 MySQL 表（一次性）

    python init_db.py

期望输出：`tables: (('daily_quota',), ('qa_log',))`

### 4. 建向量库（一次性）

    python create_db.py

### 5. 启动服务

    python main.py

浏览器打开 <http://127.0.0.1:8000/?k=你设置的S5_PASSWORD>
（`?k=` 会种一个 cookie，之后直接开 `/` 即可；也可以走浏览器弹出的 Basic Auth 框，用户名 `demo`）

## 接口说明

| 方法 | 路径 | 参数 | 说明 |
|---|---|---|---|
| GET | `/` | — | 返回提问页面（`index.html`） |
| GET | `/hello` | — | 连通性测试 |
| GET | `/ask` | `question`、`session_id`（都必填） | 问答（非流式），返回 JSON |
| GET | `/ask_stream` | `question`、`session_id`（都必填） | 问答（SSE 流式），网页用的就是它 |

接口文档：<http://127.0.0.1:8000/docs>（需先过入口口令）

**响应示例**（`/ask`，`200 OK`，`Content-Type: application/json`）

    {
      "answer": "「奇变偶不变，符号看象限」……（出处：## 三、三角函数与解三角形）"
    }

## 项目结构

| 文件 | 作用 |
|---|---|
| `agent.py` | Agent 主体：两个 client（聊天 / 向量）、4 个工具、工具调用循环、缓存、会话记忆、额度闸调用 |
| `main.py` | FastAPI 服务：4 条路由 + 入口口令中间件（cookie / `?k=` / Basic Auth） |
| `index.html` | 提问页面：原生 JS + `EventSource` 收流式；`session_id` 存在 localStorage |
| `quota_db.py` | 额度账本：`used_today()` / `count_one()`，读写 MySQL 的 `daily_quota` / `qa_log` |
| `init_db.py` | 一次性建库建表脚本（幂等） |
| `create_db.py` | 一次性建向量库脚本 |
| `requirements.txt` | 依赖清单 |

## 技术栈

| 类别 | 用了什么 | 用途 |
|---|---|---|
| 语言 | Python 3.10+ | — |
| Web 框架 | FastAPI | 提供 HTTP 接口、自动生成接口文档 |
| 服务器 | Uvicorn | 运行 ASGI 服务 |
| LLM 调用 | OpenAI SDK（兼容模式） | 统一调用 DeepSeek 官方 / 智谱的兼容接口 |
| 聊天模型 | `deepseek-flash`（DeepSeek 官方） | 生成回答、决定调用哪个工具 |
| 向量模型 | `embedding-3`（智谱） | 文本向量化 |
| 向量库 | ChromaDB（PersistentClient，cosine 距离） | 存储与检索笔记片段 |
| 数据库 | MySQL 8 + PyMySQL | 额度与问答记录落库（重启不丢） |
| 前端 | 原生 HTML + JavaScript（`EventSource`） | 提问界面，无框架 |
| 外部工具 | requests（调用 wttr.in 查天气） | Agent 的外部工具之一 |

## 设计说明

1. **切片粒度**：试过 500 字硬切（相似度 0.4175），改按段落（~130 字）后 0.5996 → 切得太长会**语义稀释**，段落切片更优
2. **system 提示词**要求"基于给定材料回答 + 必须标明出处" → 抑制幻觉、答案可追溯
3. **会话隔离**：`session_id` 三层贯通（浏览器 localStorage → 接口 → `agent.py` 的 `_sessions`）→ 多用户互不串味
4. **缓存**：`(session_id, question)` 严格相等才命中，命中即秒回；代价 = 重启清空、无失效机制
5. **额度闸落库**：内存版一重启就归零、等于没闸 → 改存 MySQL：`quota_date DATE NOT NULL UNIQUE` 保证**一天一行**，写入用 `INSERT ... ON DUPLICATE KEY UPDATE used = used + 1`（加数不加行）；**被拦住的那次不记账**
6. **只用参数化占位符**（`%s`），不拼 SQL 字符串 → 从根上防 SQL 注入

## 已知限制 / 待改进

- 单知识库、单资料文件
- 每日额度上限 `3` 写死在 `quota_db.DAILY_LIMIT`
- 缓存不持久（重启清空）、无失效机制
- Markdown / LaTeX 在网页上原样显示，未渲染
- **未做连接池**：每问一次就新建一条 MySQL 连接（并发一高会 `Too many connections`）
- `S5_PASSWORD` 不设置时**入口完全不拦**（fail open）

## 部署

已部署到云服务器（Ubuntu + `systemd` 常驻自启 + 入口口令闸），公网演示地址与口令不放在本仓库里。
