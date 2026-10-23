import os
import chromadb
from zhipuai import ZhipuAI

client = ZhipuAI(api_key=os.environ["ZHIPU_API_KEY"])
db = chromadb.PersistentClient(path=r"D:\桌面文件\dsh\S5\chroma_db")
old = db.get_collection("math_note_V7")
new = db.get_collection("math_note_v8")

data = old.get(include=["documents", "metadatas"])
rows = []
for doc, meta in zip(data["documents"], data["metadatas"]):
    chapter = (meta.get("小节") or "高中").lstrip("# ").strip() or "高中"
    rows.append((doc, chapter))
print("从老库取到", len(rows), "条")

vecs = []
for i in range(0, len(rows), 32):
    b = rows[i:i + 32]
    r = client.embeddings.create(model="embedding-3",
                                 input=[d for d, _ in b], dimensions=2048)
    vecs += [x.embedding for x in r.data]

new.upsert(
    ids=[f"hs-{i}" for i in range(1, len(rows) + 1)],
    documents=[d for d, _ in rows],
    embeddings=vecs,
    metadatas=[{"学科": "高中", "章节": c, "类型": "概念", "难度": "基础",
                "来源": "高中数学笔记", "序号": 1000 + i, "切法": "段落切块",
                "卷种": "高中"} for i, (_, c) in enumerate(rows, 1)],
)
print("库内条数：", new.count())