"""强类型 ID 与 ``client_order_id`` 生成器。

设计原则:

* 使用 ``NewType`` 让 ``AccountId`` / ``StrategyId`` / ``ClientOrderId`` 在
  mypy 看来互不兼容,防止调用方把账号字符串误当成订单号传入。
* ``client_order_id`` 必须由**本地系统**生成,且全局唯一 —— 这是交易安全红线
  (见 AGENTS.md §交易安全红线)。其作用:

    1. 防重复下单(下单前幂等校验的 key);
    2. 关联策略信号 ↔ 券商订单;
    3. 重启恢复时匹配本地 ↔ 券商;
    4. 排查异常的审计锚点。

* 真正的防重复落地靠数据库 ``UNIQUE`` 索引(见 ``finboard_persistence``);
  本函数仅负责"几乎不可能冲突"的熵源。
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import NewType

#: 账户标识(本地概念,可与券商资金账号一一对应)
AccountId = NewType("AccountId", str)

#: 策略标识(P5 之后启用,P0 通常为 ``None`` 表示人工下单)
StrategyId = NewType("StrategyId", str)

#: 本地生成的全局唯一订单标识。红线:**禁止**接受外部传入,必须由 ``generate_client_order_id`` 产生
ClientOrderId = NewType("ClientOrderId", str)

#: ``client_order_id`` 全局前缀,便于在券商后台 / 日志中识别本系统订单
CLIENT_ORDER_ID_PREFIX = "F"


def generate_client_order_id() -> ClientOrderId:
    """生成全局唯一的 ``client_order_id``。

    格式::

        F-<YYYYMMDD-UTC>-<16 hex chars>

    * 日期取 UTC,避免跨日盘切换时本地时区漂移导致索引错乱;
    * 16 字节随机熵(128 bit), birthday collision 在 2^64 量级以下可忽略;
    * 不依赖数据库 sequence,因此可以在未连库的情况下预先生成;
    * 真正的防重复靠 ``(account_id, client_order_id)`` UNIQUE 索引兜底。
    """
    today = datetime.now(UTC).strftime("%Y%m%d")
    suffix = secrets.token_hex(8)  # 16 hex chars
    return ClientOrderId(f"{CLIENT_ORDER_ID_PREFIX}-{today}-{suffix}")
