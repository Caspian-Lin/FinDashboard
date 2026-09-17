# Issue #206:研究/回测 MCP 返回瘦身(P0-P3)

## 主题

MCP 工具默认响应从 MB 级降到 KB 级(view=summary|detail / fills 分页有界 /
写操作精简回执 / grid 公共字段上提 / job_get 幂等短路),全部纯展示层变换,
不改语义、不动落库。

## 结论 / 事实

- 体积三大源头与修法:`dataset_release_get` 逐标的 instruments(→ summary 复用
  `_release_summary_to_dict`);`run_get`/`report_run` universe 逐标的判定
  ×5534(→ 服务端聚合计数 `{total, included, excluded_by_reason}`,fills 按
  决策计数);fills 全量 1300-1600 条(→ `fills_limit` 默认有界 200,
  `fills_limit=null` 全量,`fills_total` 元数据完整)。
- 写操作回执约定六字段形态:`{id(或 strategy_id/run_id), version?, status,
  checksum, execution_mode?, created_at, view: "ack", detail_hint}` —— 附
  `detail_hint` 告诉 agent 全文走哪个 get;`backtest_run(strategy_spec)` 复用
  `enqueue_research_run` 的返回也从 detail 换成了 ack(取 `ack["checksum"]`
  而非 `detail["manifest_checksum"]`),mock 该函数的测试要同步。
- **MCP SDK 重建 dataclass 返回类型**(最大的坑):新版 `mcp.server.mcpserver`
  的 func_metadata 对非 BaseModel 返回注解走 `_create_model_from_class` ——
  `create_model(cls.__name__, from_attributes=True)` **重建一个同名 pydantic
  模型**,我们挂在 dataclass 上的 `__get_pydantic_core_schema__` 自定义序列化
  **完全被绕过**(单测里 TypeAdapter 模拟路径能过,SDK 真实路径不过)。
- 治本:`ToolEnvelope`/`ToolError` 从 frozen dataclass 改为 `pydantic.BaseModel`
  (`ConfigDict(frozen=True)` + `@model_serializer(mode="wrap")` 过滤 None)。
  SDK 对 BaseModel 子类直接用作 output_model(issubclass 分支),钩子必然生效;
  属性访问(`env.status`/`env.data`)与关键字构造兼容,仅 `FrozenInstanceError`
  → `ValidationError`、`asdict()` 不可用两点需注意。
- 验证 SDK 序列化必须走真实路径:`tool.fn_metadata.convert_result(envelope)`
  → `.structured_content`(与 `MCPServer.call_tool` 同一条链),不要只测
  TypeAdapter dump——两者结果可以不同。

## Why

- agent 实测返回被 MCP 客户端截断(97MB/10MB/7-11MB 级响应),单次典型查询
  付出全量计算与传输成本却只拿到摘要信息量;三个结构性模式——详情接口默认
  全量、写操作回显全文、列表接口嵌套重复。
- exclude_none 走 dataclass 钩子失败的原因是 SDK 重建模型,这是「模拟测试绿、
  真机红」的典型,只有 convert_result 路径能暴露。

## How to apply

- 新增 MCP 详情类工具一律带 `view=summary|detail` 默认 summary;列表类工具
  大数组字段(逐标的/fills)默认有界(200)+ 计数元数据;写操作返回 ack 形态
  回执 + detail_hint。
- 需要自定义信封序列化时,直接用 BaseModel + model_serializer,不要在
  dataclass 上挂 `__get_pydantic_core_schema__` 指望 SDK 尊重它。
- 测试信封/序列化行为时固化 `test_sdk_convert_result_path_omits_none` 这类
  真实 convert_result 路径断言。
- 导出/下载类端点(REST download / report_export)必须显式 `view="detail"` /
  `fills_limit=None`——聚合函数默认值改了之后,导出全量语义靠调用方显式传参。
- 回滚:各 view/fills 参数均可显式传 detail/全量恢复旧行为;信封 BaseModel
  化无行为回滚需求(字段与构造兼容)。
