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
    class_name     TEXT,                  -- 出校语音播报『X 班』用;允许 NULL(老名单或手工新增未填)
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
    class_name  TEXT,                     -- 入库快照,即便后来改了班级也不影响历史
    direction   TEXT NOT NULL,
    status      TEXT NOT NULL,
    rssi        INTEGER,
    ant_id      INTEGER,
    reader_addr TEXT,
    swiped_at   TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    uploaded_at TEXT                     -- NULL 表示尚未上传到外部系统
);

CREATE INDEX IF NOT EXISTS idx_swipes_swiped_at ON swipes(swiped_at);
CREATE INDEX IF NOT EXISTS idx_swipes_epc       ON swipes(epc);
CREATE INDEX IF NOT EXISTS idx_swipes_no_date   ON swipes(student_no, swiped_at);
"""
# 这条 partial index 依赖 uploaded_at,在 _migrate() 里等列补完再建
INDEX_PENDING_UPLOADED = (
    "CREATE INDEX IF NOT EXISTS idx_swipes_pending "
    "ON swipes(uploaded_at) WHERE uploaded_at IS NULL"
)

_SWIPE_COLS = (
    "id, epc, student_no, name, class_name, direction, status, "
    "rssi, ant_id, reader_addr, swiped_at"
)


def _row_to_student(row: sqlite3.Row) -> Student:
    return Student(
        epc=row["epc"],
        student_no=row["student_no"],
        name=row["name"],
        class_name=row["class_name"],
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
        class_name=row["class_name"],
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
        # 等所有列都补齐后再建依赖新列的索引
        try:
            self._conn.execute(INDEX_PENDING_UPLOADED)
        except Exception:
            pass
        self._conn.commit()

    def _migrate(self) -> None:
        """给老库补列。CREATE TABLE IF NOT EXISTS 不会改已存在的表。

        每次启动都跑一遍迁移:列已存在时 ALTER 会抛,捕到就跳过,
        等价于一条幂等的迁移脚本,部署到旧数据库不需要人工干预。

        SCHEMA 里那条 idx_swipes_pending 故意不放,因为它依赖 uploaded_at 列;
        老库升级时这条列先被本函数补上,然后 caller 再创建索引。
        """
        def _add(table: str, column: str, ddl: str) -> None:
            cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column in cols:
                return
            try:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
            except Exception:
                pass

        _add("students", "last_logged_at", "last_logged_at TEXT")
        _add("students", "class_name",     "class_name TEXT")
        _add("swipes",   "class_name",     "class_name TEXT")
        _add("swipes",   "uploaded_at",    "uploaded_at TEXT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- students ----------

    def upsert_student(self, epc: str, student_no: str, name: str, created_at: str,
                       class_name: str | None = None) -> None:
        """幂等导入:已存在则只更新学号/姓名/班级，绝不动 in_campus 等运行状态。

        class_name 传 None 时不动原值；传具体字符串(可能为空串)就覆盖。
        """
        with self._lock:
            if class_name is None:
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
            else:
                self._conn.execute(
                    """
                    INSERT INTO students (epc, student_no, name, class_name, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(epc) DO UPDATE SET
                        student_no = excluded.student_no,
                        name       = excluded.name,
                        class_name = excluded.class_name
                    """,
                    (epc, student_no, name, class_name, created_at),
                )
            self._conn.commit()

    def update_student(self, epc: str, *, student_no: str, name: str,
                       class_name: str | None) -> None:
        """后台管理用:显式更新一条学生记录的所有字段。

        与 upsert_student 的区别是这里会接受 class_name 为空串,
        用于在后台把已存在的字段清空。
        """
        with self._lock:
            self._conn.execute(
                """
                UPDATE students
                   SET student_no = ?, name = ?, class_name = ?
                 WHERE epc = ?
                """,
                (student_no, name, class_name, epc),
            )
            self._conn.commit()

    def delete_student(self, epc: str) -> bool:
        """删除名单里的一条。已存在的刷卡记录不会被删(留给审计)。

        返回是否真的删了一行;没有该 EPC 时返回 False,便于调用方决定要不要报错。
        """
        with self._lock:
            cur = self._conn.execute("DELETE FROM students WHERE epc = ?", (epc,))
            self._conn.commit()
        return cur.rowcount > 0

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
                    (epc, student_no, name, class_name, direction, status,
                     rssi, ant_id, reader_addr, swiped_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.epc, event.student_no, event.name, event.class_name,
                    event.direction.value, event.status.value,
                    event.rssi, event.ant_id, event.reader_addr,
                    event.swiped_at, created_at,
                ),
            )
            self._conn.commit()
            new_id = cur.lastrowid
        return SwipeEvent(**{**event.__dict__, "id": new_id})

    def mark_uploaded(self, swipe_ids: list[int], uploaded_at: str) -> int:
        """把一批 swipes 标记为已上传。返回实际更新的行数。

        只标记还停留在 NULL 的行:这里没有 CAS,如果有重试可能把"已上传过"再写一次,
        也只是个时间戳,幂等无害。
        """
        if not swipe_ids:
            return 0
        placeholders = ",".join("?" * len(swipe_ids))
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE swipes SET uploaded_at = ? "
                f"WHERE id IN ({placeholders}) AND uploaded_at IS NULL",
                [uploaded_at, *swipe_ids],
            )
            self._conn.commit()
        return cur.rowcount

    def list_pending_uploads(self, limit: int = 100) -> list[SwipeEvent]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_SWIPE_COLS}, uploaded_at FROM swipes "
                f"WHERE uploaded_at IS NULL ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            ev = _row_to_swipe(r)
            out.append(SwipeEvent(**{**ev.__dict__, "uploaded_at": r["uploaded_at"]}))
        return out

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

    def reset_all(self) -> dict:
        """清空所有刷卡记录，并把学生状态复位为「离校」。**名单保留**。

        只有这个操作是破坏性的，调用方（`POST /api/reset`）必须让用户二次确认。
        """
        with self._lock:
            cleared = self._conn.execute("DELETE FROM swipes").rowcount
            reset = self._conn.execute(
                """
                UPDATE students
                   SET in_campus      = 0,
                       last_swipe_at  = NULL,
                       last_logged_at = NULL,
                       state_date     = NULL
                """
            ).rowcount
            self._conn.commit()
        return {"cleared_swipes": cleared, "reset_students": reset}

    # ---------- 后台专用查询 ----------

    def list_swipes_for_admin(self, *, name: str | None = None,
                              student_no: str | None = None,
                              date: str | None = None,
                              direction: str | None = None,
                              status: str | None = None,
                              uploaded: str | None = None,
                              page: int = 1, size: int = 20) -> list[SwipeEvent]:
        """后台历史查询。uploaded: 'yes' | 'no' | None。"""
        where, params = self._swipe_where(name, student_no, date)
        extras = []
        if direction:
            extras.append("direction = ?"); params.append(direction)
        if status:
            extras.append("status = ?"); params.append(status)
        if uploaded == "yes":
            extras.append("uploaded_at IS NOT NULL")
        elif uploaded == "no":
            extras.append("uploaded_at IS NULL")
        if extras:
            where = (where + " AND " if where else "WHERE ") + " AND ".join(extras)
        offset = max(page - 1, 0) * size
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_SWIPE_COLS} FROM swipes {where} "
                f"ORDER BY swiped_at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, size, offset),
            ).fetchall()
        return [_row_to_swipe(r) for r in rows]

    def count_swipes_for_admin(self, *, name=None, student_no=None, date=None,
                               direction=None, status=None, uploaded=None) -> int:
        where, params = self._swipe_where(name, student_no, date)
        extras = []
        if direction:
            extras.append("direction = ?"); params.append(direction)
        if status:
            extras.append("status = ?"); params.append(status)
        if uploaded == "yes":
            extras.append("uploaded_at IS NOT NULL")
        elif uploaded == "no":
            extras.append("uploaded_at IS NULL")
        if extras:
            where = (where + " AND " if where else "WHERE ") + " AND ".join(extras)
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM swipes {where}", tuple(params)
            ).fetchone()
        return int(row["n"])

    def insert_swipe_admin(self, event: SwipeEvent) -> SwipeEvent:
        """后台手动补一条 swipe。沿用 insert_swipe,但这里允许 swiped_at 自定义。"""
        return self.insert_swipe(event)

    def update_swipe_admin(self, swipe_id: int, *, direction: str | None = None,
                           status: str | None = None,
                           swiped_at: str | None = None) -> bool:
        """后台手动修订一条 swipes 记录,字段都可选。

        仅返回是否真的改了一行,不抛异常:这是后台编辑动作,字段缺失就当成不改。
        """
        fields, values = [], []
        if direction:
            fields.append("direction = ?"); values.append(direction)
        if status:
            fields.append("status = ?"); values.append(status)
        if swiped_at:
            fields.append("swiped_at = ?"); values.append(swiped_at)
        if not fields:
            return False
        values.append(swipe_id)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE swipes SET {', '.join(fields)} WHERE id = ?",
                values,
            )
            self._conn.commit()
        return cur.rowcount > 0

    def delete_swipe_admin(self, swipe_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM swipes WHERE id = ?", (swipe_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def today_stats(self, today: str) -> dict:
        prefix = f"{today}%";
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
            pending = self._conn.execute(
                "SELECT COUNT(*) AS n FROM swipes WHERE uploaded_at IS NULL"
            ).fetchone()
        return {
            "date": today,
            "in_count": int(row["in_count"]),
            "out_count": int(row["out_count"]),
            "in_campus_count": int(in_campus["n"]),
            "total_students": int(total["n"]),
            "pending_uploads": int(pending["n"]),
        }
