"""业务核心：把读到的 EPC 变成有意义的进出记录。

真机联调暴露的问题
------------------
现场有几十个标签常驻读取范围时，硬件会持续上报（约 36 次/秒）。原实现有两条路径
会把每一次上报都写成一条记录，实测写入速率约 2400 条/分钟：

  1. 未登记卡完全没有去重 —— 查不到学生就直接写库
  2. 重复刷卡每次都写库，且会刷新 last_swipe_at，导致常驻标签永不停止

修复思路是把原本挤在一个字段里的两件事拆开：

  last_swipe_at  —— 状态锚点。每次「有效读到」就刷新，保证学生在读卡范围内
                    逗留时「在校」状态不来回翻转（这是看板最直观的数字）。
  last_logged_at —— 写入节流。只在真正写库时刷新，把同一 EPC 的写入限制在
                    每个 duplicate_window 最多一条。

未登记卡没有 students 行可挂状态，用一个内存 TTL 表做同样的事。
它只是限流器而非业务状态，所以不落库，重启后最多多写一轮。
"""

from __future__ import annotations

import queue
import threading
from datetime import datetime, timedelta
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

# 跟踪表超过这个条目数才开始做 TTL 清理。
# 小规模场景（几十个标签）下完全不遍历，避免每次调用都 O(n)。
TRACKER_PURGE_THRESHOLD = 8
TRACKER_MAX_ENTRIES = 20000
# 多久没被读到就从跟踪表里清掉。取得比 duplicate_window 大一个量级，
# 保证正常节流窗口内的条目不会被误清。
TRACKER_TTL_SECONDS = 1200.0


def decide_direction(in_campus: bool) -> Direction:
    """单天线状态翻转：离校状态下刷卡判为进校，在校状态下判为出校。

    这是方向判定的唯一实现点。将来换成双天线判先后或 GPI 传感器真方向判定，
    只需替换这个函数，其余业务逻辑不受影响。
    """
    return Direction.OUT if in_campus else Direction.IN


def _elapsed_seconds(last_iso: str | None, now: datetime) -> float | None:
    """距上次刷卡的秒数。无记录或时间戳损坏时返回 None（视为首次刷卡）。"""
    if not last_iso:
        return None
    try:
        last = datetime.fromisoformat(last_iso)
    except ValueError:
        return None
    return (now - last).total_seconds()


class UnknownTagTracker:
    """未登记标签的「最后读到 / 最后写库」记录。内存态，重启即失效。"""

    def __init__(self, ttl_seconds: float = TRACKER_TTL_SECONDS):
        self._ttl = ttl_seconds
        # epc -> (last_seen_iso, last_logged_iso | None)
        self._entries: dict[str, tuple[str, str | None]] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, epc: str) -> tuple[str | None, str | None]:
        return self._entries.get(epc, (None, None))

    def clear(self) -> None:
        self._entries.clear()

    def update(self, epc: str, last_seen_iso: str, last_logged_iso: str | None,
               now: datetime) -> None:
        """last_logged_iso 传 None 表示本次没写库，保留原值。"""
        self._purge(now)
        _, prev_logged = self._entries.get(epc, (None, None))
        self._entries[epc] = (
            last_seen_iso,
            last_logged_iso if last_logged_iso is not None else prev_logged,
        )

    def _purge(self, now: datetime) -> None:
        if len(self._entries) < TRACKER_PURGE_THRESHOLD:
            return
        cutoff = now - timedelta(seconds=self._ttl)
        for epc in [e for e, (seen, _) in self._entries.items()
                    if seen is not None and datetime.fromisoformat(seen) < cutoff]:
            del self._entries[epc]
        if len(self._entries) >= TRACKER_MAX_ENTRIES:
            # 兜底：现场标签数异常多时直接清空，宁可多写一轮也不能把内存吃光
            self._entries.clear()


