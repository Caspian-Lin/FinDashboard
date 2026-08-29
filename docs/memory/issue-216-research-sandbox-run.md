# issue #216 研究代码沙箱执行器(一次性 Docker 容器)

- 主题: L3 沙箱第二环——已提交因子代码「能跑且跑不坏」
- 日期: 2026-08-29
- 结论: 全链路已落地(kit 包 + data_mount + runner + executor + 表/迁移 + 2 个 MCP
  工具 + CI 镜像 job + 单测/集成/门控 E2E),`research_sandbox_enabled` 默认 false。

## 结论 / 事实

- 单形态(用户决策 2026-08-28):开发/生产统一 Docker 一次性容器,不做子进程降级。
- PIT 物理隔离:data_mount 在**服务端**按 decision_at 物化只读挂载,容器内不存在
  未来数据文件;provider PIT 门控之上再设防线——任何数据日期 > decision_at 当日
  即 SandboxMountError fail-closed(测试用 gate_slack_days 模拟 provider 缺陷)。
- /out 用 **bind mount 而非 tmpfs**(与 issue 原文措辞不同,PR 已说明):tmpfs 在容器
  停止后内容丢失,`docker cp` 取不到输出;bind + 根 read-only 达成同一隔离性质
  (输出仅出现在 /out),且容器结束后产物可归档。
- 镜像 tag 与 `finboard-research-kit.__version__` 绑定(0.1.0);数值栈 pinned 与
  uv.lock 对齐(pandas 3.0.5 / numpy 2.5.1 / pyarrow 25.0.0 / polars 1.44.1),
  kit 用 `--no-deps` 安装防 pip 重解析漂移;run 记录另存镜像 digest。
- 失败分类:static_validation_failed / runtime_error / timeout / oom_killed /
  output_contract_violation / sandbox_unavailable(125/126/127 docker 自身错误,
  可重试);超时/OOM 由 runner 轮询 `docker inspect` State(OOMKilled/ExitCode)+ 运行
  期 `docker stats` 采样峰值内存/CPU 归档。
- E2E 门控 `FINBOARD_SANDBOX_E2E=1`(QMT 同款先例),镜像 tag 可用
  `FINBOARD_SANDBOX_IMAGE` 覆盖;CI 只构建镜像 + 冒烟,不跑 E2E。

## Why

- issue AC 要求「挂载清单不含 decision_at 之后的数据文件」是数据面断言,真正防
  前视的是物理隔离(容器里根本没有未来文件),不是运行时检查——所以防线放在
  挂载生成端而不是容器内。
- v1 只允许 active 引用执行(指定 commit ≠ active 先 rollback),让三向引用里的
  commit 与登记表 active 行恒一致,避免「跑了未登记版本」的审计缺口。

## How to apply

- **Windows 子进程陷阱(必读)**:FinBoard 全局在 win32 设
  `WindowsSelectorEventLoopPolicy`(psycopg 异步驱动要求),而 Selector 循环在
  Windows **不支持** `asyncio.create_subprocess_exec`(直接 NotImplementedError)
  ——docker CLI 调用必须 `subprocess.run` + `asyncio.to_thread`(SubprocessDockerDriver
  即如此实现)。worker / 测试里任何要起子进程的新代码同理。
- 调试沙箱:先 `docker build -f docker/research-sandbox/Dockerfile -t
  finboard-research-sandbox:0.1.0 .`,再 `FINBOARD_SANDBOX_E2E=1 uv run pytest
  tests/integration/test_research_sandbox_docker_e2e.py -v`;本机 Docker Hub 直连
  不通,base 镜像走 `docker pull docker.m.daocloud.io/library/python:3.12-slim`
  再 retag;out_dir 需容器 uid 65532 可写(runner 已 chmod 777,Docker Desktop
  文件共享默认放行)。2026-08-29 本机 6/6 E2E 全过。
- 改 kit 代码:同步 bump `__version__` + settings 默认 `research_sandbox_image` tag
  + Dockerfile `KIT_VERSION` 三处(单测断言 metrics.kit_version 自报)。
- harness 加载入口模块用 `importlib.util.spec_from_file_location`,不要
  `sys.path + import_module`——同进程多次运行会命中 sys.modules 缓存,跨运行污染
  (单测踩过:第二个因子测试拿到第一个模块)。
- pandas 3.0 注意:`Series.reset_index(names=...)` 不存在,用
  `rename("score").rename_axis("symbol").reset_index()`;`astype(copy=False)` 已弃用。
