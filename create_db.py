# ===== S5 / create_db.py =====
# 建库脚本：把 rag_docs 里的资料 → 切块 → embedding → 存进 chroma_db
# 【幂等】：重复运行结果一致（开头先删旧库再重建）
import os
import chromadb
from openai import OpenAI

# ===== 路径：全部从"本文件所在目录"派生（这样从哪启动都对）=====
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_FILE = os.path.join(BASE_DIR, "rag_docs", "高中数学核心知识笔记.md")
CHROMA_DIR = os.path.join(BASE_DIR, "chroma_db")
COLLECTION_FILE = os.path.join(BASE_DIR, "current_collection.txt")

COLLECTION_NAME = "math_note_v8"         # 正式库（agent.py 用的就是这个）
COMPARE_NAME = "math_note_500_V3"         # 对照库（留着证明"语义稀释"那个 A/B 结论）

# ===== 读资料 =====
with open(DOCS_FILE, "r", encoding="utf-8") as f:
    text = f.read()


# ===== 切法 1：按字数硬切（对照用）=====
def split_text(text, chunk_size=500, overlap=50):
    chunks = []
    for i in range(0, len(text), chunk_size - overlap):
        chunks.append(text[i:i + chunk_size])
    return chunks


# ===== 切法 2：按段落切（正式用）=====
def split_by_paragraph(text):
    chunk_list = []
    for part in text.split("\n\n"):
        part = part.strip()
        if part:
            chunk_list.append(part)
    return chunk_list


# ===== 给每一块标注"它属于哪一小节" =====
def annotate_sections(chunks):
    sections = []
    current = "前言"
    for c in chunks:
        if c.startswith("## "):        # 遇到小节标题就更新"当前小节"
            current = c
        sections.append(current)       # 每块都记下"我属于哪一节"
    return sections


# ===== 建库：embed + 存进 chroma =====
def build_index(chunks, sections, collection_name, chunk_method):
    embed_client = OpenAI(api_key=os.environ.get("ZHIPU_API_KEY"),
                          base_url="https://open.bigmodel.cn/api/paas/v4/")

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


# ===== 主流程 =====
if __name__ == "__main__":
    # 切法 1：字数硬切（对照库）
    chunks_a = split_text(text, chunk_size=500, overlap=50)
    sections_a = annotate_sections(chunks_a)
    build_index(chunks_a, sections_a, COMPARE_NAME, "字数硬切500")

    # 切法 2：按段落切 + 过滤（正式库）
    chunks_b = split_by_paragraph(text)
    sections_b = annotate_sections(chunks_b)
    keep = [(c, s) for c, s in zip(chunks_b, sections_b)
            if not c.startswith("##") and c != "---" and not c.startswith("# ")]
    chunks_b = [c for c, s in keep]
    sections_b = [s for c, s in keep]      # ← 同步过滤，保持一一对应
    build_index(chunks_b, sections_b, COLLECTION_NAME, "按段落")

    # 把"当前正式库名"写进文件（agent.py 会读它）
    with open(COLLECTION_FILE, "w", encoding="utf-8") as f:
        f.write(COLLECTION_NAME)
    print(f"当前库名已写入 {COLLECTION_FILE}：{COLLECTION_NAME}")