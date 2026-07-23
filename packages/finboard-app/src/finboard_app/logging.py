"""structlog 配置。

* 生产(staging/prod):JSON 输出,字段稳定便于采集;
* dev:console renderer,带颜色,易读;
* 敏感字段(``password`` / ``auth_code``)自动脱敏。
"""

from __future__ import annotations

import logging
from typing import Any

import structlog

from finboard_app.config import Settings

#: 日志中需要遮蔽的 key
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "auth_code",
        "ctp_password",
        "ctp_auth_code",
        "token",
        "secret",
        "api_key",
    }
)


def _redact_sensitive(
    _logger: object, _method_name: str, event_dict: Any
) -> Any:
    for key in list(event_dict):
        if key in SENSITIVE_KEYS:
            event_dict[key] = "***"
        value = event_dict[key]
        if isinstance(value, dict):
            for sub_key in list(value):
                if sub_key in SENSITIVE_KEYS:
                    value[sub_key] = "***"
    return event_dict


def setup_logging(settings: Settings) -> None:
    level = _parse_level(settings.log_level)

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_sensitive,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if settings.env == "dev" and not settings.log_json:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # 同时把标准库 logging 也桥接到 structlog(P0 简化:直接设置 level)
    logging.basicConfig(level=level)


def _parse_level(level: str) -> int:
    return getattr(logging, level.upper(), logging.INFO)
