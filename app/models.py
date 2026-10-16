"""领域模型：枚举与 dataclass。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Direction(str, Enum):
    IN = "in"
    OUT = "out"
    UNKNOWN = "unknown"


class SwipeStatus(str, Enum):
    OK = "ok"
    UNKNOWN_CARD = "unknown_card"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class Student:
    epc: str
    student_no: str
    name: str
    in_campus: bool = False
    last_swipe_at: str | None = None
    state_date: str | None = None


@dataclass(frozen=True)
class RawTagEvent:
    """读卡器回调产出、塞进队列的原始事件。"""

    epc: str
    rssi: int | None
    ant_id: int | None
    reader_addr: str
    read_at: str


@dataclass(frozen=True)
class SwipeEvent:
    """落库后的刷卡记录。id 为 None 表示尚未写入。"""

    id: int | None
    epc: str
    student_no: str | None
    name: str | None
    direction: Direction
    status: SwipeStatus
    rssi: int | None
    ant_id: int | None
    reader_addr: str
    swiped_at: str
