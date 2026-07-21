# finboard-app

进程入口 + CLI + 配置加载 + 依赖注入组装根(composition root)。

## 入口

```bash
uv run finboard --help        # 查看 CLI 子命令
uv run finboard run            # 启动交易内核
uv run finboard reconcile      # 执行一次本地 ↔ 券商核对
uv run finboard kill-switch LEVEL  # 激活 Kill Switch
uv run finboard migrate        # 应用 alembic 迁移
```

## 配置

所有配置走环境变量(``FINBOARD_`` 前缀),支持 ``.env`` 文件加载。
字段清单见 :class:`finboard_app.config.Settings` 与仓库根 ``.env.example``。

## 组装

依赖组装集中在 :mod:`finboard_app.bootstrap`:

* ``Settings`` → ``AsyncEngine`` + ``session_maker``
* ``Settings.broker`` → ``BrokerAdapter``(经 :func:`finboard_broker.create_broker`)
* ``RiskConfig`` + ``KillSwitch`` → ``PreTradeChecker``(实现 ``RiskChecker``)
* 上述 + repos → ``TradingKernel``
