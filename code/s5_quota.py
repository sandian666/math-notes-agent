# ===== S5 / s5_quota.py =====
"""S5 额度账本维护：清零今天 / 重建表结构。原来拆成 reset_quota.py + fix_quota.py 两个文件。

用法（在 S5 目录；容器里跑就把 `python` 换成 `docker compose exec app python`）：
    python code/s5_quota.py reset     # 只把【今天】那行的 used 清零，历史行不动
    python code/s5_quota.py fix       # 删表重建：daily_quota 改成"按 user_key + 日期"的多用户结构

⚠️ fix 会 DROP TABLE，历史额度记录全没；只在改表结构时用一次。
⚠️ 本机跑要把环境变量带进来：set -a; . ~/s5.env; set +a
   （🔴 必须 set -a：s5.env 里是 KEY=value 没有 export，不 export 子进程读不到 → KeyError）
"""
import asyncio
import sys


async def _with_pool(fn):
    """统一开池 / 关池：不关池会在解释器退出时抛 'Event loop is closed'（噪音，不是功能错）。"""
    import quota_db                         # 延迟 import：只打帮助时不要求 S6_DB_PASSWORD
    pool = await quota_db.get_pool()
    try:
        async with pool.acquire() as conn:
            return await fn(conn)
    finally:
        pool.close()
        await pool.wait_closed()


async def cmd_reset():
    import quota_db
    today = quota_db.today_str()

    async def run(conn):
        async with conn.cursor() as cur:
            await cur.execute("SELECT used FROM daily_quota WHERE quota_date = %s", (today,))
            row = await cur.fetchone()
            before = row[0] if row else 0
            await cur.execute("UPDATE daily_quota SET used = 0 WHERE quota_date = %s", (today,))
        await conn.commit()
        return before

    before = await _with_pool(run)
    print(f"今日({today}) 清零前 = {before} → 清零后 = 0（上限 {quota_db.DAILY_LIMIT}）")


# 多用户版表结构：额度按 (user_key, 日期) 一行，不再是"一天一行"
_FIX_SQL = """
DROP TABLE IF EXISTS daily_quota;
CREATE TABLE daily_quota (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_key VARCHAR(64) NOT NULL,
    quota_date DATE NOT NULL,
    used INT NOT NULL DEFAULT 0,
    UNIQUE KEY uk_user_date (user_key, quota_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


async def cmd_fix():
    async def run(conn):
        async with conn.cursor() as cur:
            for stmt in [s.strip() for s in _FIX_SQL.split(";") if s.strip()]:
                await cur.execute(stmt)
            await conn.commit()
            await cur.execute("SHOW COLUMNS FROM daily_quota")
            return [r[0] for r in await cur.fetchall()]

    cols = await _with_pool(run)
    print("新表结构：", cols)
    print("额度表已重建，今日计数清零")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "reset":
        asyncio.run(cmd_reset())
    elif cmd == "fix":
        asyncio.run(cmd_fix())
    else:
        print(__doc__)
        print("子命令：reset（今天清零）| fix（重建配额表）")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
