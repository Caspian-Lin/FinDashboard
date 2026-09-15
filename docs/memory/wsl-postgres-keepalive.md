# WSL PostgreSQL 空闲自动关机——本机集成测试「卡住」的根因

**主题**:本机(Windows)的 PostgreSQL 跑在 WSL 里,WSL2 VM 空闲约 60 秒自动关机,
导致 DB 相关测试表现为「卡住 / 超时」。

**事实**:`pg_isready` 在 WSL 内部探测的是 WSL 自己的 loopback,不能证明 Windows
侧可达;Windows→WSL 的 localhost TCP 端口转发**不会唤醒**已关机的 VM,psycopg
只会 ConnectionTimeout 长时间重试(2026-08-15 排查确认,曾误判为「契约测试在
数据库调用测试处超时」)。

2026-09-15 又确认一种独立形态:`networkingMode=mirrored` 下 WSL 内
`systemctl is-active postgresql` / `pg_isready` 均正常,但 Windows 侧
`127.0.0.1:5432` 仍超时且 PostgreSQL 看不到连接。此时不是数据库锁；须先正常
停止 PostgreSQL,执行 `wsl.exe --shutdown` 重建网络层,再启动发行版与 PostgreSQL。

**Why**:psycopg 默认没有快速失败,连接超时前看起来像卡死。

**How to apply**(跑集成测试前):

1. 唤醒:`wsl.exe -u root -e sh -lc "systemctl start postgresql; pg_isready -h 127.0.0.1 -p 5432"`
2. 保活(后台):启动隐藏的 `wsl.exe -d Ubuntu -- tail -f /dev/null` 长驻进程；固定
   `sleep 900` 只适合短测试,超时后 VM 仍会再次自动关机。
3. Windows 侧用 `.env` 里的真实密码验证 psycopg 可连,才算真的通。
4. 若 WSL 内健康而 Windows 侧仍超时:先 `systemctl stop postgresql`,再
   `wsl.exe --shutdown`;重启后重复第 1-3 步。不要用 WSL 内 `pg_isready` 代替
   Windows 侧验证。

**本机其它环境坑**(同一次排查发现):

- pytest `tmp_path` 权限问题(`pytest-of-<user>` 目录被锁,fixture setup 全体
  PermissionError):已根除 —— `pyproject.toml` addopts 固定
  `--basetemp .pytest-tmp -p no:cacheprovider`,临时目录在仓库内(已入
  .gitignore),**pytest 必须在仓库根目录运行**,不要手动加 `--basetemp`
  指向系统临时目录(见 AGENTS.md 测试章节)。
- `data_releases/` 下有锁定目录导致 `mypy .` 失败:改跑 `mypy packages/ tests/ migrations/`。
- Windows 上 `asyncio` 直接连 psycopg 需 `WindowsSelectorEventLoopPolicy`(否则
  Proactor 报错)。

相关:[[issue-157-status]]
