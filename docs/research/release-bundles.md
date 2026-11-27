# 冻结发布包

使用发布包在研究实例之间迁移一份或一组冻结发布。当前格式覆盖行情 bars、每日指标、财务指标和可转债指标。主行情发布和其基准保持原样，研究指标仍作为关联观测单独登记。导出前核对原数据源的使用与再分发条款。随公开研究版交付的样例沿用合成数据。

## 操作

- Web 数据基座打开「同步与发布」中的「冻结发布包」。选择数据集和对应版本，可跨数据集选多份发布。提交导出后从任务结果下载 ZIP。
- 导入时选择本机 ZIP，上传并查看预览。预览给出版号、数据覆盖、体积、复权、来源及冲突。确认后由数据队列完成校验和登记。任务中心显示校验、导入或导出及终态。源文件可用于同身份重试。
- 桌面研究工作台打开同一个数据基座入口，沿用当前隔离实例的发布目录。用户可打开后选择本机 ZIP。

```text
finboard data bundle export export.zip --release bars-release --release metrics-release
finboard data bundle preview export.zip
finboard data bundle import export.zip
```

旧合成样例仍可通过桌面控制台的样例导入入口或 CLI 导入。通用包预览支持 `finboard-release-bundle-v1`。

## 格式与一致性

- ZIP 的 `bundle.json` 保存格式、各发布 ID 与 checksum、交易日历及文件摘要。每份发布保留原 `manifest.json` 与逐标的 Parquet 文件。校验冻结清单和每个文件的 SHA-256，按每份发布的字段与行数核对 Parquet 元数据。冻结清单中的停牌和短历史得到保留。
- 解包到发布根目录内的专用暂存目录。文件数据分块校验，目录校验限制解包总量与成员数量，拒绝路径越界、重复成员及不在清单中的文件。
- 同 ID 同 checksum 幂等复用，发现相同身份或日历冲突时拒绝。完成文件校验后一次提交发布登记和逐标的读模型，交易日历与标的字典只添加缺失记录。事务失败后可复用之前已经完整落盘的冻结目录。
- 网页上传和托管导出的默认目录是发布根目录旁的 `data_bundles`。在容器侧通过环境变量 `FINBOARD_DATA_BUNDLE_ROOT` 指定时，网关和 worker 应使用同一目录。

## REST、MCP 与审计

| 操作 | 接口 |
| --- | --- |
| 上传并预览 | `POST /api/data/bundles/preview`，请求体为 ZIP 字节 |
| 提交导入 | `POST /api/data/bundles/imports`，回填 `bundle_id` 和预览提供的 `checksum` |
| 提交导出 | `POST /api/data/bundles/exports`，传 `release_ids`，返回任务及文件标识 |
| Web 查询、下载 | `GET /api/data/bundles/{bundle_id}` 和 `GET /api/data/bundles/{bundle_id}/download` |
| MCP 导出 | `finboard_data_bundle_export` 需要 `data.write`，返回专用目录的文件标识、校验和及体积 |
| MCP 查询 | `finboard_data_bundle_status` 需要 `research.read`，传文件标识或数据包作业 ID |

审计登记操作和脱敏摘要。队列提交、协作取消、冻结包导入和导出按[研究审计契约](audit-operations.md)记录。文件字节、宿主路径和凭据不写入审计摘要。
