"""SQLite 持久层。

单连接 + 进程内可重入互斥锁：所有状态机迁移在一个 BEGIN IMMEDIATE
事务内完成，配合 ``version`` 乐观版本号，使重复/并发提交幂等。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

log = logging.getLogger("fair_draw.db")

DB_PATH = os.environ.get(
    "DRAW_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "draw.db"),
)

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    members_json    TEXT NOT NULL,   -- 不可变的固定成员顺序
    prizes_json     TEXT NOT NULL,   -- 不可变奖项顺序
    commit_deadline TEXT NOT NULL,   -- 规范化 UTC ISO8601（秒精度, 以 Z 结尾）
    reveal_deadline TEXT NOT NULL,
    status          TEXT NOT NULL,   -- committing | revealing | settled | voided
    version         INTEGER NOT NULL DEFAULT 1,
    host_token_hash TEXT NOT NULL,
    void_reason     TEXT,
    result_json     TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    code         TEXT PRIMARY KEY,   -- 一次性邀请码（高熵随机，本身即能力凭证）
    round_id     TEXT NOT NULL REFERENCES rounds(id),
    member_index INTEGER NOT NULL,
    used         INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    UNIQUE(round_id, member_index)
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT PRIMARY KEY,
    round_id     TEXT NOT NULL REFERENCES rounds(id),
    member_index INTEGER NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commitments (
    round_id     TEXT NOT NULL REFERENCES rounds(id),
    member_index INTEGER NOT NULL,
    commitment   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (round_id, member_index)
);

CREATE TABLE IF NOT EXISTS reveals (
    round_id     TEXT NOT NULL REFERENCES rounds(id),
    member_index INTEGER NOT NULL,
    secret       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (round_id, member_index)
);

-- events.seq 为「轮次内」序号，从 1 连续递增；不同轮次各自从 1 开始。
-- 复合主键 (round_id, seq) 保证同一轮内序号唯一、且插入分配天然原子；
-- WITHOUT ROWID 表的主键聚簇按 (round_id, seq) 排序，快照按轮读取顺序无跳号。
CREATE TABLE IF NOT EXISTS events (
    round_id     TEXT NOT NULL REFERENCES rounds(id),
    seq          INTEGER NOT NULL,
    type         TEXT NOT NULL,       -- created/joined/committed/revealed/stage/settled/voided
    member_index INTEGER,
    version      INTEGER,
    payload_json TEXT,
    at           TEXT NOT NULL,
    PRIMARY KEY (round_id, seq)
) WITHOUT ROWID;
"""

# 旧版 schema 特征：seq 是全局 AUTOINCREMENT 主键，多轮次共用同一计数器。


def _migrate_per_round_seq(conn: sqlite3.Connection) -> bool:
    """把全局 AUTOINCREMENT 的旧 events 迁移为每轮独立编号。

    * 既有事件按全局 seq 升序（即插入先后）读出，在 Python 侧按轮次重新
      编号为 1..N 后写入新表——保留原有时间（at）与轮次内先后顺序，
      不依赖较新版本 SQLite 的窗口函数；
    * 迁移在单个事务内完成；幂等（已是新 schema 时直接跳过）。

    返回是否执行了迁移。
    """
    ddl_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'"
    ).fetchone()
    if ddl_row is None:
        return False  # 全新数据库：SCHEMA 已建好新表
    compact = "".join((ddl_row[0] or "").lower().split())
    if "seqintegerprimarykeyautoincrement" not in compact:
        return False  # 已是每轮编号的新 schema

    legacy = conn.execute(
        "SELECT round_id,type,member_index,version,payload_json,at"
        " FROM events ORDER BY round_id, seq"
    ).fetchall()

    # PRAGMA foreign_keys 只能在事务外切换；迁移期间临时关闭以便换表
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("DROP TABLE IF EXISTS events_new")
        conn.execute(
            "CREATE TABLE events_new ("
            "round_id TEXT NOT NULL REFERENCES rounds(id),"
            "seq INTEGER NOT NULL,"
            "type TEXT NOT NULL,"
            "member_index INTEGER,"
            "version INTEGER,"
            "payload_json TEXT,"
            "at TEXT NOT NULL,"
            "PRIMARY KEY (round_id, seq)"
            ") WITHOUT ROWID"
        )
        counters: dict[str, int] = {}
        for r in legacy:
            counters[r["round_id"]] = counters.get(r["round_id"], 0) + 1
            conn.execute(
                "INSERT INTO events_new(round_id,seq,type,member_index,"
                "version,payload_json,at) VALUES(?,?,?,?,?,?,?)",
                (r["round_id"], counters[r["round_id"]], r["type"],
                 r["member_index"], r["version"], r["payload_json"], r["at"]),
            )
        # 旧索引 idx_events_round 随旧表一并删除
        conn.execute("DROP TABLE events")
        conn.execute("ALTER TABLE events_new RENAME TO events")
        # 旧 AUTOINCREMENT 在 sqlite_sequence 中留下的计数器行随之失效，清理之
        conn.execute("DELETE FROM sqlite_sequence WHERE name='events'")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    return True


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(SCHEMA)
        # 兼容旧库：把全局 AUTOINCREMENT 事件序号重排为每轮内 1..N。
        # 首次连接（即进程内唯一写连接建立时）执行一次，整体事务化。
        with _lock:
            migrated = _migrate_per_round_seq(conn)
            if migrated:
                conn.commit()
                log.info("events 已迁移为每轮独立连续编号")
        _conn = conn
    return _conn


class CommitOnServiceError(Exception):
    """携带此异常离开事务时，已写入的状态迁移（如“作废”）必须提交。

    承诺不符 / 冲突揭示时，服务层先把整轮置为 voided 再向客户端返回 4xx；
    若按普通异常回滚，作废本身也会丢失。服务层捕获后转换为 ServiceError。
    """


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """串行化的写事务（BEGIN IMMEDIATE）。嵌套调用复用同一把可重入锁。

    CommitOnServiceError 穿透时提交（作废是合法迁移），其他异常回滚。
    """
    conn = connect()
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except CommitOnServiceError:
            conn.commit()
            raise
        except Exception:
            conn.rollback()
            raise


@contextmanager
def read_lock() -> Iterator[sqlite3.Connection]:
    conn = connect()
    with _lock:
        yield conn


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    with read_lock() as conn:
        return conn.execute(sql, params).fetchone()


def query_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with read_lock() as conn:
        return conn.execute(sql, params).fetchall()
