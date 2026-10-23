import asyncio
import time
from agent import graph_answer

CASES = [
    ("洛必达法则什么时候不能用？", ["0/0", "∞/∞"]),
    ("三重积分什么时候用柱坐标？", ["柱"]),
    ("一阶线性微分方程的通解公式是什么？", ["积分因子"]),
    ("幂级数的收敛半径怎么求？", ["比值", "根值"]),
    ("二重积分怎么交换积分次序？", ["区域"]),
    ("格林公式成立的条件是什么？", ["单连通", "偏导"]),
    ("矩阵可对角化的充要条件是什么？", ["线性无关", "重数"]),
    ("正定二次型怎么判断？", ["顺序主子式", "特征值"]),
    ("伴随矩阵和逆矩阵是什么关系？", ["A*", "|A|"]),
    ("基础解系怎么求？", ["自由变量", "秩"]),
    ("全概率公式和贝叶斯公式有什么区别？", ["完备", "逆"]),
    ("最大似然估计的步骤是什么？", ["似然函数", "求导"]),
    ("协方差和相关系数是什么关系？", ["方差", "标准差"]),
    ("切比雪夫不等式是什么？", ["ε", "方差"]),
    ("假设检验的两类错误是什么？", ["弃真", "取伪"]),
]

async def main():
    ok = 0
    for i, (q, keys) in enumerate(CASES, 1):
        t0 = time.time()
        try:
            ans = await graph_answer(f"sc-{i}", q)
        except Exception as e:
            print(f"[{i}] ✗ 调用失败：{repr(e)}")
            continue
        hit = any(k in ans for k in keys)
        ok += hit
        print(f"[{i}] {'✓' if hit else '✗'} {q}  ({time.time()-t0:.1f}s, {len(ans)}字)")
        print("     ", ans[:100].replace("\n", " "))
    print(f"\n关键词命中：{ok}/{len(CASES)}")

asyncio.run(main())