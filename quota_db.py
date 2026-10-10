# ===== S5 / quota_db.py =====
# 额度闸的"账本"：把"今天答了几次 + 答过什么"从内存搬进 MySQL(s6 库)
# 角色关系：pymysql=库；_connect()=每次开门拿出连接；cur=干活的手；commit()=落盘
import pymysql,os
from datetime import date
from dbutils.pooled_db import PooledDB

# 连接参数集中一处，池子拿它反复建连接，把字典中的键值对拆开，作为函数参数传入
DB = {
    "host": os.environ.get("S6_DB_HOST", "127.0.0.1"),
    "port": 3306,
    "user": "root",
    "password": os.environ["S6_DB_PASSWORD"],
    "database": "s6",
    "charset": "utf8mb4",
}

# 池子：最多 5 条、最小养 1 条；blocking=True = 借空时「等」
POOL = PooledDB(creator=pymysql, maxconnections=5, mincached=1, blocking=True, **DB)

DAILY_LIMIT = 3          # 每天最多答 3 次  ← 额度只在这一个地方改

def today_str():
    return date.today().strftime("%Y-%m-%d")   # MySQL 的 DATE 要 "2026-09-30" 这种字符串

def _connect():
    return POOL.connection()

def used_today():
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT used FROM daily_quota WHERE quota_date = %s", (today_str(),))
            row = cur.fetchone()      # 今天还没答过 → 没有这一行 → row 是 None
            return row[0] if row else 0     # 空就按 0 算
    finally:
        conn.close()

def count_one(question, answer):
    """记一次账：日报数 +1（两条 INSERT 一次 commit）"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            # 今天有行就 used+1；没行就插一行 used=1（靠 quota_date 的 UNIQUE）
            cur.execute(
                "INSERT INTO daily_quota (quota_date, used) VALUES (%s, 1) "
                "ON DUPLICATE KEY UPDATE used = used + 1",
                (today_str(),),
            )
            cur.execute(
                "INSERT INTO qa_log (question, answer) VALUES (%s, %s)",
                (question, answer),
            )
        conn.commit()               # 一次 commit 把两条 INSERT 一起落盘
    finally:
        conn.close()

# ===== 自测 + 验库（单跑本文件时用） =====
if __name__ == "__main__":
    print("今日已用 =", used_today(), "/", DAILY_LIMIT)
    conn = _connect()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM daily_quota")
        print("daily_quota:", cur.fetchall())
        cur.execute("SELECT id, question, created_at FROM qa_log")
        print("qa_log:", cur.fetchall())
    conn.close()