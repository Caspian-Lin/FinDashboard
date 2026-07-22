"""broker factory 单元测试。"""

from __future__ import annotations

import pytest

from finboard_broker import MockBroker, create_broker
from finboard_broker.base import BrokerAdapter
from finboard_shared.types import BrokerKind


@pytest.mark.unit
def test_create_mock_broker() -> None:
    broker = create_broker(BrokerKind.MOCK)
    assert isinstance(broker, MockBroker)
    assert broker.kind is BrokerKind.MOCK


@pytest.mark.unit
def test_create_mock_by_string() -> None:
    broker = create_broker("mock")
    assert isinstance(broker, MockBroker)


@pytest.mark.unit
def test_create_mock_ignores_kwargs() -> None:
    broker = create_broker("mock", path="/tmp", session_id="1")
    assert isinstance(broker, MockBroker)


@pytest.mark.unit
def test_unknown_broker_kind() -> None:
    with pytest.raises((ValueError, KeyError)):
        create_broker("unknown_broker")


@pytest.mark.unit
def test_create_broker_returns_broker_adapter() -> None:
    broker = create_broker("mock")
    assert isinstance(broker, BrokerAdapter)


@pytest.mark.unit
def test_qmt_without_xtquant_raises_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """无 xtquant 环境下 QMT create_broker 应抛清晰的 ImportError。"""
    import finboard_broker_qmt.adapter as qmt_adapter

    monkeypatch.setattr(qmt_adapter, "XTQUANT_AVAILABLE", False)
    with pytest.raises(ImportError, match="xtquant"):
        qmt_adapter.create_broker(path="/tmp", session_id="1")


@pytest.mark.unit
def test_ctp_create_broker_returns_adapter() -> None:
    """CTP 占位实现可实例化(方法未实现但不影响构造)。"""
    broker = create_broker("ctp")
    assert broker.kind is BrokerKind.CTP
