"""FastAPI 路由：REST + SSE + 静态前端。"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from .bus import EventBus
from .config import Config, describe_timezone
from .db import Database
from .models import RawTagEvent, student_to_dict, swipe_to_dict
from .service import SwipeService
from .upload import UploadWorker

WEB_DIR = Path(__file__).parent / "web"

SSE_KEEPALIVE_SECONDS = 15.0


def create_app(cfg: Config, db: Database, service: SwipeService,
               reader: Any, bus: EventBus, workers: Any = None,
               upload: UploadWorker | None = None) -> FastAPI:
    """装配 FastAPI 应用。

    workers 是一个有 start() / stop() 的对象（读卡线程与消费线程的持有者），
    由 app/main.py 注入。测试里传 None 即可，此时只绑定事件总线。
    用 lifespan 而不是已废弃的 on_event。
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        bus.bind_loop(asyncio.get_running_loop())
        if workers is not None:
            workers.start()
        try:
            yield
        finally:
            if workers is not None:
                workers.stop()

    app = FastAPI(title="校园出入看板", version="1.0.0", lifespan=lifespan)

    @app.get("/api/health")
    def health() -> dict:
        return {
            "reader_connected": bool(getattr(reader, "connected", False)),
            "timezone": describe_timezone(),
            "now": datetime.now().isoformat(timespec="seconds"),
        }

    @app.get("/api/stats/today")
    def stats_today() -> dict:
        return db.today_stats(datetime.now().date().isoformat())

    @app.get("/api/students")
    def students() -> list[dict]:
        return [student_to_dict(s) for s in db.list_students()]

    @app.get("/api/swipes")
    def swipes(name: str | None = None,
               student_no: str | None = None,
               date: str | None = None,
               registered_only: bool = False,
               page: int = Query(1, ge=1),
               size: int = Query(20, ge=1, le=200)) -> dict:
        rows = db.list_swipes(name=name, student_no=student_no, date=date,
                              page=page, size=size,
                              registered_only=registered_only)
        return {
            "total": db.count_swipes(name=name, student_no=student_no, date=date,
                                     registered_only=registered_only),
            "page": page,
            "size": size,
            "items": [swipe_to_dict(e) for e in rows],
        }

    @app.get("/api/events/stream")
    async def events_stream(request: Request) -> StreamingResponse:
        q = bus.subscribe()

        async def generator():
            try:
                yield "retry: 3000\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(
                            q.get(), timeout=SSE_KEEPALIVE_SECONDS
                        )
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"   # 心跳，防止反向代理掐断长连接
                        continue
                    yield f"event: swipe\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
            finally:
                bus.unsubscribe(q)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/simulate")
    def simulate(epc: str = Query(..., description="要模拟刷卡的 EPC")) -> dict:
        tag = RawTagEvent(
            epc=epc, rssi=None, ant_id=None, reader_addr="simulator",
            read_at=datetime.now().isoformat(timespec="seconds"),
        )
        event = service.process(tag, datetime.now())
        if event is None:
            return {"accepted": False, "event": None}
        payload = swipe_to_dict(event)
        bus.publish_threadsafe(payload)
        return {"accepted": True, "event": payload}

    @app.post("/api/reset")
    def reset_data() -> dict:
        """清空所有刷卡记录并把学生状态复位为「离校」。名单保留。

        **不可撤销**。看板侧调用前必须让用户二次确认。
        内存里的未登记卡去重表也要一起清，否则不是真的清空。
        """
        result = db.reset_all()
        service.reset_dedup_state()
        return result

    if WEB_DIR.exists():
        # 把静态资源挂到 /static/,根路径用一个 GET 显式返回 index.html;
        # 这样不会把所有未匹配到的请求都重定向到静态文件,
        # 也不会拦截其它动词(POST/PATCH/DELETE)的后台接口。
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="web-static")

        from fastapi.responses import FileResponse

        @app.get("/", include_in_schema=False)
        def _index() -> FileResponse:
            return FileResponse(str(WEB_DIR / "index.html"))

        # 后台管理页面。**刻意在 admin_password 为空时也提供**:
        # 页面本身不含任何数据,真正的接口全部 404,打开只会看到登录框
        # 然后提示口令错误。这样"后台是否存在"仍然无法从接口侧探测,
        # 而知道地址的人能直接打开页面、填口令。
        admin_html = WEB_DIR / "admin.html"

        @app.get("/admin", include_in_schema=False)
        def _admin_page() -> FileResponse:
            if not admin_html.exists():
                raise HTTPException(status_code=404, detail="后台页面缺失")
            return FileResponse(str(admin_html))

        # 兼容 /admin/ 的写法（有人会习惯加斜杠）
        @app.get("/admin/", include_in_schema=False)
        def _admin_page_slash() -> FileResponse:
            return _admin_page()
    else:
        # 没有 web 目录时不挂静态资源,留个最简 GET /
        @app.get("/", include_in_schema=False)
        def _index_fallback() -> dict:
            return {"name": "rfid-campus-demo"}

    # 管理后台 — 仅当 admin_password 非空时启用；启用前先 import,
    # 所以即便没启用也不报 ImportError。
    from . import admin as admin_mod
    if upload is not None:
        admin_mod.mount_admin(app, cfg, db, upload)
    else:
        # 没上传 worker 时不让后台路由挂上(否则管理员点不到东西)。
        pass

    return app
