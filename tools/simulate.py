"""无硬件时模拟刷卡，驱动整条链路。

用法：
    python tools/simulate.py                          # 随机挑一个已登记学生刷一次
    python tools/simulate.py --epc E28011B0...A1C9    # 指定 EPC
    python tools/simulate.py --count 5 --delay 2      # 连刷 5 次，间隔 2 秒
    python tools/simulate.py --unknown                # 模拟未登记卡
    python tools/simulate.py --direct                 # 绕过 HTTP 直接开库（服务没起时用）
    python tools/simulate.py --list                   # 打印名单
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config          # noqa: E402
from app.db import Database                 # noqa: E402
from app.models import RawTagEvent          # noqa: E402
from app.service import SwipeService        # noqa: E402

DEFAULT_CONFIG = Path("config.yaml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="模拟 RFID 刷卡")
    parser.add_argument("--epc", help="指定 EPC；不填则随机挑一个已登记学生")
    parser.add_argument("--count", type=int, default=1, help="刷卡次数，默认 1")
    parser.add_argument("--delay", type=float, default=1.0, help="两次刷卡间隔秒数，默认 1.0")
    parser.add_argument("--unknown", action="store_true", help="模拟未登记卡")
    parser.add_argument("--direct", action="store_true", help="绕过 HTTP，直接读写数据库")
    parser.add_argument("--list", action="store_true", help="打印名单后退出")
    parser.add_argument("--base-url", default=None, help="看板地址，默认从配置拼")
    return parser


def pick_epc(db: Database, want_unknown: bool = False) -> str:
    students = db.list_students()
    if want_unknown or not students:
        return "FFFFFFFFFFFFFFFF"
    return random.choice(students).epc


def _post(base_url: str, epc: str) -> dict:
    url = f"{base_url}/api/simulate?{urllib.parse.urlencode({'epc': epc})}"
    request = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _direct(db: Database, service: SwipeService, epc: str) -> dict:
    tag = RawTagEvent(epc=epc, rssi=None, ant_id=None, reader_addr="simulator",
                      read_at=datetime.now().isoformat(timespec="seconds"))
    event = service.process(tag, datetime.now())
    if event is None:
        return {"accepted": False, "event": None}
    return {
        "accepted": True,
        "event": {"name": event.name, "direction": event.direction.value,
                  "status": event.status.value, "swiped_at": event.swiped_at},
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(DEFAULT_CONFIG)
    db = Database(cfg.db_path)

    try:
        if args.list:
            for s in db.list_students():
                print(f"{s.epc}  {s.student_no}  {s.name}  "
                      f"{'在校' if s.in_campus else '离校'}")
            return 0

        service = SwipeService(db, cfg.dedup_window, cfg.duplicate_window)
        base_url = args.base_url or f"http://127.0.0.1:{cfg.http_port}"

        for i in range(args.count):
            epc = args.epc or pick_epc(db, want_unknown=args.unknown)
            try:
                if args.direct:
                    result = _direct(db, service, epc)
                else:
                    result = _post(base_url, epc)
            except urllib.error.URLError as exc:
                print(f"请求失败（看板没启动？加 --direct 可绕过 HTTP）：{exc}",
                      file=sys.stderr)
                return 1

            if result.get("accepted"):
                event = result["event"]
                print(f"[{i + 1}/{args.count}] {epc} → {event.get('name') or '未登记卡'} "
                      f"{event.get('direction')} {event.get('status')} "
                      f"{event.get('swiped_at')}")
            else:
                print(f"[{i + 1}/{args.count}] {epc} → 已忽略（去重窗口内）")

            if i + 1 < args.count:
                time.sleep(args.delay)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
