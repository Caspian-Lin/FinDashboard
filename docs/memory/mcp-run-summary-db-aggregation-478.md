# MCP run 摘要查询 13GB 内存飙升:全量物化 artifacts 根因与数据库侧聚合修复(#478)

**日期**:2026-09-15(事故复现与修复同日,issue #478)

## 主题 / 结论

OpenCode agent 用 `finboard_run_get(view=summary)` 查询真实多期运行
`RR-169678a2196b8842ac8f28fb`(7203 artifacts / ≈5.9GB 逻辑 JSON,rejected
multi_factor)时报 MCP error -32001;FinDashboard API/MCP Python 进程从约
170MB 涨到 **13.27GB**,PostgreSQL 活动查询可见
`research_run_artifacts.payload` 全列读取,终止进程后系统立即回落约 13GB。

根因:#206 的摘要实现把「聚合」放在 Python —— `get_run(view=summary)` 无条件
调 `repo.list_artifacts()`,`scalars().all()` 把全部 artifact 行连同 payload
JSON 反序列化进一个 list,再逐行计数。此前几轮内存优化(#458 报告护栏 /
#463 流式化 / #470 artifact 瘦指纹)只覆盖研究运行内部收尾/恢复与报告导出
路径,**漏掉了 MCP 摘要查询这条消费路径**。

修复(issue #478):新增
`ResearchRunRepository.summarize_artifacts(run_id)` —— 三条 SQL 在库内聚合
(行数 count;候选池单次物化 CTE 算 total/included/excluded_by_reason;fills
按 `COALESCE(decision_id,'')` 分组),只返回有界计数字段
(`ResearchRunArtifactSummary`);`finboard_run_get(view=summary)` 与
`finboard_report_run(view=summary)` 切换到该出口,detail 契约不变。输出与
Python 参考实现 `summarize_run_artifacts` **逐字段一致**(真实事故 run 实测
PASS;语义锚点测试防「两个实现一起错」)。

实测对比(同一真实 run):

| 路径 | 耗时 | 客户端峰值 RSS |
| --- | --- | --- |
| 旧全量物化 | 2–3 分钟(且超时) | 13.27GB |
| 新库内聚合 | ~10s | ~137MB |
| 对照:Python 参考实现分块流式 | 192–300s | ~692MB |

## Why

- 摘要类查询的内存上界必须与 run 规模无关;聚合放数据库是唯一稳态解,
  「先全量拉取再在 Python 里数数」的任何变体都会在更大的 run 上重演。
- 修一处消费路径不等于修完:同一个全量物化模式会藏在多条工具链路里
  (本次 `report_run(view=summary)` 与 `get_run` 同病灶),要按
  `list_artifacts` 的全部调用方逐个分诊。

## 后续:#480 detail / export 护栏前移(2026-09-16)

#458 的载荷护栏是「先 `list_artifacts` 全量物化 → Python `len(str(payload))`
估计 → 拒绝序列化」,估计发生在加载之后,挡不住加载本身:#479 合并并重启后,
`report_run(view=detail)` 与 `report_export` 在同一真实 run 上实测仍 110s 左右
冲到 10.2GB+(0.25s 采样曲线,熔断中断)。

修复(issue #480):`ResearchRunRepository.estimate_artifact_payload_bytes` ——
`SUM(octet_length(payload::text))`,`json` 列 `::text` 仅 detoast + 计长、
不做 JSON 解析,真实 run 实测 5666MB / 7.8s;`report_run(view=detail)` /
`report_export(kind=run)` 加载前先估计,超限直接具名 `payload_too_large`
(下钻单决策豁免不变);加载后的内存估计 `estimate_run_detail_bytes` 删除,
护栏单一来源。修后曲线:detail 8.3s / export 7.5s,峰值均 140MB。

**教训:护栏/估计必须在加载之前(数据库侧)做——「先加载再判断」的任何
护栏都救不了内存。** 遗留:REST `GET /research-runs/{id}/report/export` 与
MCP `finboard_run_artifacts` 仍按契约全量加载 payload,需分页/裁剪的契约
讨论(见 issue #480「遗留」节)。

## How to apply

- 新增「按 run 聚合」类 MCP / REST 查询时,禁止
  `repo.list_artifacts()` 全量物化后 Python 计数;照
  `summarize_artifacts` 模式:stage 谓词先行过滤(features 等大 payload
  行不进 detoast)→ 库内聚合 → 只返回有界字段。
- SQL 两条硬约束(本修复踩过的坑):
  1. `research_run_artifacts.payload` 在真实库与测试库都是泛型 JSON(列型
     `json`),`jsonb_*` 函数前必须 `::jsonb` 归一化;
  2. PostgreSQL **不保证 AND 两侧短路**,`jsonb_array_length` /
     `jsonb_array_elements` 遇非数组直接报错 —— jsonb 函数只能出现在
     CASE 的 THEN 分支(嵌套 CASE 有定义的求值顺序),不能写
     `WHEN jsonb_typeof(x)='array' AND jsonb_array_length(x)>0`。
- 语义等值验证方法:拿真实事故 run,用 Python 参考实现按块流式聚合
  (`session.stream` + `yield_per`,部分和可加合并,全程不整体物化)做
  对照;集成测试再锁 `before_cursor_execute` 断言无 ORM 实体全列加载。
- MCP 进程内存飙升排查顺序:`pg_stat_activity` 看是否在拉 payload 大列 →
  Python 进程 RSS 采样定位;恢复 = 终止进程(查询只读,cancel 无副作用,
  PG 侧可 `pg_cancel_backend`),然后先修查询路径再重启 dev server
  (内嵌 `finboard_mcp` 随 finboard-app 启动,改代码后须重启才生效)。
