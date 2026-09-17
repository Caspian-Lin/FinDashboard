"""内置策略的类型化参数 schema。

这里是策略参数的单一真值来源:

* :func:`finboard_app.strategies.create_strategy` 在实例化前校验参数;
* API 从 Pydantic JSON Schema 自动生成前端表单元数据;
* 策略预设保存前复用同一套校验。

用户提交的 Python 代码不属于本模块能力范围。注册表只包含随应用发布、经过测试的
内置策略类。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finboard_shared.types import OrderType


class StrategyParams(BaseModel):
    """所有内置策略参数模型的基类。"""

    model_config = ConfigDict(extra="forbid")


class PeriodicQueryParams(StrategyParams):
    """定时查询策略无可配置参数。"""


class EtfDcaParams(StrategyParams):
    """ETF 定额定投策略参数。"""

    symbol_code: str = Field(
        title="定投标的",
        min_length=1,
        max_length=32,
        description="定投标的代码,例如 510300.SH",
    )
    quantity: Decimal = Field(
        title="买入数量",
        gt=0,
        description="每次买入数量;A 股通常应为 100 股的整数倍",
    )
    order_type: OrderType = Field(
        title="委托类型",
        default=OrderType.MARKET,
        description="委托类型",
    )
    price: Decimal | None = Field(
        title="限价",
        default=None,
        gt=0,
        description="限价单价格;市价单留空",
    )

    @model_validator(mode="after")
    def validate_limit_price(self) -> EtfDcaParams:
        if self.order_type is OrderType.LIMIT and self.price is None:
            raise ValueError("限价单必须填写价格")
        return self


class MaCrossParams(StrategyParams):
    """双均线交叉策略参数。

    动态选股(``universe_*`` 字段)是策略层的轻量 Bar 规则,只缩小回测请求的
    静态候选池,不能添加新标的或触发数据下载。默认 ``universe_mode="all"``
    保持现有策略行为;``liquidity_momentum`` 模式需要回溯窗口预热完成后才会
    让标的入选,且只读取已收到的当前/历史 Bar,不访问未来数据。
    """

    short_window: int = Field(
        title="短期均线",
        default=5,
        ge=1,
        le=250,
        description="短期均线周期",
    )
    long_window: int = Field(
        title="长期均线",
        default=20,
        ge=2,
        le=500,
        description="长期均线周期,必须大于短期均线",
    )
    max_position_pct: float = Field(
        title="最大仓位比例",
        default=0.95,
        gt=0,
        le=1,
        description="最大总仓位比例,0.95 表示最多使用 95% 权益",
    )
    symbol_code: str | None = Field(
        title="旧版单标的代码",
        default=None,
        description="兼容旧版单标的配置;多标的策略会忽略此参数",
        json_schema_extra={"ui_hidden": True},
    )
    universe_mode: Literal["all", "liquidity_momentum"] = Field(
        title="动态选股模式",
        default="all",
        description=(
            "all = 全部静态候选标的(默认,保持现有行为);"
            "liquidity_momentum = 按回溯窗口的平均成交额与区间动量过滤"
        ),
    )
    universe_lookback: int = Field(
        title="选股回溯窗口",
        default=20,
        ge=1,
        le=500,
        description="选股窗口长度;窗口填满前标的不入选,避免读取未来数据",
    )
    universe_min_avg_amount: Decimal | None = Field(
        title="最低平均成交额",
        default=None,
        ge=0,
        description=(
            "回溯窗口内的平均成交额下限;留空表示不校验。"
            "成交额缺失或为 0 时回退为 close * volume"
        ),
    )
    universe_min_momentum: Decimal | None = Field(
        title="最低区间动量",
        default=None,
        ge=-1,
        description=(
            "区间动量下限,等于 (close[-1] / close[-lookback]) - 1;"
            "留空表示不校验"
        ),
    )
    universe_exit_clear: bool = Field(
        title="退出清仓",
        default=False,
        description=(
            "开启后,标的发生 入选→退出 转换时通过策略上下文提交清仓卖出意图;"
            "卖出仍走完整 Risk → OrderManager → Broker 链路"
        ),
    )

    @model_validator(mode="after")
    def validate_windows(self) -> MaCrossParams:
        if self.short_window >= self.long_window:
            raise ValueError("短期均线周期必须小于长期均线周期")
        return self

    @model_validator(mode="after")
    def validate_universe(self) -> MaCrossParams:
        if self.universe_mode != "all" and self.universe_lookback < 1:
            raise ValueError("动态选股回溯窗口必须 >= 1")
        return self
