import asyncio
from quota_db import get_pool

SQL = """
DROP TABLE IF EXISTS daily_quota;
CREATE TABLE daily_quota (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_key VARCHAR(64) NOT NULL,
    quota_date DATE NOT NULL,
    used INT NOT NULL DEFAULT 0,
    UNIQUE KEY uk_user_date (user_key, quota_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for stmt in [s.strip() for s in SQL.split(";") if s.strip()]:
                await cur.execute(stmt)
            await conn.commit()
            await cur.execute("SHOW COLUMNS FROM daily_quota")
            print("新表结构：", [r[0] for r in await cur.fetchall()])
    print("额度表已重建，今日计数清零")

asyncio.run(main())