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
    """双均线交叉策略参数。"""

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

    @model_validator(mode="after")
    def validate_windows(self) -> MaCrossParams:
        if self.short_window >= self.long_window:
            raise ValueError("短期均线周期必须小于长期均线周期")
        return self
