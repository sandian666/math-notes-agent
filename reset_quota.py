# reset_quota.py —— 把【今天】的额度计数清零（只动今天那一行，历史不动）
# 跑法 A（容器）：docker compose exec app python reset_quota.py
# 跑法 B（服务器本机）：set -a; . ~/s5.env; set +a; .venv/bin/python reset_quota.py
#   🔴 必须 set -a：~/s5.env 里是 KEY=value（没有 export），
#      直接 source 只得到 shell 变量，不 export 的话子进程读不到 → KeyError
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

    # 🔴 收尾：先把池关掉，再让事件循环结束。
    #    不关的话，连接对象会拖到解释器退出后才被回收，那时事件循环已经关了，
    #    析构里的 close() 就抛 "RuntimeError: Event loop is closed"（只是噪音，不是功能错）
    pool.close()
    await pool.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())