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


def test_decide_direction_when_off_campus_is_in(db):
    student = db.get_student(EPC_LI)
    assert decide_direction(student) is Direction.IN


def test_decide_direction_when_on_campus_is_out(db):
    db.set_student_state(EPC_LI, True, "2026-10-15T08:00:00", "2026-10-15")
    student = db.get_student(EPC_LI)
    assert decide_direction(student) is Direction.OUT


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


def test_swipe_exactly_at_dedup_window_is_not_dropped(service):
    service.process(_tag(), _at("2026-10-15T08:23:11"))
    result = service.process(_tag(), _at("2026-10-15T08:23:21"))  # 恰好 +10s
    assert result is not None
    assert result.status is SwipeStatus.DUPLICATE


def test_swipe_in_duplicate_window_is_flagged_and_not_toggled(service, db):
    service.process(_tag(), _at("2026-10-15T08:23:11"))          # 进校
    result = service.process(_tag(), _at("2026-10-15T08:23:41"))  # +30s
    assert result is not None
    assert result.status is SwipeStatus.DUPLICATE
    assert result.direction is Direction.OUT  # 按当时状态推算的方向
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
    # 10 次逗留刷卡全部标记为重复
    assert db.count_swipes(date="2026-10-15") == 11
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


def test_unknown_card_is_not_subject_to_dedup(service, db):
    service.process(_tag(EPC_UNKNOWN), _at("2026-10-15T08:23:11"))
    again = service.process(_tag(EPC_UNKNOWN), _at("2026-10-15T08:23:12"))
    assert again is not None
    assert again.status is SwipeStatus.UNKNOWN_CARD


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
