"""配置加载：环境变量 > config.yaml > 内置默认。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Config:
    rdr_host: str
    rdr_port: int
    rdr_antenna: int
    rdr_power: int | None
    rdr_repeated_time: int
    dedup_window: float
    duplicate_window: float
    roster_file: Path
    db_path: Path
    use_device_beep: bool
    # 看板自己的地址。只用于 tools/simulate.py 拼默认请求地址 ——
    # 真正的监听端口由 uvicorn 命令行给（Dockerfile 里固定 8000）。
    # 在容器里跑 simulate.py 时，这个值应当是容器内部的端口，不是宿主机映射的端口。
    http_port: int
    # 外部上传接收端的配置。URL 为空 = 关闭上传（默认），
    # 让『没有外部系统』的场景零成本跑起来。
    upload_enabled: bool
    upload_url: str
    upload_token: str          # Bearer Token;可留空
    upload_timeout: float      # 单次请求超时，秒
    upload_interval: float     # 轮询周期，秒
    upload_batch_size: int     # 单批最大条数
    # 后台管理页的登录口令。空串 = 禁用后台（默认）
    admin_password: str

DEFAULTS: dict[str, object] = {
    "rdr_host": "192.168.1.168",
    "rdr_port": 8160,
    "rdr_antenna": 1,
    "rdr_power": 30,
    "rdr_repeated_time": 50,
    "dedup_window": 10.0,
    "duplicate_window": 120.0,
    "roster_file": "data/学生记录.xlsx",
    "db_path": "data/campus.db",
    "use_device_beep": False,
    "http_port": 8000,
    "upload_enabled": False,
    "upload_url": "",
    "upload_token": "",
    "upload_timeout": 5.0,
    "upload_interval": 15.0,
    "upload_batch_size": 50,
    "admin_password": "",
}

ENV_MAP: dict[str, str] = {
    "rdr_host": "RDR_HOST",
    "rdr_port": "RDR_PORT",
    "rdr_antenna": "RDR_ANTENNA",
    "rdr_power": "RDR_POWER",
    "rdr_repeated_time": "RDR_REPEATED_TIME",
    "dedup_window": "DEDUP_WINDOW",
    "duplicate_window": "DUPLICATE_WINDOW",
    "roster_file": "ROSTER_FILE",
    "db_path": "DB_PATH",
    "use_device_beep": "USE_DEVICE_BEEP",
    "http_port": "HTTP_PORT",
    "upload_enabled": "UPLOAD_ENABLED",
    "upload_url": "UPLOAD_URL",
    "upload_token": "UPLOAD_TOKEN",
    "upload_timeout": "UPLOAD_TIMEOUT",
    "upload_interval": "UPLOAD_INTERVAL",
    "upload_batch_size": "UPLOAD_BATCH_SIZE",
    "admin_password": "ADMIN_PASSWORD",
}

_PATH_KEYS = ("roster_file", "db_path")
_FALSEY = {"", "0", "false", "no", "off"}


def _coerce_bool(raw: str) -> bool:
    return raw.strip().lower() not in _FALSEY


def _coerce(raw: str, default: object) -> object:
    """按默认值的类型把环境变量字符串转成目标类型。"""
    if isinstance(default, bool):
        return _coerce_bool(raw)
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


def load_config(config_path: Path | None = None) -> Config:
    raw: dict[str, object] = dict(DEFAULTS)

    if config_path is not None and Path(config_path).exists():
        with open(config_path, encoding="utf-8") as fh:
            from_file = yaml.safe_load(fh) or {}
        # 值为 None 的键不覆盖默认，避免 yaml 里写了 `key:` 就把默认值抹掉
        raw.update({k: v for k, v in from_file.items() if v is not None and k in DEFAULTS})

    for key, env_name in ENV_MAP.items():
        if env_name in os.environ:
            raw[key] = _coerce(os.environ[env_name], DEFAULTS[key])

    kwargs: dict[str, object] = {}
    for key, value in raw.items():
        kwargs[key] = Path(value) if key in _PATH_KEYS else value
    return Config(**kwargs)  # type: ignore[arg-type]


def describe_timezone() -> str:
    """启动时自检用：打印当前时区与本地时间，便于排查 TZ 没设导致的日期错位。"""
    now = datetime.now()
    return f"时区={now.astimezone().tzname()} 当前本地时间={now.isoformat(timespec='seconds')}"
