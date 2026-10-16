from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.db import Database
from app.models import Direction, RawTagEvent, SwipeStatus
from app.service import SwipeService, decide_direction

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
