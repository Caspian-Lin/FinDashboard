"""全局配置(pydantic-settings)。

* 从环境变量(``FINBOARD_`` 前缀)与 ``.env`` 文件加载;
* broker 凭据属于敏感字段,**禁止**写入日志(见 :mod:`finboard_app.logging`);
* ``BrokerKind`` 等枚举字段直接解析为 enum,避免在业务层到处 ``BrokerKind(s)``。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from finboard_shared.types import BrokerKind, KillSwitchLevel


def _strip_empty_values(data: Any) -> Any:
    """把空字符串值视为「未设置」,从输入中删除,使其回落到字段默认值。

    pydantic-settings 默认会把 ``FOO=`` 解析为 ``""`` 并作为显式值覆盖默认值,导致
    int/float 字段抛 ``ValidationError``。前端设置页或手动编辑把某数值字段清空保存时
    会触发此问题。删除键后,pydantic 会使用字段声明里的默认值。
    """
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if not (isinstance(v, str) and v.strip() == "")}
    return data


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
    # 主源默认 tushare(issue #393):股票 daily+adj_factor+suspend_d 路由
    # 已就绪(#341 实测 2000 积分档可调),akshare 降级为副源(回落链语义
    # 保持 #257:primary 空结果 / 异常才回退)。回退默认 akshare:tushare
    # 不覆盖的 ETF / 期货(#341/#267)由 akshare 兜底。
    data_provider: Literal["akshare", "yfinance", "tushare"] = "tushare"
    data_fallback_provider: Literal["akshare", "yfinance", "tushare"] | None = "akshare"
    tushare_token: str = Field(default="", repr=False)
    tushare_requests_per_minute: int = 200
    tushare_daily_request_limit: int = 100_000
    tushare_usage_file: str = "data_cache/tushare_usage.json"
    # ParquetCache 进程内读缓存的近似字节上限(issue #440):默认 None = 不限
    # (行为与 issue #287 元素数上限一致)。条目按「元素数 x 每元素实测平均
    # 字节」近似计量,非精确深度量;超预算按 LRU 从最旧淘汰,单条目超预算
    # 不缓存。全市场日频工作集可到数百 MB,长 run 建议设 512MB(536870912)。
    read_cache_max_bytes: int | None = None
    # 研究特征快照的跨标的读取并发;不影响实盘交易线程。
    feature_snapshot_max_concurrency: int = Field(default=8, ge=1, le=64)
    # 特征快照使用的独立计算进程数;0 表示只使用旧的进程内 worker。
    feature_snapshot_process_workers: int = Field(default=8, ge=0, le=64)
    # research_run 多期回放逐期价格特征重算使用的独立计算进程数(issue #288);
    # 池在一次加载期内常驻并复用(max_tasks_per_child 摊销 Windows spawn 开销),
    # 0 表示沿用进程内协程池(单核,旧行为)。只影响研究/回测域。
    research_price_feature_process_workers: int = Field(default=4, ge=0, le=64)

    # ---- 回测 strategy 形态异步化(issue #189) ----
    # backtest_run(strategy 形态)自动切换异步的估算工作量阈值:工作量 ≈
    # 标的不数 x 估算交易日(start~end,周末 5/7 折算)。达到阈值自动入队
    # kind=backtest_run 后台任务并返回 job_id,避免 MCP 客户端超时后响应丢失
    # (实测约 10ms/段,30s 客户端超时 ≈ 3000 段;默认 15000 ≈ 60 标的 x 一年)。
    # 0 表示禁用自动切换(仅 run_async=true 显式异步)。
    backtest_auto_async_symbol_days: int = Field(default=15000, ge=0)

    # ---- 统一后台任务队列 worker(issue #117 / #142) ----
    # 独立进程 ``finboard worker`` 用 FOR UPDATE SKIP LOCKED 领取任务。
    # 不影响实盘交易线程,仅服务研究/数据域耗时任务。
    worker_poll_interval_seconds: float = Field(default=2.0, gt=0)
    worker_max_concurrent: int = Field(default=4, ge=1, le=32)
    worker_lease_timeout_seconds: float = Field(default=600.0, gt=0)
    worker_heartbeat_interval_seconds: float = Field(default=30.0, gt=0)
    # 周期性维护间隔:回收过期租约 + 重试任务自动重排(issue #161)。
    worker_maintenance_interval_seconds: float = Field(default=10.0, gt=0)
    # retry_waiting / interrupted 自动重排前的退避秒数(issue #161)。
    worker_retry_backoff_seconds: float = Field(default=30.0, gt=0)
    worker_queues: str = Field(
        default="",
        description="逗号分隔的逻辑队列白名单;空字符串表示消费全部队列",
    )
    # worker 进程数(issue #286):默认 1 保持单进程行为;>1 时 ``finboard worker
    # run`` 作为父进程拉起 N 个独立 worker 子进程(各自 engine / session_maker /
    # worker_id),把 GIL 边界从 1 核扩到 N 核。kind_concurrency 由 claim_next
    # 的 SQL 层约束跨进程生效。回滚 = --workers 1(默认)+ revert。
    worker_processes: int = Field(default=1, ge=1, le=64)
    # worker 停机优雅宽限秒数(issue #307):收到停止信号后,worker 先等
    # in-flight 任务完成至该上限、超时才取消(取消后任务由 lease 过期回收 →
    # interrupted → 退避重排兜底,与既有语义一致)。默认 0 = 立即取消 in-flight
    # (即时停机,行为零回归)。``finboard dev`` 与 ``--workers N`` supervisor
    # 的子进程收敛宽限同读此值;等待中第二次停止信号立即强退(退出码 130)。
    worker_shutdown_grace_seconds: float = Field(default=0.0, ge=0)
    # 僵尸无进展检测阈值(issue #306):heartbeat 正常续租但 progress_done/phase
    # 持续无变化超过该秒数 → job 具名失败 zombie_no_progress 并转 retry_waiting
    # 自动重试。默认 3600s(1 小时)刻意宽松 —— 研究加载期分块探针、数据摄取
    # 逐 symbol 进度、回测引擎至少每小时推进一次;RR-7a74 类「心跳续租型僵尸」
    # 7.5h 才被人肉发现,1h 阈值已能兜底且远离健康长任务的误杀线。0 = 关闭。
    worker_zombie_no_progress_seconds: float = Field(default=3600.0, ge=0)

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

    # ---- 研究代码仓库(issue #215)----
    # agent 提交的策略/因子代码存放的本地 bare git 仓库路径(暂不推远端;
    # 远端备份作为后续可选项)。只存储与版本化,不执行任何代码。
    research_code_repo_path: str = "data_cache/research_code.git"
    # 单次提交文件数 / 单文件字节数上限(静态校验,纵深防御第一层)。
    research_code_max_files: int = Field(default=32, ge=1)
    research_code_max_file_bytes: int = Field(default=262144, ge=1024)

    # ---- 研究代码沙箱(issue #216)----
    # 一次性 Docker 容器执行 agent 因子代码(单形态:开发/生产统一 Docker,
    # 本机前置要求 Docker Desktop)。默认关闭 —— 回滚 = 关闭入口,已归档的
    # run 记录与 workspace 文件不受影响。容器无网络无凭证,纯离线研究域。
    research_sandbox_enabled: bool = False
    # 沙箱镜像(tag 与 finboard-research-kit 版本绑定,构建见
    # docker/research-sandbox/;run 记录同时归档镜像 digest)。
    research_sandbox_image: str = "finboard-research-sandbox:0.3.2"
    # 整跑墙钟超时(秒),超时 docker kill;内存 MB(memory-swap 同值禁 swap);
    # CPU 配额;进程数上限;容器内非 root uid(镜像内 sandbox 用户)。
    # 容量规划(issue #374):容器内存 ≈ 2GB 基线 + 挂载行数 x ~0.3KB(pandas
    # 整表加载对象开销)——4096 档覆盖全市场 daily_metrics ~18 个月窗口
    # (≈700 万行,2026-09-07 BJ-CF3D90915A75443C 在 2048 档实测 OOM);
    # 更长窗口(如 3 年 ≈ 1100 万行)经 env 提到 8192。无 swap,并发 build
    # 时并发数 x 本值须 ≤ Docker Desktop WSL2 可用内存。
    research_sandbox_timeout_seconds: float = Field(default=600.0, ge=1.0)
    research_sandbox_memory_mb: int = Field(default=4096, ge=256)
    research_sandbox_cpus: float = Field(default=2.0, ge=0.5)
    research_sandbox_pids_limit: int = Field(default=256, ge=16)
    research_sandbox_user: str = "65532"
    research_sandbox_docker_bin: str = "docker"
    # run workspace 根目录:<root>/<RCR-id>/{code,data,out,stdout.txt,...}。
    research_sandbox_workspace_root: str = "data_cache/research_sandbox"
    # 输出质量门(issue #217):NaN 比例上限 / 覆盖率下限,不合格拒绝把
    # scores 落库为 feature snapshot(run failed=quality_gate_failed,
    # 错误信息指明阈值)。
    research_sandbox_max_nan_ratio: float = Field(default=0.5, ge=0.0, le=1.0)
    research_sandbox_min_coverage: float = Field(default=0.5, ge=0.0, le=1.0)
    # 研究代码晋级门(issue #219):screen 指标与 #57 OOS 验证共同决定
    # promotion_status=passed;实际阈值随 artifact evidence 冻结。
    research_promotion_min_abs_rank_ic: float = Field(default=0.02, ge=0.0)
    research_promotion_max_average_turnover: float = Field(default=0.80, ge=0.0, le=1.0)
    research_promotion_max_abs_correlation: float = Field(default=0.80, ge=0.0, le=1.0)
    research_promotion_min_periods: int = Field(default=2, ge=1)

    # ---- FinBoard MCP Server(issue #108)----
    # 向外置研究 Agent(OpenCode)暴露受控研究工具的 MCP 服务。默认关闭,显式启用。
    # 不连接实盘账户 / 订单 / 持仓;实盘能力永久不注册为工具。
    mcp_enabled: bool = False
    mcp_transport: Literal["stdio", "streamable-http", "sse"] = "stdio"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8765
    # HTTP 传输(``streamable-http``/``sse``)的 Bearer token 鉴权。
    # stdio 模式忽略(本机子进程接入无需鉴权);HTTP 模式必须设置,空值拒绝启动(防裸奔)。
    # 敏感字段:不进入日志 / 审计 / Provenance(AGENTS.md 红线)。
    mcp_auth_token: str = ""
    # 只读模式:禁用所有写工具(审批门也不开放),只暴露查询与 AI 问答。
    mcp_readonly_only: bool = False
    # 审计是否额外持久化到研究审计表(默认仅结构化日志 + 内存副本)。
    mcp_audit_persist: bool = False

    # ---- OpenCode 研究运行时接入(issue #109)----
    # OpenCode 作为外置研究 Agent 运行时,通过 finboard-mcp 受控工具访问研究能力。
    # 默认关闭;启用时需要先 ``opencode serve`` 并配置 base_url。
    # 不连接实盘账户 / 订单 / 持仓;内置 bash/edit/write 工具默认拒绝。
    opencode_enabled: bool = False
    opencode_base_url: str = "http://127.0.0.1:4096"
    opencode_api_prefix: str = "/api"
    opencode_default_agent: str = "finboard-researcher"
    opencode_request_timeout_seconds: float = 30.0

    # ---- OpenCode Web 研究工作台(issue #118;#157 移除 basic auth)----
    # FinBoard 网关托管一个容器级隔离的 ``opencode web`` 实例,前端 iframe 跨源嵌入。
    # 这是控制面网关:只签发访问信息 + 管理容器生命周期,不透传 OpenCode 流量、不
    # 连接实盘。``opencode_web_enabled`` 是总开关(默认关闭 → 网关返回 503,前端隐藏入口)。
    # #157 用户决策:单用户模型下 OpenCode Web 不启用 basic auth,直接使用明文
    # ``http://127.0.0.1:{port}`` URL;127.0.0.1 绑定是唯一网络边界。
    opencode_web_enabled: bool = False
    # FinBoard 是否托管子进程(True=自动启动/停止 opencode web;False=外部已启动,仅连接)。
    opencode_manage_process: bool = False
    # OpenCode 可执行文件路径(版本锁定;CI / 生产可指向固定版本二进制)。
    opencode_binary: str = "opencode"
    # Web 实例端口(默认 4097,与 ``opencode serve`` 的 4096 区分)。
    opencode_web_port: int = 4097
    # 监听地址:进程级隔离强制 127.0.0.1,不暴露公网。
    opencode_web_hostname: str = "127.0.0.1"
    # 允许跨源访问的浏览器源(逗号分隔);iframe 嵌入必须显式允许 FinBoard 源。
    # 例:"http://localhost:5173,http://localhost:8000"
    opencode_web_cors_origins: str = ""
    # 宿主机侧工作目录(仓库根;含 ``.opencode`` / ``.agents``,bind mount 进容器)。
    opencode_workdir: str = "."
    # 研究记录目录(仓库 ``docs/research/`` 三件套 + rounds/,只读暴露给前端,
    # 见 finboard_api.routes.research_docs;canonical 仍是仓库文件)。
    research_docs_dir: str = "docs/research"
    # 子进程日志路径。
    opencode_log_path: str = ".opencode/logs/opencode-web.log"
    # 额外注入子进程的环境变量(LLM provider Key 等;逗号分隔 KEY=VAL)。
    # 严格白名单继承:FinBoard 的 DB 密码 / broker 凭证永不传入 OpenCode 子进程。
    opencode_env_overrides: str = ""

    # ---- OpenCode Web Docker 隔离(issue #xxx,基于 #118)----
    # OpenCodeProcessManager 托管一个进程级隔离的 Docker 容器(而非宿主机子进程),
    # 实现与会话 / auth.json / 版本彻底隔离。需要 Docker Desktop 运行。
    # Docker 镜像(官方 ``ghcr.io/anomalyco/opencode``;旧 ``ghcr.io/sst/opencode`` 已废弃)。
    # 可指向衍生镜像 ``finboard-opencode-agent:<版本>``(官方镜像 + jq/python3,
    # issue #313);启用 / 升级 / 回滚步骤见 docs/opencode-agent-image.md。
    opencode_image: str = "ghcr.io/anomalyco/opencode:latest"
    # 固定容器名(便于 stop / logs / inspect)。
    opencode_container_name: str = "finboard-opencode-web"
    # 容器内 opencode 连接宿主机 finboard_mcp 的 URL(跨容器 → host.docker.internal)。
    # #157:该配置在容器启动时渲染进 ``.opencode/runtime/opencode.json`` 并以单文件
    # bind mount 覆盖容器内 opencode.json —— 修改它即改变容器实际连接的 MCP 地址。
    opencode_mcp_remote_url: str = "http://host.docker.internal:8765/mcp"
    # 是否在 ``opencode_web_enabled`` 时由 API lifespan 内嵌启动 finboard-mcp HTTP
    # server(复用 ``mcp_host``/``mcp_port``/``mcp_auth_token``,uvicorn 后台任务)。
    # 默认开启:容器内 opencode 连 ``host.docker.internal:8765`` 时无需用户手动跑
    # ``python -m finboard_mcp``。设 False 回退到独立进程模式(向后兼容)。
    # 注意:容器经 host.docker.internal 访问宿主机,要求 MCP 绑 0.0.0.0 —— web
    # 容器模式下若 ``mcp_host`` 仍为默认 127.0.0.1,lifespan 会自动改绑 0.0.0.0
    # (显式配置了其它地址则尊重用户配置)。
    opencode_embed_mcp: bool = True
    # 容器启动渲染 runtime opencode.json 时,是否对声明 baseURL 的 config provider
    # 拉取 OpenAI 兼容 /models 并把新模型 id 只增不改合并进渲染产物(#242)。
    # 结果缓存 .opencode/runtime/provider-model-cache.json,拉取失败回退缓存;
    # baseURL/auth 永以仓库文件为准。设 False 渲染期不发起任何网络请求(回滚开关)。
    opencode_model_sync_enabled: bool = True

    def opencode_web_cors_origin_list(self) -> list[str]:
        """解析逗号分隔的 CORS 源列表。"""
        if not self.opencode_web_cors_origins.strip():
            return []
        return [
            origin.strip() for origin in self.opencode_web_cors_origins.split(",") if origin.strip()
        ]

    def opencode_env_override_map(self) -> dict[str, str]:
        """解析逗号分隔的 KEY=VAL 环境变量覆盖。"""
        result: dict[str, str] = {}
        for item in self.opencode_env_overrides.split(","):
            item = item.strip()
            if not item or "=" not in item:
                continue
            key, _, value = item.partition("=")
            key = key.strip()
            if key:
                result[key] = value
        return result

    @model_validator(mode="before")
    @classmethod
    def _drop_empty_str_fields(cls, data: Any) -> Any:
        """空字符串视为未设置,从输入删除后回落到字段默认值(见模块级说明)。"""
        return _strip_empty_values(data)


def load_settings(env_file: str | None = None) -> Settings:
    """加载配置;测试中可指定独立 env_file。"""
    if env_file is not None:
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    return Settings()


#: psycopg(libpq)连接黑洞加固参数(#450)。WSL2 NAT 转发下的长驻连接池
#: 实测会被静默黑洞化(对端无 RST,本地 send 成功、recv 永不返回):worker
#: 事件循环上的任务全体冻结在 ``await`` 上、心跳停摆、相位不动,僵尸检测与
#: 协作取消同循环同死。TCP keepalive 让死连接在 ~keepalives_idle + count x
#: interval(默认 ~60s)内显式报错,由调用方(逐操作短会话等)按连接级
#: 失败处理;非 postgres 驱动(sqlite 等)返回空 dict 不影响测试。
def postgres_connect_args(db_url: str) -> dict[str, int]:
    if not db_url.startswith(("postgresql://", "postgresql+psycopg://", "postgres://")):
        return {}
    return {
        "connect_timeout": 10,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
    }
