# issue #214 因子目录收敛(v2 唯一事实来源 + v1 投影层)

- 主题:FACTOR_CATALOG(v1 selection)与 FACTOR_LAB_CATALOG(v2)双头维护收敛
- 日期:2026-08-29

## 结论 / 事实

- 用户确认两项决策:**v2(FACTOR_LAB_CATALOG)为唯一事实来源,v1 变投影层**;
  **历史 run 的 selection(factor_version=v1)不迁移,保持兼容**。
- `finboard_data/factors.py` 的 `FACTOR_CATALOG` 由 `_project_v1_catalog()` 从
  `FACTOR_LAB_CATALOG` 逐字段投影生成:dependencies=source_fields、
  frequency 映射、description=economic_hypothesis;仅原始值单位
  (cny/multiple/ratio)保留 v1 展示映射——v2 目录登记的是标准化后的
  z_score 单位,不能直接搬。
- v1 名称在 v2 目录缺失时导入期 RuntimeError(fail-loud,防漂移)。
- 仓库里其实有第三个目录:`finboard_backtest.factors.catalog.
  RESEARCH_FACTOR_CATALOG`(复合评分元数据,FACTOR_FRAMEWORK_VERSION="v1"),
  与本次两套目录语义不同(无 dependencies 契约),issue 范围外,未动。

## Why

- 双目录字段重叠(dependencies/frequency/描述)但措辞各自演化,agent 认知
  负担大(#190 只能靠文档澄清双命名空间)。
- v2 缺 v1 的「原始值单位」概念,直接全字段投影不可行,故保留最小展示映射
  而不是把 unit 塞进 v2(v2 的 unit 语义是标准化后单位,混用会错)。

## How to apply

- 新增选股因子:先在 `factor_lab.py` 的 `FACTOR_LAB_CATALOG` 登记(含
  implementation),再在 `factors.py` 的 `FactorName` 枚举与 units 映射加名,
  投影自动带出 dependencies/frequency/描述;不要在 v1 侧手写 FactorDefinition。
- 依赖/频率语义变更只需改 v2 一处,投影与测试
  (`tests/unit/data/test_factor_catalog_projection.py`)自动校验一致。
