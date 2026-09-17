# test_releases.py 在 coverage 下偶发失败(非回归信号)

## 主题
`tests/unit/data/test_releases.py` 存在两个对文件系统时序敏感的用例,
在 **coverage 追踪开启**时偶发失败,`--no-cov` 下稳定通过;基线即有,与业务
改动无关。

## 结论 / 事实
- `test_parallel_publish_is_deterministic_and_equal_to_serial[4]`:2026-08-17
  在本机(Windows)全量单测中偶发失败,同一 commit 重跑结果不稳定;在基线
  `ace7ed5`(PR #191 之前)上同样复现,证实非该 PR 引入。
- `test_failed_release_preserves_previous_and_cleans_staging`:2026-08-17 在
  GitHub Actions runner 上偶发(push 事件 run 31960563915,staging 目录清理
  断言失败),**rerun 后通过**。
- 共性:并行发布 / 临时目录清理类断言,coverage 的 tracer 拖慢执行、放大竞态。

## Why
避免把这两个用例的偶发失败误判为回归去改业务代码,或阻塞无关 PR 的合并。

## How to apply
- 遇到这两个用例失败:先 `--no-cov` 或单独重跑复现判定;确认 flaky 后直接重跑。
- CI 因它们挂掉时 rerun failed jobs 即可,不需要改代码。
- 若未来要治本,方向是给发布路径的 staging 清理与并行写入补确定性(显式
  同步 / 事件驱动断言),而不是放宽断言。
