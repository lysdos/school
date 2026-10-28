"""外部上传 worker。

职责:把 swipes 表里 uploaded_at IS NULL 的记录批量 POST 到配置的目标 URL,
成功(2xx)就把这一批的 uploaded_at 戳上。失败就让它们留到下一轮再试。

设计选择:
- 后台线程轮询而不是每次刷卡都发:刷卡是高频路径,网络慢不应该阻塞它。
  轮询周期 upload_interval 默认 15 秒 —— 通常配置的『外部系统』不需要更实时。
- 单批 upload_batch_size 默认 50,失败时整批不重试,下一轮再拉。
- 401/403/422 等永久性失败也不致命:依靠人工在后台管理界面发现并修复。
- URL 为空或 enabled=false 时整个 worker 直接不启动,零开销。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from .config import Config
from .db import Database
from .models import swipe_to_dict


class UploadWorker:
    def __init__(self, cfg: Config, db: Database,
                 clock=datetime.now, sleep=__import__("time").sleep):
        self._cfg = cfg
        self._db = db
        self._clock = clock
        self._sleep = sleep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.upload_enabled and self._cfg.upload_url)

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="upload-worker", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.flush_once()
            except Exception:
                # 任何异常都不该杀死工作线程 —— 它自带退避重试
                pass
            self._sleep(self._cfg.upload_interval)

    def flush_once(self) -> dict:
        """单次拉一批推送。给后台『立即重传』按钮用。"""
        if not self.enabled:
            return {"ok": False, "reason": "disabled", "sent": 0, "ok_count": 0}
        rows = self._db.list_pending_uploads(limit=self._cfg.upload_batch_size)
        if not rows:
            return {"ok": True, "sent": 0, "ok_count": 0}
        payload = {"events": [swipe_to_dict(e) for e in rows]}
        ids = [e.id for e in rows if e.id is not None]
        try:
            self._post(payload)
        except Exception as exc:
            return {"ok": False, "reason": str(exc), "sent": len(ids), "ok_count": 0}
        uploaded_at = self._clock().isoformat(timespec="seconds")
        n = self._db.mark_uploaded(ids, uploaded_at)
        return {"ok": True, "sent": len(ids), "ok_count": n}

    def _post(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._cfg.upload_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "rfid-campus-demo/1.0",
            },
        )
        token = self._cfg.upload_token
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=self._cfg.upload_timeout) as resp:
                # 2xx 即视为成功;不解析响应体,给上游自由发挥
                if not (200 <= resp.status < 300):
                    raise RuntimeError(f"HTTP {resp.status}")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"URLError: {exc.reason}") from exc
