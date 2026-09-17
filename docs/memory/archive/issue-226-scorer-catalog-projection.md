# issue #226 复合评分因子目录语义字段投影

- 主题:`finboard_backtest.factors.catalog.RESEARCH_FACTOR_CATALOG` 与
  `FACTOR_LAB_CATALOG` 的语义字段去重(继 #214 选股目录投影之后的第三个目录)
- 日期:2026-08-29

## 结论 / 事实

- 采用「部分投影」:评分消费参数(winsorize pcts / standardize /
  missing_strategy / available_at_rule / 原始值 unit / category)保留本地
  `_SCORING_PARAMS`;语义字段(economic_hypothesis / expected_failure /
  source_field / direction)逐项从 `FACTOR_LAB_CATALOG` 投影生成
  (`_project_research_catalog()`)。
- direction 由 preference 映射:higher→LONG、lower→SHORT;EXPOSURE_ONLY
  无方向语义,导入期 RuntimeError 拒绝。评分因子名在 v2 缺失同样 fail-loud。
- 13 因子的 `direction_sign` 与 compiler `_dataset_from_source` 映射结果
  逐项等价(冻结断言);`_dataset_from_source` 的旧别名表保留,投影后
  source_field 前缀本就是合法 dataset 名。

## Why

- 语义字段在两套目录各写一份且已出现措辞漂移(PB 假设两边文字不同),
  与 #214 同类的双头维护;但评分参数是「评分器怎么用因子」,与「因子是
  什么」(v2 契约)分离,硬塞进 v2 会污染快照 schema。

## How to apply

- 新增复合评分因子:先在 `factor_lab.FACTOR_LAB_CATALOG` 登记(须有方向性
  preference),再在 `_SCORING_PARAMS` 加评分参数条目,投影自动带出语义字段。
- 只改假设/失效文案或数据来源:改 v2 一处即可,评分目录与
  `tests/unit/factors/test_catalog.py` 投影断言自动跟随。
