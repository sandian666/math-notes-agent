import json, os, re, time
import chromadb
from zhipuai import ZhipuAI

KEY = os.environ.get("ZHIPU_API_KEY") or os.environ.get("ZHIPUAI_API_KEY")
client = ZhipuAI(api_key=KEY)
JSONL = "blocks.jsonl"
CHROMA = r"D:\桌面文件\dsh\S5\chroma_db"
COL = "math_note_v8"

# (学科, 章节, 适用卷种)
CHAPTERS = [
    ("高数", "函数、极限与连续",           "数一/数二/数三"),
    ("高数", "一元函数微分学",             "数一/数二/数三"),
    ("高数", "一元函数积分学",             "数一/数二/数三"),
    ("高数", "向量代数与空间解析几何",      "数一"),
    ("高数", "多元函数微分学",             "数一/数二/数三"),
    ("高数", "二重积分",                   "数一/数二/数三"),
    ("高数", "三重积分与曲线曲面积分",      "数一"),
    ("高数", "无穷级数",                   "数一/数三"),
    ("高数", "常微分方程",                 "数一/数二/数三"),
    ("线代", "行列式",                     "数一/数二/数三"),
    ("线代", "矩阵",                       "数一/数二/数三"),
    ("线代", "向量组的线性相关性",          "数一/数二/数三"),
    ("线代", "线性方程组",                 "数一/数二/数三"),
    ("线代", "特征值与特征向量",            "数一/数二/数三"),
    ("线代", "二次型",                     "数一/数二/数三"),
    ("概率", "随机事件与概率",              "数一/数三"),
    ("概率", "一维随机变量及其分布",        "数一/数三"),
    ("概率", "多维随机变量及其分布",        "数一/数三"),
    ("概率", "随机变量的数字特征",          "数一/数三"),
    ("概率", "大数定律与中心极限定理",      "数一/数三"),
    ("概率", "参数估计",                   "数一/数三"),
    ("概率", "假设检验",                   "数一"),
]
QUOTA = {"概念": 5, "定理": 4, "例题": 7, "易错": 4}   # 每章 20 条


def escape_bad_backslashes(s):
    """除 \\ 和 \" 外，所有反斜杠都当 LaTeX，补成合法转义"""
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


def build_prompt(subject, chapter, papers):
    types = "、".join(f"{k}{v}条" for k, v in QUOTA.items())
    return f"""学科：{subject}
章节：{chapter}
适用卷种：考研数学{papers}
生成 20 条知识块，类型配额：{types}。

要求：
1. 每条 150~250 字，中文，公式用 $...$。
2. 定理必须写全条件（如"在闭区间上连续"不能省）。
3. 严格限定在"考研数学{papers}"的考纲范围内，不考的内容不写。
4. 概念块讲"是什么"；定理块写条件+结论；例题块给典型小题+关键步骤；
   易错块写常见错误+正确做法。
5. 条与条之间不重复。

只输出 JSON 数组，不要解释、不要代码块：
[{{"type":"概念","difficulty":"基础","text":"..."}}]"""


def done():
    if not os.path.exists(JSONL):
        return set()
    return {(json.loads(l)["metadata"]["学科"], json.loads(l)["metadata"]["章节"])
            for l in open(JSONL, encoding="utf-8")}


def gen():
    seq = sum(1 for _ in open(JSONL, encoding="utf-8")) if os.path.exists(JSONL) else 0
    for subject, chapter, papers in CHAPTERS:
        if (subject, chapter) in done():
            print(f"跳过（已生成）：{subject}/{chapter}")
            continue
        for attempt in (1, 2):
            try:
                resp = client.chat.completions.create(
                    model="glm-4-flash",
                    messages=[{"role": "user",
                               "content": build_prompt(subject, chapter, papers)}],
                    temperature=0.7)
                raw = resp.choices[0].message.content.strip()
                raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
                raw = escape_bad_backslashes(raw)
                items = json.loads(raw, strict=False)
                break
            except Exception as e:
                print(f"  第 {attempt} 次失败：{e}")
                time.sleep(3)
        else:
            print(f"放弃本章：{subject}/{chapter}")
            continue

        with open(JSONL, "a", encoding="utf-8") as f:
            for it in items[:20]:
                seq += 1
                f.write(json.dumps({
                    "text": it["text"],
                    "metadata": {"学科": subject, "章节": chapter, "类型": it["type"],
                                 "难度": it.get("difficulty", "中等"),
                                 "来源": "AI整理·2027大纲", "序号": seq,
                                 "切法": "类型切块", "卷种": papers}},
                    ensure_ascii=False) + "\n")
        print(f"{subject}/{chapter}: +{len(items)}，累计 {seq}")
        time.sleep(1)


def embed():
    rows = [json.loads(l) for l in open(JSONL, encoding="utf-8")]
    print("待入库：", len(rows), "条")
    chroma = chromadb.PersistentClient(path=CHROMA)
    col = chroma.get_or_create_collection(COL, metadata={"hnsw:space": "cosine"})
    for i in range(0, len(rows), 32):
        b = rows[i:i + 32]
        r = client.embeddings.create(model="embedding-3",
                                     input=[x["text"] for x in b], dimensions=2048)
        col.upsert(ids=[f"v8-{x['metadata']['序号']}" for x in b],
                   documents=[x["text"] for x in b],
                   embeddings=[d.embedding for d in r.data],
                   metadatas=[x["metadata"] for x in b])
        print(f"{i + len(b)}/{len(rows)}")
    return col


if __name__ == "__main__":
    gen()
    col = embed()
    print("库内条数：", col.count())
    for q in ["洛必达法则的使用条件", "矩阵可对角化的充要条件", "全概率公式和贝叶斯公式的区别"]:
        v = client.embeddings.create(model="embedding-3", input=[q], dimensions=2048).data[0].embedding
        res = col.query(query_embeddings=[v], n_results=3)
        print("\nQ:", q)
        for d, m in zip(res["documents"][0], res["metadatas"][0]):
            print("  -", m["学科"], "|", m["章节"], "|", m["类型"], "|", d[:40])