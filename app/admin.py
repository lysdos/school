"""后台管理路由:鉴权 + 学生管理 + 刷卡记录管理 + 上传配置。

鉴权:Cookie 会话。给定 admin_password 时,
- POST /api/admin/login  接受 password,对了就 Set-Cookie 写入;
- 之后 /api/admin/* 都用这个 Cookie。
- 配置项为空时整个后台禁用:除 /api/admin/login 返回 503,其他全部 404。

目的:不依赖第三方,够演示用。把 admin_password 当成『知道的人才能改』的口令,
不是真正的认证;别拿这套去对外网。给它单独路由也避免与公开 API 路径混淆。
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Cookie, FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from .config import Config
from .db import Database
from .models import Direction, SwipeStatus
from .upload import UploadWorker

SESSION_COOKIE = "admin_sid"
SESSION_TTL_SECONDS = 8 * 3600  # 8 小时


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class StudentUpsert(BaseModel):
    epc: str = Field(min_length=4, max_length=64)
    student_no: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    class_name: str | None = Field(default=None, max_length=64)


class StudentPatch(BaseModel):
    student_no: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    class_name: str | None = Field(default=None, max_length=64)


class SwipeEdit(BaseModel):
    direction: str | None = Field(default=None)
    status: str | None = Field(default=None)
    swiped_at: str | None = Field(default=None)


class UploadConfigIn(BaseModel):
    enabled: bool
    url: str = Field(default="", max_length=500)
    token: str = Field(default="", max_length=500)
    timeout: float = Field(default=5.0, ge=0.5, le=60.0)
    interval: float = Field(default=15.0, ge=1.0, le=600.0)
    batch_size: int = Field(default=50, ge=1, le=500)


class AdminPasswordIn(BaseModel):
    new_password: str = Field(min_length=0, max_length=200)


def _check_enabled(cfg: Config) -> None:
    """后台未配置密码时所有管理类接口直接 404。"""
    if not cfg.admin_password:
        raise HTTPException(status_code=404, detail="管理后台未启用")


def _require_session(cfg: Config, request: Request) -> None:
    """校验 Cookie 中的 sid 是否仍有效。

    注意:这份『会话』只是演示用 —— 它把 sid 看作不透明 token,任何合法 sid
    都视为『登录过的』,没有用户体系。要做真正鉴权得换 OAuth2 / JWT 之类。
    """
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid or sid not in _SESSIONS.get(cfg, set()):
        raise HTTPException(status_code=401, detail="未登录")


# 让 Cookie 串能跟着当前 Config 失效:重启或改密码自动踢出旧会话
_SESSIONS: dict[Config, set[str]] = {}


def _sessions_for(cfg: Config) -> set[str]:
    return _SESSIONS.setdefault(cfg, set())


def mount_admin(app: FastAPI, cfg: Config, db: Database, upload: UploadWorker) -> None:
    """把后台管理接口挂到 app。

    关键约束:admin_password 为空时**完全不挂任何路由**——做到这一点后,
    应用行为对外和对 disable 状态完全一致,无法从外部探测后台是否『存在』。
    """
    if not cfg.admin_password:
        return

    @app.post("/api/admin/login")
    def login(body: LoginBody, response: Response) -> dict:
        _check_enabled(cfg)
        if not secrets.compare_digest(body.password, cfg.admin_password):
            raise HTTPException(status_code=403, detail="口令错误")
        sid = secrets.token_urlsafe(32)
        _sessions_for(cfg).add(sid)
        response.set_cookie(SESSION_COOKIE, sid,
                            max_age=SESSION_TTL_SECONDS,
                            httponly=True, samesite="lax", path="/")
        return {"ok": True}

    @app.post("/api/admin/logout")
    def logout(request: Request) -> dict:
        sid = request.cookies.get(SESSION_COOKIE)
        if sid:
            _sessions_for(cfg).discard(sid)
        return {"ok": True}

    @app.get("/api/admin/whoami")
    def whoami(request: Request) -> dict:
        _check_enabled(cfg)
        try:
            _require_session(cfg, request)
            return {"authenticated": True}
        except HTTPException:
            return {"authenticated": False}

    # ---------- 学生管理 ----------

    @app.get("/api/admin/students")
    def list_students(request: Request) -> list[dict]:
        _check_enabled(cfg); _require_session(cfg, request)
        rows = db.list_students()
        return [{
            "epc": s.epc, "student_no": s.student_no, "name": s.name,
            "class_name": s.class_name, "in_campus": s.in_campus,
            "last_swipe_at": s.last_swipe_at,
        } for s in rows]

    @app.post("/api/admin/students")
    def upsert(body: StudentUpsert, request: Request) -> dict:
        _check_enabled(cfg); _require_session(cfg, request)
        from .models import normalize_epc
        from datetime import datetime
        epc = normalize_epc(body.epc)
        db.upsert_student(
            epc, body.student_no, body.name,
            datetime.now().isoformat(timespec="seconds"),
            class_name=body.class_name or None,
        )
        return {"ok": True, "epc": epc}

    @app.patch("/api/admin/students/{epc}")
    def patch_student(epc: str, body: StudentPatch, request: Request) -> dict:
        _check_enabled(cfg); _require_session(cfg, request)
        from .models import normalize_epc
        epc = normalize_epc(epc)
        if db.get_student(epc) is None:
            raise HTTPException(status_code=404, detail="EPC 不在名单")
        db.update_student(epc,
                          student_no=body.student_no,
                          name=body.name,
                          class_name=body.class_name or None)
        return {"ok": True}

    @app.delete("/api/admin/students/{epc}")
    def delete_student(epc: str, request: Request) -> dict:
        _check_enabled(cfg); _require_session(cfg, request)
        from .models import normalize_epc
        epc = normalize_epc(epc)
        if not db.delete_student(epc):
            raise HTTPException(status_code=404, detail="EPC 不在名单")
        return {"ok": True}

    # ---------- 刷卡记录管理 ----------

    @app.get("/api/admin/swipes")
    def list_swipes(name: str | None = None, student_no: str | None = None,
                    date: str | None = None, direction: str | None = None,
                    status_filter: str | None = None,
                    uploaded: str | None = None,
                    request: Request = None,
                    page: int = 1, size: int = 20) -> dict:
        _check_enabled(cfg); _require_session(cfg, request)
        size = max(1, min(size, 200))
        rows = db.list_swipes_for_admin(
            name=name, student_no=student_no, date=date,
            direction=direction, status=status_filter, uploaded=uploaded,
            page=page, size=size,
        )
        total = db.count_swipes_for_admin(
            name=name, student_no=student_no, date=date,
            direction=direction, status=status_filter, uploaded=uploaded,
        )
        return {
            "total": total, "page": page, "size": size,
            "items": [{
                "id": e.id, "epc": e.epc, "student_no": e.student_no,
                "name": e.name, "class_name": e.class_name,
                "direction": e.direction.value, "status": e.status.value,
                "swiped_at": e.swiped_at, "uploaded_at": e.uploaded_at,
            } for e in rows],
        }

    @app.patch("/api/admin/swipes/{swipe_id}")
    def edit_swipe(swipe_id: int, body: SwipeEdit,
                   request: Request) -> dict:
        _check_enabled(cfg); _require_session(cfg, request)
        if body.direction and body.direction not in {d.value for d in Direction}:
            raise HTTPException(422, detail=f"direction 必须是 "
                                            f"{[d.value for d in Direction]}")
        if body.status and body.status not in {s.value for s in SwipeStatus}:
            raise HTTPException(422, detail=f"status 必须是 "
                                            f"{[s.value for s in SwipeStatus]}")
        if not db.update_swipe_admin(swipe_id,
                                     direction=body.direction,
                                     status=body.status,
                                     swiped_at=body.swiped_at):
            raise HTTPException(status_code=404, detail="记录不存在")
        return {"ok": True}

    @app.delete("/api/admin/swipes/{swipe_id}")
    def delete_swipe(swipe_id: int, request: Request) -> dict:
        _check_enabled(cfg); _require_session(cfg, request)
        if not db.delete_swipe_admin(swipe_id):
            raise HTTPException(status_code=404, detail="记录不存在")
        return {"ok": True}

    @app.post("/api/admin/reupload")
    def reupload(request: Request) -> dict:
        _check_enabled(cfg); _require_session(cfg, request)
        return upload.flush_once()

    # ---------- 上传配置 ----------

    @app.get("/api/admin/upload/config")
    def get_upload(request: Request) -> dict:
        _check_enabled(cfg); _require_session(cfg, request)
        return {
            "enabled": cfg.upload_enabled,
            "url": cfg.upload_url,
            "token": cfg.upload_token,
            "timeout": cfg.upload_timeout,
            "interval": cfg.upload_interval,
            "batch_size": cfg.upload_batch_size,
        }

    @app.put("/api/admin/upload/config")
    def put_upload(body: UploadConfigIn, request: Request) -> dict:
        _check_enabled(cfg); _require_session(cfg, request)
        # 配置只能通过环境变量或 config.yaml 写 —— 后台持久化要求重启,
        # 这里只接受修改运行时『立即生效』的部分(enabled/url/token/参数),
        # 但 dataclass 是 frozen 的,所以『立即生效』需要写到运行时上下文。
        # 简化处理:全部用环境变量文件提示用户去改。
        # 这里接受请求但提示重启:
        raise HTTPException(
            status_code=501,
            detail=("请改 config.yaml 或环境变量 (UPLOAD_ENABLED/URL/TOKEN...) "
                    "后重启服务。运行期改不了,因为 dataclass 是 immutable。"),
        )

    # ---------- 后台口令本身 ----------

    @app.put("/api/admin/password")
    def change_password(body: AdminPasswordIn, request: Request) -> dict:
        _check_enabled(cfg); _require_session(cfg, request)
        # 同上,运行期改值做不到。这里只给友好提示。
        raise HTTPException(
            status_code=501,
            detail="请改 config.yaml / 环境变量 ADMIN_PASSWORD 后重启服务。",
        )
