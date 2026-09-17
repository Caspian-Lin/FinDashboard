# 跨 issue 持久坑位速查（durable-gotchas）

主题：开发过程中反复适用、与单个 issue 绑定的记忆归档后仍需一处速查的持久坑位。

## 迁移

- 迁移 revision id 全局唯一，勿复用已占用号（c1d2e3f4a5b6 被 #215 占用，#383 曾险些复用）。新建迁移前先查 `packages/finboard-persistence/*/alembic/versions/`。
- `findashboard_test` 测试库无 `alembic_version`，勿直接 `alembic upgrade`（schema 由 conftest `create_all` 建）；模型加列后测试库须手动 ALTER 或 drop 表重建——详见 [test-db-create-all-schema-drift](test-db-create-all-schema-drift.md)。

## 测试

- `test_cli_dev` 是基线预置失败，勿追。
- pytest-timeout 杀进程会掩盖失败名，取证加 `-v`；conftest 已设 `lock_timeout` 防 DB 僵尸锁（见 [pg-lock-hygiene](pg-lock-hygiene.md)）。
- LogCapture 断言：python logging 对象无 `.warning` 属性可用（用 `caplog.records` / `caplog.text`）。
- job `phase` 文案是被测试锁定的隐性契约：收短只准去明细、必须保留具名标记。

## 类型与 lint

- CI mypy 也查 `tests/`，改测试文件须本地跑 `uv run mypy .`（本地单文件 mypy 上下文与 CI 全仓不同，type:ignore 判定可能不一致——用真身 + cast 替代鸭子类型假身）。
- 勿对既有文件跑 `ruff format`（会产生大量无关重排行）；hand-edit 后只跑 `ruff check`（CI 不跑 format）。
- ruff UP047 要求 PEP 695 泛型（`def f[T](...)`）；全角乘号 × 触发 RUF001-003。

## 序列化与 checksum

- `as_dict()` 新增可选键 None 省略输出，保旧 payload 字节稳定（manifest/checksum 不漂移）；语义变更走 `schema_version`，禁止直接改既有字段输出。

## kit 版本联动

- finboard-research-kit 版本 bump 五处联动：kit pyproject、Docker 镜像 tag、config 默认值、CI E2E 默认 tag、`uv.lock`（#376 曾漏 uv.lock）。

## git

- research code repo 的 git 读命令必须显式 `--git-dir`（否则落在本仓库 CWD）。

**Why:** 这些坑位跨 issue 反复出现；逐 issue 归档后仍需一处快速速查，避免每个新 issue 重新踩一遍。

**How to apply:** 开工前扫一眼相关类别；新坑位按类别追加并更新 README 索引；失效的条目删除（同时在 AGENTS.md「平台关键不变量」核实是否有对应条目需要调整）。
