"""业务核心：把读到的 EPC 变成有意义的进出记录。"""

from __future__ import annotations

import queue
import threading
from datetime import datetime
from typing import Callable

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


def _elapsed_seconds(last_iso: str | None, now: datetime) -> float | None:
    """距上次刷卡的秒数。无记录或时间戳损坏时返回 None（视为首次刷卡）。"""
    if not last_iso:
        return None
    try:
        last = datetime.fromisoformat(last_iso)
    except ValueError:
        return None
    return (now - last).total_seconds()


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
        elapsed = _elapsed_seconds(student.last_swipe_at, now)

        # 第一级：软件去重窗口内静默丢弃，不写库、不改状态
        if elapsed is not None and elapsed < self._dedup_window:
            return None

        direction = Direction.OUT if in_campus else Direction.IN

        # 第二级：重复刷卡窗口内记录并告警，但不翻转状态。
        # 同时刷新 last_swipe_at —— 否则学生在闸机前久留时窗口会从旧事件起算而过期，
        # 导致"人还在门口却被判为出校"。
        is_duplicate = elapsed is not None and elapsed < self._duplicate_window

        saved = self._db.insert_swipe(SwipeEvent(
            id=None, epc=epc, student_no=student.student_no, name=student.name,
            direction=direction,
            status=SwipeStatus.DUPLICATE if is_duplicate else SwipeStatus.OK,
            rssi=tag.rssi, ant_id=tag.ant_id, reader_addr=tag.reader_addr,
            swiped_at=now_iso,
        ))
        # 重复刷卡保持原状态，正常刷卡才翻转
        self._db.set_student_state(
            epc, in_campus if is_duplicate else not in_campus, now_iso, today
        )
        return saved


class SwipeConsumer:
    """从队列取 RawTagEvent、跑业务逻辑、把非 None 的结果交给 on_event。

    单条事件处理失败不能杀死线程 —— 否则一次脏数据就会让整个看板停止更新。
    """

    def __init__(self, service: SwipeService,
                 in_queue: "queue.Queue[RawTagEvent]",
                 on_event: Callable[[SwipeEvent], None],
                 poll_timeout: float = 0.5,
                 clock: Callable[[], datetime] = datetime.now):
        self._service = service
        self._in = in_queue
        self._on_event = on_event
        self._poll_timeout = poll_timeout
        self._clock = clock
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="swipe-consumer", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                tag = self._in.get(timeout=self._poll_timeout)
            except queue.Empty:
                continue
            try:
                event = self._service.process(tag, self._clock())
            except Exception:
                continue
            if event is not None:
                self._on_event(event)
