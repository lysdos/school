import queue
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.db import Database
from app.models import Direction, RawTagEvent, SwipeStatus
from app.service import SwipeConsumer, SwipeService, decide_direction

EPC_LI = "E28011B0A50500756E79A1C9"
EPC_UNKNOWN = "FFFFFFFFFFFFFFFF"


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "campus.db")
    d.upsert_student(EPC_LI, "1424764951", "李弘毅", "2026-10-15T00:00:00")
    yield d
    d.close()


@pytest.fixture()
def service(db):
    return SwipeService(db, dedup_window=10.0, duplicate_window=120.0)


def _tag(epc=EPC_LI, rssi=-52, ant_id=1):
    return RawTagEvent(
        epc=epc, rssi=rssi, ant_id=ant_id,
        reader_addr="192.168.1.168:8160",
        read_at="2026-10-15T08:23:11",
    )


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def test_decide_direction_when_off_campus_is_in():
    assert decide_direction(False) is Direction.IN


def test_decide_direction_when_on_campus_is_out():
    assert decide_direction(True) is Direction.OUT


def test_first_swipe_marks_in_campus(service, db):
    event = service.process(_tag(), _at("2026-10-15T08:23:11"))
    assert event is not None
    assert event.direction is Direction.IN
    assert event.status is SwipeStatus.OK
    assert event.name == "李弘毅"
    assert event.student_no == "1424764951"
    assert event.rssi == -52
    assert event.ant_id == 1
    assert event.id == 1

    assert db.get_student(EPC_LI).in_campus is True
    assert db.get_student(EPC_LI).state_date == "2026-10-15"


def test_second_swipe_same_day_marks_out(service, db):
    service.process(_tag(), _at("2026-10-15T08:23:11"))
    event = service.process(_tag(), _at("2026-10-15T12:00:00"))
    assert event.direction is Direction.OUT
    assert event.status is SwipeStatus.OK
    assert db.get_student(EPC_LI).in_campus is False


def test_unknown_card_is_recorded_without_state_change(service, db):
    event = service.process(_tag(EPC_UNKNOWN), _at("2026-10-15T08:23:11"))
    assert event.status is SwipeStatus.UNKNOWN_CARD
    assert event.direction is Direction.UNKNOWN
    assert event.student_no is None
    assert event.name is None
    assert event.epc == EPC_UNKNOWN


def test_unknown_card_does_not_create_student_row(service, db):
    service.process(_tag(EPC_UNKNOWN), _at("2026-10-15T08:23:11"))
    assert db.get_student(EPC_UNKNOWN) is None
    assert len(db.list_students()) == 1


def test_epc_is_normalized_before_lookup(service):
    event = service.process(_tag(epc=EPC_LI.lower()), _at("2026-10-15T08:23:11"))
    assert event.epc == EPC_LI
    assert event.status is SwipeStatus.OK


def test_state_resets_across_day(service, db):
    # 昨天进校，今天第一次刷卡应该判为「进」而不是「出」
    db.set_student_state(EPC_LI, True, "2026-10-14T17:00:00", "2026-10-14")
    event = service.process(_tag(), _at("2026-10-15T08:00:00"))
    assert event.direction is Direction.IN
    assert event.status is SwipeStatus.OK
    student = db.get_student(EPC_LI)
    assert student.in_campus is True
    assert student.state_date == "2026-10-15"


def test_swiped_at_uses_local_isoformat(service):
    event = service.process(_tag(), _at("2026-10-15T08:23:11"))
    assert event.swiped_at == "2026-10-15T08:23:11"


def test_reader_addr_is_recorded(service):
    event = service.process(_tag(), _at("2026-10-15T08:23:11"))
    assert event.reader_addr == "192.168.1.168:8160"


# ---------- Task 5: 两级去重窗口 ----------

def test_swipe_within_dedup_window_is_silently_dropped(service, db):
    service.process(_tag(), _at("2026-10-15T08:23:11"))
    result = service.process(_tag(), _at("2026-10-15T08:23:19"))  # +8s
    assert result is None
    assert db.count_swipes() == 1
    assert db.get_student(EPC_LI).in_campus is True  # 状态未被翻转


def test_swipe_exactly_at_dedup_window_is_not_dropped(service, db):
    """去重边界是「小于」而非「小于等于」：恰好 10 秒时不再丢弃。

    这一步会被写入节流拦下（距上次写库不足 duplicate_window），
    所以断言的是「状态锚点被刷新」而不是「写了记录」——
    这正好区分了「丢弃」和「不写库但更新锚点」两种路径。
    """
    service.process(_tag(), _at("2026-10-15T08:23:11"))
    result = service.process(_tag(), _at("2026-10-15T08:23:21"))  # 恰好 +10s
    assert result is None
    assert db.get_student(EPC_LI).last_swipe_at == "2026-10-15T08:23:21"
    assert db.count_swipes() == 1


