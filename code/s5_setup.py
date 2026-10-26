# ===== S5 / s5_setup.py =====
"""S5 环境初始化：MySQL 库表 + chroma 向量库，一个入口建完。

为什么合成一个文件：这两步原来是 init_db.py / create_db.py —— 永远是"装环境时一起跑"，
分开只会让人漏跑一个（漏了 chroma：服务能起来、能对话，但检索全空，症状很隐蔽）。

用法（在 S5 目录，用项目 venv 的 python）：
    python code/s5_setup.py              # 两个都建（都幂等，重复跑不会坏）
    python code/s5_setup.py --mysql      # 只建 MySQL 库 + 两张表（秒级，不花钱）
    python code/s5_setup.py --chroma     # 只重建向量库（要调 embedding：慢 + 花钱）

⚠️ --chroma 会先删同名 collection 再重建；**服务运行中别跑**（两个进程同时写 chroma 会写坏索引）。
⚠️ MySQL 那半需要环境变量 S6_DB_PASSWORD（以及可选的 S6_DB_HOST / PORT / USER）。
"""
import argparse
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # = S5 项目根（代码在 code/）
DATA_DIR = os.path.join(BASE_DIR, "data")          # 所有"会变的东西"都进 data/
DOCS_FILE = os.path.join(BASE_DIR, "rag_docs", "高中数学核心知识笔记.md")
CHROMA_DIR = os.path.join(BASE_DIR, "chroma_db")
COLLECTION_FILE = os.path.join(DATA_DIR, "current_collection.txt")

COLLECTION_NAME = "math_note_v8"          # 正式库（agent.py 用的就是这个）
COMPARE_NAME = "math_note_500_V3"         # 对照库（留着证明"语义稀释"那个 A/B 结论）


# ============================================================
# 一、MySQL：建库 + 两张表（daily_quota 额度账本 / qa_log 问答记录）
# ============================================================
def setup_mysql():
    """幂等：全是 IF NOT EXISTS，重复跑不报错，想重建就直接再跑一次。"""
    import pymysql

    DB_NAME = "s6"          # 库名沿用开发时的命名；quota_db.py 里连的也是这个库

    conn = pymysql.connect(
        host=os.environ.get("S6_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("S6_DB_PORT", "3306")),
        user=os.environ.get("S6_DB_USER", "root"),
        # 读不到就当场 KeyError（别默默连一个错的库；key/密码一律走环境变量，不写死）
        password=os.environ["S6_DB_PASSWORD"],
        charset="utf8mb4",
    )
    cur = conn.cursor()

    cur.execute(f"CREATE DATABASE IF NOT EXISTS {DB_NAME} DEFAULT CHARSET utf8mb4")
    cur.execute(f"USE {DB_NAME}")

    # 额度账本：一天一行，靠 quota_date 的 UNIQUE 由数据库强制（不靠程序记）
    cur.execute("""
    CREATE TABLE IF NOT EXISTS daily_quota (
        id INT AUTO_INCREMENT PRIMARY KEY,
        quota_date DATE NOT NULL UNIQUE,
        used INT NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 问答记录：只有"答成了"才写一行（被额度挡住的、报错的都不写）
    cur.execute("""
    CREATE TABLE IF NOT EXISTS qa_log (
        id INT AUTO_INCREMENT PRIMARY KEY,
        question TEXT NOT NULL,
        answer TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 验证块：把库里的真实结果打出来（"看起来对"不算证据）
    cur.execute("SHOW TABLES")
    print("tables:", cur.fetchall())
    cur.execute("DESC daily_quota")
    print("daily_quota 列:", [r[0] for r in cur.fetchall()])
    cur.execute("DESC qa_log")
    print("qa_log 列:", [r[0] for r in cur.fetchall()])

    cur.close()
    conn.close()
    print("✅ MySQL 就绪")


# ============================================================
# 二、chroma：rag_docs 的资料 → 切块 → embedding → 存进 chroma_db
# ============================================================
def _split_text(text, chunk_size=500, overlap=50):
    """切法 1：按字数硬切（对照库用，为的是复现"语义稀释"的 A/B 结论）。"""
    chunks = []
    for i in range(0, len(text), chunk_size - overlap):
        chunks.append(text[i:i + chunk_size])
    return chunks


def _split_by_paragraph(text):
    """切法 2：按段落切（正式库用；一段一个知识点，语义不被切断）。"""
    chunk_list = []
    for part in text.split("\n\n"):
        part = part.strip()
        if part:
            chunk_list.append(part)
    return chunk_list


def _annotate_sections(chunks):
    """给每一块标注"它属于哪一小节"——检索时好把出处一起返回。"""
    sections = []
    current = "前言"
    for c in chunks:
        if c.startswith("## "):        # 遇到小节标题就更新"当前小节"
            current = c
        sections.append(current)       # 每块都记下"我属于哪一节"
    return sections


def _build_index(chunks, sections, collection_name, chunk_method, embed_client):
    import chromadb

    # 分批 embedding（智谱单次有条数上限），每批 50 条
    embeddings = []
    for i in range(0, len(chunks), 50):
        batch = chunks[i:i + 50]
        resp = embed_client.embeddings.create(model="embedding-3", input=batch)
        embeddings.extend([d.embedding for d in resp.data])

    chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)

    # 【幂等】先删掉同名旧库（没有也无所谓，所以用 try 包住）
    try:
        chroma_client.delete_collection(collection_name)
        print(f"已删除旧库：{collection_name}")
    except Exception:
        pass

    col = chroma_client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},
    )
    col.add(
        ids=[f"chunk_{i}" for i in range(len(chunks))],
        documents=chunks,
        embeddings=embeddings,
        metadatas=[{"序号": i, "来源": "高中数学笔记", "切法": chunk_method, "小节": sections[i]}
                   for i in range(len(chunks))],
    )
    print(f"[{chunk_method}] 库里有 {col.count()} 条")
    return col


