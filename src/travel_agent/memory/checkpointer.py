"""LangGraph 检查点:会话状态持久化,支撑追问挂起 / 中断恢复。"""

import sqlite3
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from travel_agent.config import settings


def build_checkpointer(db_path: str | None = None, in_memory: bool = False) -> BaseCheckpointSaver:
    """创建检查点保存器。

    - in_memory=True → InMemorySaver(测试用,进程内有效);
    - 否则 → SqliteSaver 落盘持久化,路径取 db_path 参数或配置(默认 data/travel.db):
      interrupt() 挂起后(等用户回答追问/确认),状态存进 SQLite;
      进程重启后同一 thread_id 可从中断处续跑。

    注:langgraph 1.x 的 SqliteSaver.from_conn_string() 返回上下文管理器,
    此处直接用 SqliteSaver(conn) 构造,连接生命周期与进程一致。
    """
    if in_memory:
        return InMemorySaver()
    path = db_path or settings.db_path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    return SqliteSaver(conn)
