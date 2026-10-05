"""SQLite 业务留痕:四表持久化会话/消息/决策/trace(M7 记忆层②)。

与 JSONL 的分工:SQLite 是"查询用主库"(按 session 查历史、按幂等 key
去重),JSONL 是"回放用副本"(逐行追加,给 replay 命令流式读)。
二者都写,一份数据两种用途。

四表:
- sessions:一次行程规划一行(状态:挂起/已交付/失败)
- messages:队列消息留痕,idempotency_key 唯一索引 —— 重发同 key 的消息
  被数据库层直接去重,与队列层 SETNX 形成双保险
- decisions:所有"人机决策"留痕(预算调整/天气变更/确认结果),即
  M4 四要素、M5 三要素之外的第三层审计台账
- traces:trace 事件镜像(JSONL 为主,这里可按 span_id 精确查询)

线程安全:sqlite3 连接 check_same_thread=False 允许多线程共用,
写操作统一过一把锁串行化;全部 INSERT 用 OR IGNORE + 唯一约束
实现幂等(重复写入自动跳过,不报错)。
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from travel_agent.config import settings
from travel_agent.schemas.message import AgentMessage
from travel_agent.schemas.trace import TraceEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    user_input  TEXT,
    status      TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    message_id      TEXT PRIMARY KEY,
    session_id      TEXT,
    idempotency_key TEXT UNIQUE,
    topic           TEXT,
    msg_type        TEXT,
    payload         TEXT,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    session_id  TEXT,
    kind        TEXT,
    payload     TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS traces (
    span_id     TEXT PRIMARY KEY,
    trace_id    TEXT,
    kind        TEXT,
    name        TEXT,
    status      TEXT,
    duration_ms REAL,
    attempts    INTEGER,
    started_at  TEXT,
    detail      TEXT,
    created_at  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    """SQLite 留痕库(连接惰性创建,写操作幂等)。"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or settings.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        """惰性建连 + 建表(首次写入时才真正落盘)。"""
        if self._conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn
        return self._conn

    def _write(self, sql: str, params: tuple) -> None:
        """带锁执行一条写语句(OR IGNORE 语义由调用方 SQL 决定)。"""
        with self._lock:
            conn = self._get_conn()
            conn.execute(sql, params)
            conn.commit()

    # ── sessions ──

    def save_session(self, session_id: str, user_input: str,
                     status: str = "running") -> None:
        self._write(
            "INSERT OR IGNORE INTO sessions(session_id, created_at, user_input, status)"
            " VALUES(?, ?, ?, ?)",
            (session_id, _now(), user_input, status),
        )

    def update_session_status(self, session_id: str, status: str) -> None:
        self._write("UPDATE sessions SET status=? WHERE session_id=?",
                    (status, session_id))

    # ── messages(幂等 key 唯一 → 重复消息落库自动去重)──

    def save_message(self, msg: AgentMessage, session_id: str = "") -> None:
        self._write(
            "INSERT OR IGNORE INTO messages"
            "(message_id, session_id, idempotency_key, topic, msg_type, payload, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?)",
            (msg.id, session_id, msg.idempotency_key, msg.topic,
             msg.type.value, json.dumps(msg.payload, ensure_ascii=False, default=str),
             _now()),
        )

    # ── decisions ──

    def save_decision(self, decision_id: str, session_id: str,
                      kind: str, payload: dict) -> None:
        self._write(
            "INSERT OR IGNORE INTO decisions(decision_id, session_id, kind, payload, created_at)"
            " VALUES(?, ?, ?, ?, ?)",
            (decision_id, session_id, kind,
             json.dumps(payload, ensure_ascii=False, default=str), _now()),
        )

    # ── traces ──

    def save_trace(self, event: TraceEvent) -> None:
        detail = {"input": event.input, "output": event.output,
                  "error": event.error, "extra": event.extra,
                  "parent_span_id": event.parent_span_id}
        self._write(
            "INSERT OR IGNORE INTO traces"
            "(span_id, trace_id, kind, name, status, duration_ms, attempts, started_at, detail, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event.span_id, event.trace_id, event.kind, event.name, event.status,
             event.duration_ms, event.attempts, event.started_at,
             json.dumps(detail, ensure_ascii=False, default=str), _now()),
        )

    # ── 查询(CLI/评测用)──

    def list_sessions(self, limit: int = 20) -> list[tuple]:
        conn = self._get_conn()
        return conn.execute(
            "SELECT session_id, created_at, status, user_input FROM sessions"
            " ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

    def decisions_for(self, session_id: str) -> list[tuple]:
        conn = self._get_conn()
        return conn.execute(
            "SELECT kind, payload, created_at FROM decisions WHERE session_id=?"
            " ORDER BY created_at", (session_id,)).fetchall()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


@lru_cache
def get_store() -> Store:
    """进程内单例(数据库路径来自配置)。"""
    return Store()
