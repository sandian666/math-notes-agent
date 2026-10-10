# ===== S5 / init_db.py =====
# 一次性建库脚本：创建 MySQL 库 + 两张表（daily_quota 额度账本 / qa_log 问答记录）
# 幂等：重复跑不会报错（全是 IF NOT EXISTS），所以想重建直接再跑一次
# 跑法：先配好环境变量 S6_DB_PASSWORD，再 `python init_db.py`
import os
import pymysql

DB_NAME = "s6"          # 库名沿用开发时的命名；quota_db.py 里连的也是这个库

conn = pymysql.connect(
    host=os.environ.get("S6_DB_HOST", "127.0.0.1"),
    port=int(os.environ.get("S6_DB_PORT", "3306")),
    user=os.environ.get("S6_DB_USER", "root"),
    # 读不到就当场报 KeyError（别默默连一个错的库；key/密码一律走环境变量，不写死）
    password=os.environ["S6_DB_PASSWORD"],
    charset="utf8mb4",
)
cur = conn.cursor()

# 库（仓库）不存在就建，再切进去；后面两张表都建在这个库里
cur.execute(f"CREATE DATABASE IF NOT EXISTS {DB_NAME} DEFAULT CHARSET utf8mb4")
cur.execute(f"USE {DB_NAME}")

# 额度账本：一天一行，靠 quota_date 的 UNIQUE 由数据库强制（不靠程序记）
cur.execute("""
CREATE TABLE IF NOT EXISTS daily_quota (
    id INT AUTO_INCREMENT PRIMARY KEY,
    quota_date DATE NOT NULL UNIQUE,
    used INT NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

# 问答记录：只有"答成了"才写一行（被额度挡住的、报错的都不写）
cur.execute("""
CREATE TABLE IF NOT EXISTS qa_log (
    id INT AUTO_INCREMENT PRIMARY KEY,
    question TEXT NOT NULL,
    answer TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

# 验证块：把库里的真实结果打出来（"看起来对"不算证据）
cur.execute("SHOW TABLES")
print("tables:", cur.fetchall())
cur.execute("DESC daily_quota")
print("daily_quota 列:", [r[0] for r in cur.fetchall()])
cur.execute("DESC qa_log")
print("qa_log 列:", [r[0] for r in cur.fetchall()])

cur.close()
conn.close()
print("init_db 完成")
