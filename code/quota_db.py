# ===== S5 / quota_db.py =====
# 额度闸的"账本"：把"今天答了几次 + 答过什么"从内存搬进 MySQL(s6 库)
import os,asyncio,aiomysql
from datetime import date

DB = {
    "host": os.environ.get("S6_DB_HOST", "127.0.0.1"),
    "port": 3306,
    "user": "root",
    "password": os.environ["S6_DB_PASSWORD"],
    "db": "s6",              # ← aiomysql 叫 db（你原来写的 database 是 PooledDB 的叫法）
    "charset": "utf8mb4",
}

DAILY_LIMIT = 3          # 每人每天最多答 3 次

_pool = None             # 全局唯一的池子

async def get_pool():
    """第一次要用时才建池 —— create_pool 是 async 的，不能写在模块顶层"""
    global _pool
    if _pool is None:
        _pool = await aiomysql.create_pool(minsize=1, maxsize=5, **DB)
    return _pool

def today_str():
    return date.today().strftime("%Y-%m-%d")

async def used_today(user_key: str = ""):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT used FROM daily_quota WHERE user_key = %s AND quota_date = %s",
                (user_key, today_str()),
            )
            row = await cur.fetchone()
            return row[0] if row else 0

async def count_one(user_key: str, question, answer):
    """记一次账：日报数 +1 + 写 qa_log（两条 INSERT 一次 commit）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO daily_quota (user_key, quota_date, used) VALUES (%s, %s, 1) "
                    "ON DUPLICATE KEY UPDATE used = used + 1",
                    (user_key, today_str()),
                )
                await cur.execute(
                    "INSERT INTO qa_log (question, answer) VALUES (%s, %s)",
                    (question, answer),
                )
            await conn.commit()                        # 一次 commit 两条一起落盘
        except Exception:  # 如果中间出错，先回滚再把错误抛出去
            await conn.rollback()
            raise

# ===== 自测 + 验库（单跑本文件时用）=====
async def _selftest():
    print("今日已用 =", await used_today(""), "/", DAILY_LIMIT)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM daily_quota")
            print("daily_quota:", await cur.fetchall())
            await cur.execute("SELECT id, question, created_at FROM qa_log")
            print("qa_log:", await cur.fetchall())
    pool.close()
    await pool.wait_closed()

if __name__ == "__main__":
    asyncio.run(_selftest())