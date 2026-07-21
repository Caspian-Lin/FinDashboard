"""Alembic 环境入口。

* **同步**模式运行(alembic 命令本身是同步进程),driver 用 psycopg v3 的 sync 接口;
* ``sqlalchemy.url`` 被 ``FINBOARD_DB_URL`` 环境变量覆盖;若环境变量使用
  ``postgresql+asyncpg`` 方言,自动改写成 ``postgresql+psycopg`` 以走 sync 路径;
* ``target_metadata`` 指向 :mod:`finboard_persistence.base.Base.metadata`,
  因此必须导入 :mod:`finboard_persistence.models` 让所有表注册到 metadata。
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 让 alembic 能 import 到 workspace 内的包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "finboard-persistence" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "finboard-shared" / "src"))

# 关键修复:alembic 进程本身不读 .env(pydantic-settings 才读),
# 这里用 python-dotenv 显式加载,让 FINBOARD_DB_URL 等变量进入 os.environ。
# override=False:已有真实环境变量(如 CI)优先于 .env 文件。
try:
    from dotenv import load_dotenv

    _repo_root = Path(__file__).resolve().parent.parent
    load_dotenv(_repo_root / ".env", override=False)
except ImportError:  # pragma: no cover - pydantic-settings 间接依赖,缺失说明环境异常
    pass

from finboard_persistence import models  # noqa: F401  必须导入以注册表
from finboard_persistence.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

_env_url = os.getenv("FINBOARD_DB_URL")
if _env_url:
    # alembic 走同步 driver,把 asyncpg 路径改回 psycopg v3 sync
    _env_url = _env_url.replace("postgresql+asyncpg", "postgresql+psycopg")
    config.set_main_option("sqlalchemy.url", _env_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
