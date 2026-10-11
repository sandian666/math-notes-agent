# reset_quota.py —— 把【今天】的额度计数清零（只动今天那一行，历史不动）
# 跑法：docker compose exec app python reset_quota.py
import asyncio

import quota_db


async def main():
    today = quota_db.today_str()
    pool = await quota_db.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT used FROM daily_quota WHERE quota_date = %s", (today,)
            )
            row = await cur.fetchone()
            before = row[0] if row else 0
            await cur.execute(
                "UPDATE daily_quota SET used = 0 WHERE quota_date = %s", (today,)
            )
        await conn.commit()
    print(f"今日({today}) 清零前 = {before} → 清零后 = 0（上限 {quota_db.DAILY_LIMIT}）")


if __name__ == "__main__":
    asyncio.run(main())