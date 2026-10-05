"""JSONL 留痕写入器:trace 事件追加写盘,线程安全,支持回放读取。

JSONL(JSON Lines)格式:文件里每行一个独立 JSON 对象。
选它的原因:追加写入极快(打开文件 seek 到末尾写一行)、
读取可逐行流式解析(回放大文件不用全量载入内存)、
单行损坏不影响其余行(容错)。

线程安全:进程内多处(状态机节点、队列消费者、CLI)可能并发
写同一个会话文件,用一把锁串行化 append,保证行与行不交错。
"""

import json
import threading
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

from travel_agent.config import settings


class JsonlWriter:
    """把记录追加写入目录下的 JSONL 文件(文件名由调用方指定)。"""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self._lock = threading.Lock()   # 全局一把锁:append 是互斥的

    # ── 写入 ──

    def append(self, filename: str, record: dict | BaseModel) -> None:
        """追加一行 JSON。record 传 dict 或 Pydantic 模型均可。"""
        if isinstance(record, BaseModel):
            line = record.model_dump_json()
        else:
            line = json.dumps(record, ensure_ascii=False, default=str)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / filename
        with self._lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    # ── 读取(回放用)──

    def read(self, filename: str) -> list[dict]:
        """逐行读回整个文件;文件不存在返回空列表(回放前先探测)。"""
        path = self.directory / filename
        if not path.is_file():
            return []
        records: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:                       # 跳过空行(容忍损坏行)
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue               # 单行损坏不影响其余行
        return records

    def trace_path(self, trace_id: str) -> Path:
        """trace 文件名约定:session_{trace_id}.jsonl。"""
        return self.directory / f"session_{trace_id}.jsonl"


@lru_cache
def get_writer() -> JsonlWriter:
    """进程内单例:目录来自配置 settings.trace_dir。"""
    return JsonlWriter(settings.trace_dir)


def record_event(event) -> None:
    """给 trace 事件写盘(trace 文件名按 event.trace_id 派生)。"""
    get_writer().append(f"session_{event.trace_id}.jsonl", event)


def read_trace(trace_id: str) -> list[dict]:
    """按 trace_id 读回全部事件(CLI replay 命令用)。"""
    return get_writer().read(f"session_{trace_id}.jsonl")