def setup_chroma():
    from openai import OpenAI

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DOCS_FILE, "r", encoding="utf-8") as f:
        text = f.read()

    embed_client = OpenAI(api_key=os.environ.get("ZHIPU_API_KEY"),
                          base_url="https://open.bigmodel.cn/api/paas/v4/")

    # 对照库：字数硬切 500
    chunks_a = _split_text(text)
    _build_index(chunks_a, _annotate_sections(chunks_a), COMPARE_NAME, "字数硬切500", embed_client)

    # 正式库：按段落切 + 过滤掉标题/分隔线，再同步过滤 section，保持一一对应
    chunks_b = _split_by_paragraph(text)
    sections_b = _annotate_sections(chunks_b)
    keep = [(c, s) for c, s in zip(chunks_b, sections_b)
            if not c.startswith("##") and c != "---" and not c.startswith("# ")]
    chunks_b = [c for c, s in keep]
    sections_b = [s for c, s in keep]
    _build_index(chunks_b, sections_b, COLLECTION_NAME, "按段落", embed_client)

    # 把"当前正式库名"写进 data/current_collection.txt（agent.py 启动时读它）
    with open(COLLECTION_FILE, "w", encoding="utf-8") as f:
        f.write(COLLECTION_NAME)
    print(f"✅ 当前库名已写入 {COLLECTION_FILE}：{COLLECTION_NAME}")


# ============================================================
def main():
    ap = argparse.ArgumentParser(description="S5 建库：MySQL 表 / chroma 向量库")
    ap.add_argument("--mysql", action="store_true", help="只建 MySQL 库表（不花钱）")
    ap.add_argument("--chroma", action="store_true", help="只重建 chroma 向量库（花钱）")
    args = ap.parse_args()

    # 不给参数 = 两个都建
    do_mysql = args.mysql or not (args.mysql or args.chroma)
    do_chroma = args.chroma or not (args.mysql or args.chroma)

    if do_mysql:
        setup_mysql()
    if do_chroma:
        setup_chroma()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # 中文别按 GBK 崩
    except Exception:
        pass
    main()
