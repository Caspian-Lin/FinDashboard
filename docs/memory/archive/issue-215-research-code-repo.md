# Issue #215:研究代码仓库与服务端提交工具

- 主题:L3 路线第一环 —— agent 代码入口(bare git 仓库 + MCP 受控提交,只存不执行)
- 日期:2026-08-29(分支 `feat/research-code-repo-215`,PR 见 issue #215)

## 结论 / 事实

- 服务分层:`finboard-backtest/research_code/`(git_repo.py=bare 仓库封装,
  validation.py=AST 静态校验,__init__.py=ResearchCodeService 门面)+
  `finboard-persistence/research_code_repo.py`(research_code_artifacts 登记簿)
  + `finboard-mcp/tools/research_code.py`(4 工具:submit/rollback 写 +
  list/get 只读)。rollback 是额外加的第 4 个工具(超出 issue 列的 3 个),
  为满足「可回滚到旧 commit 引用」验收项。
- 每次 submit = 追加一行 active 登记 + 同名旧 active 行置 retired;
  rollback = 把历史 commit 重新登记 active(git 历史不重写)。

## Why(踩坑)

1. **git 子进程必须显式 `--git-dir`**:读类命令(ls-tree/log/diff/show)若
   只写 `git ls-tree main:path`,会在**调用方进程 CWD**(FinBoard 自身就是
   git 仓库!)执行,读到错误仓库且部分命令不报错(log 返回空)。写路径
   (clone→commit→push)不受影响。
2. **本机 core.autocrlf**:git 调用统一加 `-c core.autocrlf=false`,避免
   内容 checksum 与提交内容漂移。
3. **`git log <tree-ish>` 不是路径历史**:`git log main:factors/x` 不报错但
   返回空;路径历史须 `git log main -- factors/x`。
4. **repository 返回冻结 dataclass 快照**:`register()` 返回 flush 时的
   快照对象,后续 UPDATE(置 retired)不会反映到已返回的 dataclass ——
   调用方要重查。ORM 侧 update 加了 `synchronize_session="fetch"` 也只
   同步 Model,不同步 dataclass。
5. OpenCode allowlist 无需改:已是 `finboard_*: allow` 通配,新工具自动放行。

## How to apply

- 后续沙箱执行 issue 直接复用 `ResearchCodeService.read(kind, name, commit)`
  取指定版本源码;IMPORT_WHITELIST 应与沙箱容器可用包集合保持镜像
  (白名单清单文档化在 Skill `references/tools.md` #215 节)。
- 新增研究写工具照抄本 issue 模式:handler 内先 `_require_write_enabled`,
  业务校验错误抛 `McpToolError("invalid_argument", 逐条可操作问题)`。