class SwipeService:
    def __init__(self, db: Database, dedup_window: float = 10.0,
                 duplicate_window: float = 120.0):
        self._db = db
        self._dedup_window = dedup_window
        self._duplicate_window = duplicate_window
        self._unknown = UnknownTagTracker()

    @property
    def unknown_tracked_count(self) -> int:
        return len(self._unknown)

    def reset_dedup_state(self) -> None:
        """清空未登记卡的内存去重表。

        数据库重置时必须一起调，否则「清空」是假清空 ——
        内存里还留着这些标签的「刚见过」记录，重置后它们要等窗口过期才会重新上报。
        """
        self._unknown.clear()

    def process(self, tag: RawTagEvent, now: datetime) -> SwipeEvent | None:
        epc = normalize_epc(tag.epc)
        now_iso = now.isoformat(timespec="seconds")
        student = self._db.get_student(epc)
        if student is None:
            return self._process_unknown(epc, tag, now, now_iso)
        return self._process_student(student, tag, now, now_iso)

    # ---------- 已知学生 ----------

    def _process_student(self, student: Student, tag: RawTagEvent,
                         now: datetime, now_iso: str) -> SwipeEvent | None:
        today = now.date().isoformat()
        # 跨天重置：状态不属于今天就视为离校，省掉一个定时任务
        in_campus = student.in_campus if student.state_date == today else False

        plan = self._plan(_elapsed_seconds(student.last_swipe_at, now),
                          _elapsed_seconds(student.last_logged_at, now))
        if plan is None:
            return None                       # 静默丢弃，状态和锚点都不动

        log_now, toggle = plan
        direction = decide_direction(in_campus)

        event = None
        if log_now:
            event = self._db.insert_swipe(SwipeEvent(
                id=None, epc=student.epc, student_no=student.student_no,
                name=student.name, direction=direction,
                status=SwipeStatus.OK if toggle else SwipeStatus.DUPLICATE,
                rssi=tag.rssi, ant_id=tag.ant_id, reader_addr=tag.reader_addr,
                swiped_at=now_iso,
            ))
        self._db.set_student_state(
            student.epc, (not in_campus) if toggle else in_campus,
            now_iso, today,
            last_logged_at=now_iso if log_now else None,
        )
        return event

    # ---------- 未登记卡 ----------

    def _process_unknown(self, epc: str, tag: RawTagEvent,
                         now: datetime, now_iso: str) -> SwipeEvent | None:
        last_seen, last_logged = self._unknown.get(epc)
        plan = self._plan(_elapsed_seconds(last_seen, now),
                          _elapsed_seconds(last_logged, now))
        if plan is None:
            return None

        log_now, _ = plan
        event = None
        if log_now:
            event = self._db.insert_swipe(SwipeEvent(
                id=None, epc=epc, student_no=None, name=None,
                direction=Direction.UNKNOWN, status=SwipeStatus.UNKNOWN_CARD,
                rssi=tag.rssi, ant_id=tag.ant_id, reader_addr=tag.reader_addr,
                swiped_at=now_iso,
            ))
        self._unknown.update(epc, now_iso, now_iso if log_now else None, now)
        return event

    # ---------- 去重与节流决策 ----------

    def _plan(self, since_swipe: float | None,
              since_log: float | None) -> tuple[bool, bool] | None:
        """返回 (是否写库, 是否翻转状态)；返回 None 表示静默丢弃。

        两级窗口：
          - since_swipe < dedup_window         → 丢弃，什么都不动
          - since_swipe < duplicate_window     → 不翻转（人在门口逗留）
          - since_log   < duplicate_window     → 不写库（写入节流）

        注意 since_swipe 会随每次有效读到刷新，而 since_log 只在写库时刷新。
        这个分离是关键：只用 since_swipe 做节流的话，常驻标签每 10 秒就会写一条，
        永不停止（真机实测 2400 条/分钟就是这么来的）。
        """
        if since_swipe is not None and since_swipe < self._dedup_window:
            return None
        log_now = since_log is None or since_log >= self._duplicate_window
        toggle = since_swipe is None or since_swipe >= self._duplicate_window
        return log_now, toggle


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
