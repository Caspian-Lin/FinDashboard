"""全局配置(pydantic-settings)。

* 从环境变量(``FINBOARD_`` 前缀)与 ``.env`` 文件加载;
* broker 凭据属于敏感字段,**禁止**写入日志(见 :mod:`finboard_app.logging`);
* ``BrokerKind`` 等枚举字段直接解析为 enum,避免在业务层到处 ``BrokerKind(s)``。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from finboard_shared.types import BrokerKind, KillSwitchLevel


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FINBOARD_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- 运行环境 ----
    env: Literal["dev", "staging", "prod", "ci"] = "dev"
    log_level: str = "INFO"
    log_json: bool = True

    # ---- 数据库 ----
    db_url: str = "postgresql+psycopg://findashboard:CHANGE_ME@127.0.0.1:5432/findashboard"
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # ---- 历史行情数据源 ----
    data_provider: Literal["akshare", "yfinance", "tushare"] = "akshare"
    data_fallback_provider: Literal["akshare", "yfinance", "tushare"] | None = None
    tushare_token: str = Field(default="", repr=False)
    tushare_requests_per_minute: int = 200
    tushare_daily_request_limit: int = 100_000
    tushare_usage_file: str = "data_cache/tushare_usage.json"
    # 研究特征快照的跨标的读取并发;不影响实盘交易线程。
    feature_snapshot_max_concurrency: int = Field(default=8, ge=1, le=64)
    # 特征快照使用的独立计算进程数;0 表示只使用旧的进程内 worker。
    feature_snapshot_process_workers: int = Field(default=8, ge=0, le=64)

    # ---- Broker ----
    broker: BrokerKind = BrokerKind.MOCK
    account_id: str = "test-account"

    # QMT
    qmt_path: str = ""
    qmt_session_id: str = ""

    # CTP
    ctp_front_addr: str = ""
    ctp_broker_id: str = ""
    ctp_user_id: str = ""
    ctp_password: str = Field(default="", repr=False)
    ctp_app_id: str = ""
    ctp_auth_code: str = Field(default="", repr=False)

    # ---- 风控(与 finboard_risk.config.RiskConfig 字段一致) ----
    risk_max_order_value: Decimal = Decimal("10000")
    risk_max_symbol_position_value: Decimal = Decimal("30000")
    risk_max_daily_buy_value: Decimal = Decimal("50000")
    risk_max_active_orders: int = 10
    risk_max_orders_per_minute: int = 5
    risk_allow_short: bool = False
    risk_allow_market_order: bool = False

    # ---- Kill Switch 初始态 ----
    kill_switch_initial: KillSwitchLevel = KillSwitchLevel.OFF

    # ---- AI 研究助手(issue #84) ----
    # 只服务研究与教育;不连接实盘账户 / 订单 / 持仓。
    # api_key 属于敏感字段,禁止进入日志 / 审计 / Provenance。
    llm_provider: Literal["fake", "openai_compatible"] = "fake"
    llm_base_url: str = ""
    llm_api_key: str = Field(default="", repr=False)
    llm_model: str = "gpt-4o-mini"
    # 流式请求连续没有真实 token 的空闲超时(兼容旧变量名)。
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 3
    llm_connect_timeout_seconds: float = 10.0
    llm_total_timeout_seconds: float = 600.0
    llm_max_tokens: int = 4096
    # OpenAI-compatible provider 中,DeepSeek 使用 thinking;其它 provider 可关闭。
    llm_thinking_enabled: bool = True
    llm_reasoning_effort: Literal["high", "max"] = "high"


def load_settings(env_file: str | None = None) -> Settings:
    """加载配置;测试中可指定独立 env_file。"""
    if env_file is not None:
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    return Settings()
