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
    # 状态锚点：每次有效读到就刷新。保证学生在读卡范围内逗留时「在校」状态不来回翻。
    last_swipe_at: str | None = None
    # 写入节流：只在真正写库时刷新。限制同一张卡写库的频率。
    # 两者必须分开 —— 合并成一个字段会让常驻标签无限写库（真机实测 2400 条/分钟）。
    last_logged_at: str | None = None
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


def normalize_epc(epc: str) -> str:
    """EPC 一律转大写后比对，避免读写器上报与名单大小写不一致导致匹配失败。"""
    return str(epc).strip().upper()


def swipe_to_dict(event: SwipeEvent) -> dict:
    """API 与 SSE 共用的序列化。枚举转成字符串值。"""
    return {
        "id": event.id,
        "epc": event.epc,
        "student_no": event.student_no,
        "name": event.name,
        "direction": event.direction.value,
        "status": event.status.value,
        "rssi": event.rssi,
        "ant_id": event.ant_id,
        "reader_addr": event.reader_addr,
        "swiped_at": event.swiped_at,
    }


def student_to_dict(student: Student) -> dict:
    return {
        "epc": student.epc,
        "student_no": student.student_no,
        "name": student.name,
        "in_campus": student.in_campus,
        "last_swipe_at": student.last_swipe_at,
    }
