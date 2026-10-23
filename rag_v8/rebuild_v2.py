"""v8 索引已损坏、向量也读不出来 → 从 blocks_all.jsonl 重建一个新集合 math_note_v9。

跑法（先停掉 main.py，否则又会写坏）：
    python rag_v8/rebuild_v2.py

代价：529 次 embedding，几毛钱，约 2~4 分钟。
"""
import json
import os
import sys
import time

import chromadb
from zhipuai import ZhipuAI

DB = r"D:\桌面文件\dsh\S5\chroma_db"
SRC = r"D:\桌面文件\dsh\S5\blocks_all.jsonl"
NEW = "math_note_v9"
POINTER = r"D:\桌面文件\dsh\S5\current_collection.txt"

key = os.environ.get("ZHIPU_API_KEY") or os.environ.get("ZHIPUAI_API_KEY")
if not key:
    sys.exit("没找到 ZHIPU_API_KEY")

cli = ZhipuAI(api_key=key)
rows = [json.loads(l) for l in open(SRC, encoding="utf-8")]
print("待重建：", len(rows), "条")

c = chromadb.PersistentClient(path=DB)
try:
    c.delete_collection(NEW)
    print("清掉旧的", NEW)
except Exception:
    pass
col = c.create_collection(NEW, metadata={"hnsw:space": "cosine"})

B = 32
for i in range(0, len(rows), B):
    b = rows[i:i + B]
    r = cli.embeddings.create(model="embedding-3",
                              input=[x["text"] for x in b], dimensions=2048)
    col.add(ids=[f"v9-{x['metadata'].get('序号', 0)}-{i + j}" for j, x in enumerate(b)],
            documents=[x["text"] for x in b],
            embeddings=[d.embedding for d in r.data],
            metadatas=[x["metadata"] for x in b])
    print(f"  写入 {min(i + B, len(rows))}/{len(rows)}")
    time.sleep(0.2)

with open(POINTER, "w", encoding="utf-8") as f:
    f.write(NEW)
print("指针已切到：", NEW)
print("库内条数：", col.count())

q = cli.embeddings.create(model="embedding-3", dimensions=2048,
                          input=["二重积分怎么交换积分次序"]).data[0].embedding
res = col.query(query_embeddings=[q], n_results=3)
print("自检命中章节：", [m.get("章节") for m in res["metadatas"][0]])
