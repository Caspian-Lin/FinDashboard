"""Broker 工厂:根据 ``BrokerKind`` 动态创建 adapter。

动态导入的好处:

* ``finboard-broker-qmt`` / ``finboard-broker-ctp`` 依赖的券商 SDK
  (``xtquant`` / ``ctp``)通常**只在 Windows / 特定券商终端环境**可用;
* 在 Linux CI / 本地开发机上不应因为缺 SDK 导致主进程起不来;
* 因此用 ``importlib.import_module`` 按需加载,失败时抛清晰的 ``ImportError``。
"""

from __future__ import annotations

import importlib
from typing import Any

from finboard_broker.base import BrokerAdapter
from finboard_broker.mock import MockBroker
from finboard_shared.types import BrokerKind

#: kind → 实现 package 的 module path 映射
_BROKER_MODULES: dict[BrokerKind, str] = {
    BrokerKind.MOCK: "finboard_broker.mock",
    BrokerKind.QMT: "finboard_broker_qmt.adapter",
    BrokerKind.CTP: "finboard_broker_ctp.adapter",
}

#: 每个实现包对外暴露的工厂函数名(签名:``create_broker() -> BrokerAdapter``)
_BROKER_FACTORY = "create_broker"


def create_broker(kind: BrokerKind | str, **kwargs: Any) -> BrokerAdapter:
    """按 ``kind`` 实例化一个 ``BrokerAdapter``。

    ``kwargs`` 透传给实现包的 ``create_broker()`` 工厂函数;
    MockBroker 不接受参数。
    """
    resolved = BrokerKind(kind) if not isinstance(kind, BrokerKind) else kind

    if resolved is BrokerKind.MOCK:
        return MockBroker()

    module_path = _BROKER_MODULES.get(resolved)
    if module_path is None:
        raise ValueError(f"未知的 broker 类型: {kind!r}")

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:  # pragma: no cover - 实盘环境才能触发
        raise ImportError(
            f"无法加载 broker 实现 {module_path}: {exc}. "
            f"请确认已安装 {resolved.value} 所需的券商 SDK,且运行环境支持."
        ) from exc

    factory = getattr(module, _BROKER_FACTORY, None)
    if factory is None:  # pragma: no cover - 防御性
        raise RuntimeError(
            f"{module_path} 未暴露 {_BROKER_FACTORY}() 工厂函数"
        )
    result = factory(**kwargs)
    # 实现包返回 Any,这里收敛到 BrokerAdapter 契约
    assert isinstance(result, BrokerAdapter)
    return result
