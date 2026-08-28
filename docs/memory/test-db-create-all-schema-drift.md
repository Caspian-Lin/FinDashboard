# 测试库 schema 漂移:create_all 不会 ALTER 已有表,加列须手动补

## 主题

`findashboard_test` 测试库的表结构管理与 alembic 迁移的关系(2026-08-29,issue #221 踩坑)。

## 结论 / 事实

- 集成测试的 engine fixture 用 `Base.metadata.create_all` 建表(见
  `tests/integration/test_background_job_persistence.py` 的 module fixture),
  `create_all` 只建缺失的表,**不会给已有表加新列**。
- 测试库没有 `alembic_version` 表(从未走过 alembic),因此对它跑
  `alembic upgrade head` 会从 base 开始建表,撞 `DuplicateTable: relation
  "accounts" already exists` 直接失败。
- ORM 模型加列(如 #221 的 `background_jobs.archived_at`)后,测试库不动的话,
  所有触碰该表的用例报 `psycopg.errors.UndefinedColumn`,一次挂几十个。

## Why

两套 schema 管理并存:开发/生产库走 alembic;测试库走 create_all + 逐用例清表。
create_all 的 checkfirst 语义只覆盖「表不存在」,列级变更完全不在其内。

## How to apply

- 模型加列后,测试库用 SQL 手动补列(与迁移语义一致),例如:

  ```python
  # psycopg 连 findashboard_test 后
  conn.execute("alter table background_jobs add column archived_at timestamptz null")
  ```

- 不要对测试库跑 alembic(没有 version 表,会从头建表报 DuplicateTable)。
- 密码在 `.env` 的 `FINBOARD_DB_URL`;shell 里 grep/cut/sed 提取容易被 CRLF 与
  `sed 's|/findashboard|...'` 误伤用户名(`//findashboard` 也命中模式),用
  Python `url.rsplit('/', 1)[0] + '/findashboard_test'` 最稳。
- 开发库(`findashboard`)正常走 `alembic upgrade head`;升降级可逆性可本地
  upgrade → downgrade -1 → upgrade 验证一轮。
