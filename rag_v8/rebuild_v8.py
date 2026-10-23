"""重建 math_note_v8 的向量索引（不重新调模型，不花钱）。

起因：索引目录里只剩 index_metadata.pickle，data_level0.bin / header.bin 没了，
      查询时报"向量索引加载失败"。向量本身还在 chroma.sqlite3 里，直接搬过去即可。

跑法：先停掉 main.py，再执行
    python rag_v8/rebuild_v8.py
"""
import sys

import chromadb

DB = r"D:\桌面文件\dsh\S5\chroma_db"
COLL = "math_note_v8"

client = chromadb.PersistentClient(path=DB)

try:
    old = client.get_collection(COLL)
    data = old.get(include=["documents", "metadatas", "embeddings"])
except Exception as e:
    sys.exit(f"连旧索引都读不出来：{e!r}\n改用 API 重新 embedding 的方案，告诉我")

ids = data["ids"]
docs = data["documents"]
metas = data["metadatas"]
embs = [[float(x) for x in v] for v in data["embeddings"]]
print("读出：", len(ids), "条，向量维度", len(embs[0]))

client.delete_collection(COLL)
col = client.create_collection(COLL, metadata={"hnsw:space": "cosine"})

B = 64
for i in range(0, len(ids), B):
    col.add(ids=ids[i:i + B], documents=docs[i:i + B],
            metadatas=metas[i:i + B], embeddings=embs[i:i + B])
    print(f"  写入 {min(i + B, len(ids))}/{len(ids)}")

print("重建完成，条数：", col.count())

res = col.query(query_embeddings=[embs[0]], n_results=1)
hit = res["ids"][0][0] == ids[0]
print("自检：", "通过" if hit else "没命中自己", "| 章节：", res["metadatas"][0][0].get("章节"))
