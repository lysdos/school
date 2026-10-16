"""SQLite schema 与 DAO。多线程共用一条连接，用锁串行化。"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from .models import Direction, Student, SwipeEvent, SwipeStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    epc            TEXT PRIMARY KEY,
    student_no     TEXT NOT NULL,
    name           TEXT NOT NULL,
    in_campus      INTEGER NOT NULL DEFAULT 0,
    last_swipe_at  TEXT,
    last_logged_at TEXT,
    state_date     TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS swipes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    epc         TEXT NOT NULL,
    student_no  TEXT,
    name        TEXT,
    direction   TEXT NOT NULL,
    status      TEXT NOT NULL,
    rssi        INTEGER,
    ant_id      INTEGER,
    reader_addr TEXT,
    swiped_at   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_swipes_swiped_at ON swipes(swiped_at);
CREATE INDEX IF NOT EXISTS idx_swipes_epc       ON swipes(epc);
CREATE INDEX IF NOT EXISTS idx_swipes_no_date   ON swipes(student_no, swiped_at);
"""

_SWIPE_COLS = (
    "id, epc, student_no, name, direction, status, "
    "rssi, ant_id, reader_addr, swiped_at"
)


def _row_to_student(row: sqlite3.Row) -> Student:
    return Student(
        epc=row["epc"],
        student_no=row["student_no"],
        name=row["name"],
        in_campus=bool(row["in_campus"]),
        last_swipe_at=row["last_swipe_at"],
        last_logged_at=row["last_logged_at"],
        state_date=row["state_date"],
    )


def _row_to_swipe(row: sqlite3.Row) -> SwipeEvent:
    return SwipeEvent(
        id=row["id"],
        epc=row["epc"],
        student_no=row["student_no"],
        name=row["name"],
        direction=Direction(row["direction"]),
        status=SwipeStatus(row["status"]),
        rssi=row["rssi"],
        ant_id=row["ant_id"],
        reader_addr=row["reader_addr"],
        swiped_at=row["swiped_at"],
    )


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """给早于写入节流修复的库补列。CREATE TABLE IF NOT EXISTS 不会改已存在的表。"""
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(students)")}
        if "last_logged_at" not in columns:
            self._conn.execute("ALTER TABLE students ADD COLUMN last_logged_at TEXT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- students ----------

    def upsert_student(self, epc: str, student_no: str, name: str, created_at: str) -> None:
        """幂等导入：已存在则只更新学号/姓名，绝不动 in_campus 等运行状态。"""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO students (epc, student_no, name, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(epc) DO UPDATE SET
                    student_no = excluded.student_no,
                    name       = excluded.name
                """,
                (epc, student_no, name, created_at),
            )
            self._conn.commit()

    def get_student(self, epc: str) -> Student | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM students WHERE epc = ?", (epc,)
            ).fetchone()
        return _row_to_student(row) if row else None

    def list_students(self) -> list[Student]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM students ORDER BY student_no"
            ).fetchall()
        return [_row_to_student(r) for r in rows]

    def set_student_state(
        self, epc: str, in_campus: bool, last_swipe_at: str, state_date: str,
        last_logged_at: str | None = None,
    ) -> None:
        """更新学生状态。

        last_logged_at 传 None 表示「本次没有写库」，此时保留原值 ——
        否则每次读到标签都会把节流窗口重置，常驻标签又会无限写库。
        """
        with self._lock:
            self._conn.execute(
                """
                UPDATE students
                   SET in_campus      = ?,
                       last_swipe_at  = ?,
                       state_date     = ?,
                       last_logged_at = COALESCE(?, last_logged_at)
                 WHERE epc = ?
                """,
                (1 if in_campus else 0, last_swipe_at, state_date,
                 last_logged_at, epc),
            )
            self._conn.commit()

    # ---------- swipes ----------

    def insert_swipe(self, event: SwipeEvent) -> SwipeEvent:
        created_at = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO swipes
                    (epc, student_no, name, direction, status,
                     rssi, ant_id, reader_addr, swiped_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.epc, event.student_no, event.name,
                    event.direction.value, event.status.value,
                    event.rssi, event.ant_id, event.reader_addr,
                    event.swiped_at, created_at,
                ),
            )
            self._conn.commit()
            new_id = cur.lastrowid
        return SwipeEvent(**{**event.__dict__, "id": new_id})

    def _swipe_where(self, name, student_no, date,
                     registered_only: bool = False) -> tuple[str, list]:
        """拼 WHERE 子句。

        SQL 片段只从下面这几个固定模板里选，不拼接任何外部输入；
        所有用户提供的值都走 params 绑定，因此下面的 f-string 是安全的。
        """
        clauses: list[str] = []
        params: list = []
        if name:
            clauses.append("name LIKE ?")
            params.append(f"%{name}%")
        if student_no:
            clauses.append("student_no = ?")
            params.append(student_no)
        if date:
            clauses.append("swiped_at LIKE ?")
            params.append(f"{date}%")
        if registered_only:
            # 未登记卡不参与历史展示；它们仍在库里（审计与告警要用），只是查不到
            clauses.append("name IS NOT NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def list_swipes(self, name=None, student_no=None, date=None,
                    page: int = 1, size: int = 20,
                    registered_only: bool = False) -> list[SwipeEvent]:
        where, params = self._swipe_where(name, student_no, date, registered_only)
        offset = max(page - 1, 0) * size
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_SWIPE_COLS} FROM swipes {where} "
                f"ORDER BY swiped_at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, size, offset),
            ).fetchall()
        return [_row_to_swipe(r) for r in rows]

    def count_swipes(self, name=None, student_no=None, date=None,
                     registered_only: bool = False) -> int:
        where, params = self._swipe_where(name, student_no, date, registered_only)
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM swipes {where}", tuple(params)
            ).fetchone()
        return int(row["n"])

    def today_stats(self, today: str) -> dict:
        prefix = f"{today}%"
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN direction = 'in'
                                      AND status = 'ok' THEN 1 ELSE 0 END), 0) AS in_count,
                    COALESCE(SUM(CASE WHEN direction = 'out'
                                      AND status = 'ok' THEN 1 ELSE 0 END), 0) AS out_count
                  FROM swipes
                 WHERE swiped_at LIKE ?
                """,
                (prefix,),
            ).fetchone()
            in_campus = self._conn.execute(
                "SELECT COUNT(*) AS n FROM students WHERE in_campus = 1 AND state_date = ?",
                (today,),
            ).fetchone()
            total = self._conn.execute(
                "SELECT COUNT(*) AS n FROM students"
            ).fetchone()
        return {
            "date": today,
            "in_count": int(row["in_count"]),
            "out_count": int(row["out_count"]),
            "in_campus_count": int(in_campus["n"]),
            "total_students": int(total["n"]),
        }
