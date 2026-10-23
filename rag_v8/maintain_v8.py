"""math_note_v8 一次性保养脚本：去重 + 矩阵换行修复 + 短块扩写 + 重新入库。

跑法（S5 目录，venv 已激活，ZHIPU_API_KEY 已设）：
    python rag_v8/maintain_v8.py

代价：约 25 次 glm-4-flash 调用 + 535 条 embedding，几毛钱。
"""
import json
import os
import re
import sys
import time

import chromadb
from zhipuai import ZhipuAI

DB = r"D:\桌面文件\dsh\S5\chroma_db"
COLL = "math_note_v8"
BATCH = 8          # 每次扩写几条
MIN_LEN = 60       # 短于这个字数的（非高中）块要扩写
TARGET = "160~220 字"

key = os.environ.get("ZHIPU_API_KEY") or os.environ.get("ZHIPUAI_API_KEY")
if not key:
    sys.exit("没找到 ZHIPU_API_KEY，先设环境变量")
client = ZhipuAI(api_key=key)


def fix_escape(s):
    """模型返回的 JSON 里，LaTeX 的单反斜杠要补成合法转义。"""
    out, i, n, in_str = [], 0, len(s), False
    while i < n:
        ch = s[i]
        if in_str and ch == "\\":
            nxt = s[i + 1] if i + 1 < n else ""
            if nxt in ("\\", '"'):
                out.append(ch); out.append(nxt); i += 2
            else:
                out.append("\\\\"); i += 1
        else:
            if ch == '"':
                in_str = not in_str
            out.append(ch); i += 1
    return "".join(out)


def load_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(fix_escape(text), strict=False)


# ---------- 读库 ----------
col = chromadb.PersistentClient(path=DB).get_collection(COLL)
raw = col.get(include=["documents", "metadatas"])
items = [{"id": i, "doc": d, "meta": m}
         for i, d, m in zip(raw["ids"], raw["documents"], raw["metadatas"])]
print("原始条数：", len(items))
json.dump(items, open("blocks_all_backup.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print("已备份 -> blocks_all_backup.json")

# ---------- ① 去重 ----------
seen, kept, removed = set(), [], []
for it in items:
    norm = re.sub(r"\s+", "", it["doc"])[:60]
    if norm in seen:
        removed.append(it["id"])
        continue
    seen.add(norm)
    kept.append(it)
print(f"① 去重：{len(items)} -> {len(kept)}（删 {len(removed)}）")

# ---------- ③ 矩阵换行修复 ----------
MAT = re.compile(r"(\\begin\{(?:b|p|v)matrix\})(.*?)(\\end\{(?:b|p|v)matrix\})", re.S)
ROW = re.compile(r"(?<!\\)\\ ")


def fix_matrix(t):
    def repl(m):
        body = ROW.sub(r"\\\\ ", m.group(2))
        return m.group(1) + body + m.group(3)
    return MAT.sub(repl, t)


fixed = 0
for it in kept:
    new = fix_matrix(it["doc"])
    if new != it["doc"]:
        it["doc"] = new
        fixed += 1
print("③ 矩阵修复：", fixed, "条")

# ---------- ② 短块扩写 ----------
SHORT = ("下面每组是考研数学知识块，原文都偏短。请在不改变原意的前提下，把每条扩写到 "
         + TARGET + "：补足适用条件、关键步骤或一个简短例子；中文；公式用 $...$；"
         "不要编造与原意冲突的内容；不要加小标题或序号。\n"
         "输入 JSON：\n{i}\n"
         '只输出 JSON 数组，元素形如 {{"i": 同输入编号, "text": "扩写后的正文"}}，'
         "不要解释、不要代码块。")

shorts = [it for it in kept
          if len(it["doc"]) < MIN_LEN and it["meta"].get("学科") != "高中"]
print("② 待扩写：", len(shorts), "条")


def expand(batch):
    payload = [{"i": n, "章节": it["meta"].get("章节", ""),
                "类型": it["meta"].get("类型", ""), "text": it["doc"]}
               for n, it in enumerate(batch, 1)]
    prompt = SHORT.format(i=json.dumps(payload, ensure_ascii=False))
    for attempt in (1, 2):
        try:
            resp = client.chat.completions.create(
                model="glm-4-flash",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.6)
            data = load_json(resp.choices[0].message.content)
            got = {int(x["i"]): x["text"] for x in data if x.get("text")}
            return got
        except Exception as e:
            print("   扩写失败，重试：", repr(e)[:80])
            time.sleep(3)
    return {}


ok = 0
for s in range(0, len(shorts), BATCH):
    batch = shorts[s:s + BATCH]
    got = expand(batch)
    for n, it in enumerate(batch, 1):
        text = got.get(n)
        if text and len(text) > len(it["doc"]):
            it["doc"] = text
            ok += 1
    print(f"   扩写 {min(s + BATCH, len(shorts))}/{len(shorts)}，成功 {ok}")
    time.sleep(1)
print("② 扩写完成：", ok, "条")

# ---------- 写回 ----------
if removed:
    col.delete(ids=removed)

B = 32
for i in range(0, len(kept), B):
    chunk = kept[i:i + B]
    vec = client.embeddings.create(
        model="embedding-3", input=[x["doc"] for x in chunk], dimensions=2048)
    col.upsert(ids=[x["id"] for x in chunk],
               documents=[x["doc"] for x in chunk],
               embeddings=[d.embedding for d in vec.data],
               metadatas=[x["meta"] for x in chunk])
    print(f"   入库 {i + len(chunk)}/{len(kept)}")

with open("blocks_all.jsonl", "w", encoding="utf-8") as f:
    for x in kept:
        f.write(json.dumps({"text": x["doc"], "metadata": x["meta"]},
                           ensure_ascii=False) + "\n")

print("\n最终库内条数：", col.count())
print("平均字数：", round(sum(len(x["doc"]) for x in kept) / len(kept)))
print("短于 60 的：", sum(1 for x in kept if len(x["doc"]) < MIN_LEN))