def test_duplicate_alert_fires_once_per_window_while_lingering(service, db):
    """重复刷卡告警：标签持续在范围内时，每个 duplicate_window 记一条 duplicate。

    30 秒内的第二次刷卡会被节流静默（距上次写库不足 120 秒），
    要等窗口到期才写 —— 这样既保留告警，又不会被常驻标签刷爆库。
    """
    service.process(_tag(), _at("2026-10-15T08:23:11"))            # 进校，写库
    assert service.process(_tag(), _at("2026-10-15T08:23:41")) is None  # +30s 被节流

    result = service.process(_tag(), _at("2026-10-15T08:25:11"))   # +120s，窗口到期
    assert result is not None
    assert result.status is SwipeStatus.DUPLICATE
    assert result.direction is Direction.OUT       # 按当时状态推算的方向
    assert db.get_student(EPC_LI).in_campus is True  # 未翻转


def test_duplicate_refreshes_last_swipe_at(service, db):
    service.process(_tag(), _at("2026-10-15T08:23:11"))
    service.process(_tag(), _at("2026-10-15T08:23:41"))
    assert db.get_student(EPC_LI).last_swipe_at == "2026-10-15T08:23:41"


def test_lingering_student_never_flips(service, db):
    """学生在闸机前逗留 5 分钟，每 30 秒被读到一次，状态应始终为「在校」。

    每轮都要断言，不能只看最终状态：若实现误翻转，偶数次迭代后状态会绕回原值，
    只在结尾断言会让这个 bug 蒙混过关（变异测试实测发现过这个问题）。
    """
    t = _at("2026-10-15T08:00:00")
    service.process(_tag(), t)
    assert db.get_student(EPC_LI).in_campus is True
    for i in range(1, 11):
        service.process(_tag(), t + timedelta(seconds=30 * i))
        assert db.get_student(EPC_LI).in_campus is True, f"第 {i} 次逗留刷卡后状态被翻转"
    # 300 秒 / 120 秒窗口 = 2 条 duplicate，加上首次那条 ok，共 3 条
    assert db.count_swipes(date="2026-10-15") == 3
    assert all(e.status is SwipeStatus.DUPLICATE for e in db.list_swipes(size=100)[:-1])


def test_swipe_after_duplicate_window_toggles_normally(service, db):
    service.process(_tag(), _at("2026-10-15T08:23:11"))          # 进校
    result = service.process(_tag(), _at("2026-10-15T08:25:12"))  # +121s
    assert result.status is SwipeStatus.OK
    assert result.direction is Direction.OUT
    assert db.get_student(EPC_LI).in_campus is False


def test_dedup_window_is_per_student(service, db):
    db.upsert_student("BBBB", "1213311379", "单钰辰", "2026-10-15T00:00:00")
    service.process(_tag(), _at("2026-10-15T08:23:11"))
    other = service.process(_tag(epc="BBBB"), _at("2026-10-15T08:23:12"))
    assert other is not None
    assert other.status is SwipeStatus.OK


def test_unknown_card_within_dedup_window_is_dropped(service, db):
    """未登记卡也要走去重 —— 真机实测发现原实现每次上报都写库，
    75 个常驻样品标签能刷出 2400 条/分钟。"""
    service.process(_tag(EPC_UNKNOWN), _at("2026-10-15T08:23:11"))
    again = service.process(_tag(EPC_UNKNOWN), _at("2026-10-15T08:23:16"))  # +5s
    assert again is None
    assert db.count_swipes() == 1


# ---------- 真机联调修复：写入节流 ----------
# 现场有几十个标签常驻读取范围时，原实现会以约 2400 条/分钟的速度写库。
# 修复思路：把「状态锚点」和「写入节流」拆成两个字段。
#   last_swipe_at  —— 每次有效读到就刷新，保证在校状态不因逗留而误翻转
#   last_logged_at —— 只在真正写库时刷新，限制同一 EPC 的写入频率

def test_lingering_student_writes_at_most_one_row_per_window(service, db):
    """学生在读卡范围内停留 10 分钟，每 5 秒被读到一次。

    状态必须始终是「在校」（不能来回翻），且写库次数要受 duplicate_window 限制。
    600 秒 / 120 秒窗口 = 5 条 duplicate，加上首次那条 ok，共 6 条。
    """
    t = _at("2026-10-15T08:00:00")
    service.process(_tag(), t)
    for i in range(1, 121):
        service.process(_tag(), t + timedelta(seconds=5 * i))
        assert db.get_student(EPC_LI).in_campus is True, f"第 {i} 次读取后状态被翻转"

    assert db.count_swipes() == 6
    assert db.get_student(EPC_LI).in_campus is True


