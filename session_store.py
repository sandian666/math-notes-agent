"""会话历史的落脚点：把消息列表存进 SQLite，重启服务也不丢。

对照：
  - json 文件    -> 只能整个读、整个写，想改一条得重写全文件
  - MySQL(S6)   -> 要装、要连服务器、要 commit
  - SQLite(这里) -> Python 自带，整个库就是一个 .db 文件，SQL 语法跟 MySQL 几乎一样
"""
import json
import sqlite3
from pathlib import Path

from langchain_core.messages import messages_from_dict, messages_to_dict

# 跟着本文件走，所以你从哪个目录启动服务都能找到这个库文件
DB_PATH = Path(__file__).with_name("sessions.db")


def _conn():
    """连上库，顺手把表建好（IF NOT EXISTS = 不存在才建，重复跑也没事）。"""
    conn = sqlite3.connect(DB_PATH)                 # 对照 S6 的 pymysql.connect
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT, -- 每条记录的编号
            session_id TEXT    NOT NULL,                  -- 属于哪个会话
            seq        INTEGER NOT NULL,                  -- 这条消息在会话中的顺序）
            payload    TEXT    NOT NULL,                  -- 消息内容，保存成 JSON 文本
            created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime'))  -- 保存时间
        )
        """
    )
    # 建索引，作用是让：按照 session_id 查询，并按照 seq 排序变得更快。
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON session_messages(session_id, seq)")
    return conn


def load(session_id):
    """把这个会话的历史捞出来，按顺序还原成消息对象列表。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT payload FROM session_messages WHERE session_id = ? ORDER BY seq",
            (session_id,),        # 找到指定 session_id 的消息按照 seq 顺序排列全部读取出来
        ).fetchall()
    finally:
        conn.close()                                # 用完就关，绝不长期占着（也避免多线程共用连接）

    if not rows:
        return []                                   # 第一次来的会话 = 空历史，跟以前一样
    raw = [json.loads(r[0]) for r in rows]          # 文本 -> 字典
    return messages_from_dict(raw)                  # 字典 -> LangChain 消息对象


def save(session_id, messages):
    """整段覆盖式保存：先删这个会话的旧行，再按顺序整份写回。"""
    raw = messages_to_dict(messages)                # 把 LangChain 消息对象转换成普通字典。
    conn = _conn()
    try:
        conn.execute("DELETE FROM session_messages WHERE session_id = ?", (session_id,)) # 删除这个会话原来的历史
        conn.executemany(   #接着重新插入全部消息
            "INSERT INTO session_messages (session_id, seq, payload) VALUES (?, ?, ?)",
            # default=str 是兜底：万一条目里有 JSON 存不了的值，转成字符串，别让整轮对话写库失败
            [(session_id, i, json.dumps(m, ensure_ascii=False, default=str)) for i, m in enumerate(raw)],
        )
        conn.commit()
    finally:
        conn.close()
    return len(raw)                                 # 返回存了几条，方便打印日志


def sessions():
    """列出库里有历史的 session 和条数 —— 专门用来验"历史真的落库了"。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT session_id, COUNT(*) FROM session_messages "
            "GROUP BY session_id ORDER BY session_id"
        ).fetchall()
    finally:
        conn.close()
    return rows





