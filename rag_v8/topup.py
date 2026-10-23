import json, os, re, time
import chromadb
from zhipuai import ZhipuAI

client = ZhipuAI(api_key=os.environ["ZHIPU_API_KEY"])
JSONL = "blocks.jsonl"
CHROMA = r"D:\桌面文件\dsh\S5\chroma_db"
COL = "math_note_v8"

# (学科, 章节, 检测关键词, 考点)
POINTS = [
    ("高数", "函数、极限与连续", "洛必达", "洛必达法则的使用条件与失效情形"),
    ("高数", "函数、极限与连续", "等价无穷小", "常用等价无穷小替换"),
    ("高数", "函数、极限与连续", "泰勒公式", "泰勒公式与麦克劳林展开"),
    ("高数", "一元函数微分学", "拉格朗日中值定理", "拉格朗日中值定理的条件与结论"),
    ("高数", "一元函数微分学", "柯西中值定理", "柯西中值定理"),
    ("高数", "一元函数微分学", "拐点", "拐点与凹凸性的判定"),
    ("高数", "一元函数积分学", "分部积分", "分部积分的应用"),
    ("高数", "一元函数积分学", "换元积分", "定积分换元法与上下限"),
    ("高数", "一元函数积分学", "旋转体", "旋转体体积计算"),
    ("高数", "一元函数积分学", "反常积分", "反常积分的敛散性"),
    ("高数", "多元函数微分学", "隐函数", "隐函数求导"),
    ("高数", "多元函数微分学", "方向导数", "方向导数与梯度"),
    ("高数", "多元函数微分学", "条件极值", "拉格朗日乘数法"),
    ("高数", "二重积分", "极坐标", "二重积分极坐标变换"),
    ("高数", "二重积分", "交换积分次序", "交换积分次序"),
    ("高数", "三重积分与曲线曲面积分", "格林公式", "格林公式及其应用"),
    ("高数", "三重积分与曲线曲面积分", "高斯公式", "高斯公式"),
    ("高数", "无穷级数", "收敛半径", "幂级数收敛半径与收敛域"),
    ("高数", "无穷级数", "比值判别法", "正项级数比值判别法"),
    ("高数", "常微分方程", "一阶线性", "一阶线性微分方程通解公式"),
    ("高数", "常微分方程", "二阶常系数", "二阶常系数非齐次方程特解设法"),
    ("线代", "行列式", "范德蒙", "范德蒙德行列式"),
    ("线代", "矩阵", "伴随矩阵", "伴随矩阵与逆矩阵的关系"),
    ("线代", "矩阵", "初等变换", "初等变换与初等矩阵"),
    ("线代", "向量组的线性相关性", "施密特", "施密特正交化"),
    ("线代", "线性方程组", "基础解系", "基础解系与通解结构"),
    ("线代", "特征值与特征向量", "对角化", "矩阵可对角化的充要条件"),
    ("线代", "特征值与特征向量", "相似矩阵", "相似矩阵的性质"),
    ("线代", "二次型", "正定", "正定二次型的判定"),
    ("线代", "二次型", "合同", "合同变换与惯性定理"),
    ("概率", "随机事件与概率", "条件概率", "条件概率与乘法公式"),
    ("概率", "一维随机变量及其分布", "分布函数", "分布函数的性质"),
    ("概率", "多维随机变量及其分布", "卷积", "二维随机变量函数的分布"),
    ("概率", "多维随机变量及其分布", "独立性", "随机变量的独立性判定"),
    ("概率", "随机变量的数字特征", "协方差", "协方差与相关系数"),
    ("概率", "随机变量的数字特征", "方差的性质", "方差的性质与计算公式"),
    ("概率", "大数定律与中心极限定理", "切比雪夫", "切比雪夫不等式"),
    ("概率", "大数定律与中心极限定理", "中心极限定理", "中心极限定理的应用"),
    ("概率", "参数估计", "最大似然", "最大似然估计"),
    ("概率", "参数估计", "矩估计", "矩估计法"),
    ("概率", "参数估计", "置信区间", "置信区间的求法"),
    ("概率", "假设检验", "两类错误", "假设检验的两类错误"),
]


def escape_bad_backslashes(s):
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


def prompt(subject, chapter, point):
    return f"""学科：{subject}
章节：{chapter}
考点：{point}
写 2 条知识块：
1) 一条"定理"或"概念"，写清该考点的条件、结论（定理必须写全条件）；
2) 一条"例题"或"易错"，给典型小题+关键步骤，或常见错误+正确做法。
每条 150~250 字，中文，公式用 $...$，只写考研考纲内的内容。

只输出 JSON 数组，不要解释、不要代码块：
[{{"type":"定理","difficulty":"中等","text":"..."}}]"""


rows = [json.loads(l) for l in open(JSONL, encoding="utf-8")] if os.path.exists(JSONL) else []
seq = len(rows)
DONE_FILE = "done_points.json"
done_pts = json.load(open(DONE_FILE, encoding="utf-8")) if os.path.exists(DONE_FILE) else []
if not done_pts:
    # 第一次运行：把关键词已命中的记为已完成
    done_pts = [p[3] for p in POINTS if any(p[2] in r["text"] for r in rows)]
missing = [p for p in POINTS if p[3] not in done_pts]
print(f"考点 {len(POINTS)} 个，库里缺 {len(missing)} 个")

start = len(rows)
for subject, chapter, kw, point in missing:
    print(f"补：{subject}/{point}")
    try:
        resp = client.chat.completions.create(
            model="glm-4-flash",
            messages=[{"role": "user", "content": prompt(subject, chapter, point)}],
            temperature=0.7)
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
        items = json.loads(escape_bad_backslashes(raw), strict=False)
    except Exception as e:
        print(f"  失败：{e}")
        time.sleep(3)
        continue
    for it in items:
        seq += 1
        rows.append({"text": it["text"],
                     "metadata": {"学科": subject, "章节": chapter, "类型": it["type"],
                                  "难度": it.get("difficulty", "中等"),
                                  "来源": "AI补漏·2027大纲", "序号": seq,
                                  "切法": "类型切块", "卷种": "数一/数二/数三"}})
    done_pts.append(point)
    json.dump(done_pts, open(DONE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    time.sleep(1)

with open(JSONL, "w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
print("补完，总条数：", len(rows))

new = rows[start:]
if new:
    chroma = chromadb.PersistentClient(path=CHROMA)
    col = chroma.get_or_create_collection(COL, metadata={"hnsw:space": "cosine"})
    for i in range(0, len(new), 32):
        b = new[i:i + 32]
        r = client.embeddings.create(model="embedding-3",
                                     input=[x["text"] for x in b], dimensions=2048)
        col.upsert(ids=[f"v8-{x['metadata']['序号']}" for x in b],
                   documents=[x["text"] for x in b],
                   embeddings=[d.embedding for d in r.data],
                   metadatas=[x["metadata"] for x in b])
        print(f"入库 {i + len(b)}/{len(new)}")
    print("库内条数：", col.count())
else:
    print("没有缺口，不用补")