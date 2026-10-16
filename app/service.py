"""业务核心：把读到的 EPC 变成有意义的进出记录。"""

from __future__ import annotations

from datetime import datetime

from .db import Database
from .models import (
    Direction,
    RawTagEvent,
    Student,
    SwipeEvent,
    SwipeStatus,
    normalize_epc,
)


def decide_direction(student: Student) -> Direction:
    """单天线状态翻转：离校状态下刷卡判为进校，在校状态下判为出校。

    这是方向判定的唯一实现点。将来换成双天线判先后或 GPI 传感器真方向判定，
    只需替换这个函数，其余业务逻辑不受影响。
    """
    return Direction.OUT if student.in_campus else Direction.IN


class SwipeService:
    def __init__(self, db: Database, dedup_window: float = 10.0,
                 duplicate_window: float = 120.0):
        self._db = db
        self._dedup_window = dedup_window
        self._duplicate_window = duplicate_window

    def process(self, tag: RawTagEvent, now: datetime) -> SwipeEvent | None:
        epc = normalize_epc(tag.epc)
        today = now.date().isoformat()
        now_iso = now.isoformat(timespec="seconds")

        student = self._db.get_student(epc)
        if student is None:
            return self._db.insert_swipe(SwipeEvent(
                id=None, epc=epc, student_no=None, name=None,
                direction=Direction.UNKNOWN, status=SwipeStatus.UNKNOWN_CARD,
                rssi=tag.rssi, ant_id=tag.ant_id, reader_addr=tag.reader_addr,
                swiped_at=now_iso,
            ))

        # 跨天重置：状态不属于今天就视为离校，省掉一个定时任务
        in_campus = student.in_campus if student.state_date == today else False

        direction = Direction.OUT if in_campus else Direction.IN
        saved = self._db.insert_swipe(SwipeEvent(
            id=None, epc=epc, student_no=student.student_no, name=student.name,
            direction=direction, status=SwipeStatus.OK,
            rssi=tag.rssi, ant_id=tag.ant_id, reader_addr=tag.reader_addr,
            swiped_at=now_iso,
        ))
        self._db.set_student_state(epc, not in_campus, now_iso, today)
        return saved