def test_lingering_unknown_card_writes_at_most_one_row_per_window(service, db):
    """未登记标签常驻同理：不能每次上报都写库。"""
    t = _at("2026-10-15T08:00:00")
    service.process(_tag(EPC_UNKNOWN), t)
    for i in range(1, 121):
        service.process(_tag(EPC_UNKNOWN), t + timedelta(seconds=5 * i))

    assert db.count_swipes() == 6


def test_many_unknown_cards_do_not_flood(service, db):
    """75 个标签常驻 1 分钟，每个每 5 秒被读到一次。

    窗口是 120 秒，所以每个标签只应该写首次那一条 —— 共 75 条，
    而不是 75 × 12 = 900 条。
    """
    t = _at("2026-10-15T08:00:00")
    for tick in range(12):
        for k in range(75):
            service.process(_tag(f"UNKNOWN{k:04d}"),
                            t + timedelta(seconds=5 * tick))

    assert db.count_swipes() == 75


def test_state_anchor_refreshes_on_every_effective_read(service, db):
    """last_swipe_at 要跟着刷新（它是状态锚点），last_logged_at 只在写库时动。"""
    t = _at("2026-10-15T08:00:00")
    service.process(_tag(), t)
    service.process(_tag(), t + timedelta(seconds=30))   # 不写库，但刷新锚点

    s = db.get_student(EPC_LI)
    assert s.last_swipe_at == "2026-10-15T08:00:30"
    assert s.last_logged_at == "2026-10-15T08:00:00"
    assert db.count_swipes() == 1


def test_real_pass_after_window_still_toggles_and_logs(service, db):
    """节流不能影响正常进出：间隔够长的两次刷卡都要记录。"""
    service.process(_tag(), _at("2026-10-15T08:00:00"))
    service.process(_tag(), _at("2026-10-15T12:00:00"))

    assert db.count_swipes() == 2
    assert [e.direction for e in db.list_swipes()] == [Direction.OUT, Direction.IN]
    assert db.get_student(EPC_LI).in_campus is False


def test_unknown_tracker_purges_stale_entries(service):
    """跟踪表要能清理陈旧条目，否则内存会随现场标签数无限增长。"""
    t = _at("2026-10-15T08:00:00")
    for k in range(50):
        service.process(_tag(f"UNKNOWN{k:04d}"), t)
    assert service.unknown_tracked_count == 50

    service.process(_tag("UNKNOWN9999"), t + timedelta(hours=1))
    assert service.unknown_tracked_count == 1


def test_tracker_is_per_epc_not_global(service, db):
    """两个不同的未登记标签互不影响去重。"""
    service.process(_tag("UNKNOWN0001"), _at("2026-10-15T08:00:00"))
    service.process(_tag("UNKNOWN0002"), _at("2026-10-15T08:00:01"))
    assert db.count_swipes() == 2


# ---------- Task 7: 消费线程 ----------

def _wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_consumer_delivers_processed_events(db):
    service = SwipeService(db, 10.0, 120.0)
    tag_queue: queue.Queue = queue.Queue()
    seen = []
    consumer = SwipeConsumer(service, tag_queue, seen.append, poll_timeout=0.05)
    consumer.start()
    try:
        tag_queue.put(_tag())
        assert _wait_until(lambda: len(seen) == 1)
        assert seen[0].name == "李弘毅"
    finally:
        consumer.stop()


def test_consumer_drops_deduped_events(db):
    service = SwipeService(db, 10.0, 120.0)
    tag_queue: queue.Queue = queue.Queue()
    seen = []
    consumer = SwipeConsumer(service, tag_queue, seen.append, poll_timeout=0.05)
    consumer.start()
    try:
        tag_queue.put(_tag())
        assert _wait_until(lambda: len(seen) == 1)
        tag_queue.put(_tag())          # 去重窗口内 → process 返回 None
        time.sleep(0.3)
        assert len(seen) == 1
    finally:
        consumer.stop()


def test_consumer_survives_service_exception(db):
    class Boom:
        def process(self, tag, now):
            raise RuntimeError("boom")

    tag_queue: queue.Queue = queue.Queue()
    consumer = SwipeConsumer(Boom(), tag_queue, lambda _e: None, poll_timeout=0.05)
    consumer.start()
    try:
        tag_queue.put(_tag())
        time.sleep(0.2)
        assert consumer._thread.is_alive()
    finally:
        consumer.stop()


def test_consumer_stop_joins_thread(db):
    service = SwipeService(db, 10.0, 120.0)
    consumer = SwipeConsumer(service, queue.Queue(), lambda _e: None, poll_timeout=0.05)
    consumer.start()
    consumer.stop()
    assert not consumer._thread.is_alive()
