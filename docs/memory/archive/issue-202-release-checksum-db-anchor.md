# 冻结发布 manifest 校验和必须 DB 锚定,不能依赖当前代码重算(issue #202)

## 主题

`finboard_data.releases` 的 manifest 完整性校验策略:何时用 DB 锚定
(`expected_checksum`),何时允许重算;以及改 manifest 字段的正确姿势。

## 结论 / 事实

- 旧校验方式(`load_dataset_release` / `FrozenReleaseProvider`,2026-08-18 之前):
  用**调用方当前代码**的 `as_dict()` 重算整清单 sha256 与 manifest 内冻结值比对。
  重算结果依赖代码版本:任何字段增删、git SHA 漂移、工作区带未提交改动(`-dirty`)
  都会让**连刚发布的数据集也被拒读**——feature_snapshot / research_run(含
  multi_period)整体挂掉,只有事件驱动回测(直接读 bars 缓存不经 manifest)幸免。
- #187 给 `dataset_kind` 做的手工兜底(releases.py `_release_checksum` 内 bars
  发布 pop 掉 `dataset_kind` 字段)就是这个坑的第一次暴露;逐字段兜底不可持续。
- **2026-08-18 修复(issue #202)**:`load_dataset_release` / `FrozenReleaseProvider`
  新增可选 `expected_checksum`(= DB `research_dataset_releases.release_checksum`,
  research_run manifest 的 `FrozenArtifactRef.checksum` 入队时已冻结同一值):
  - 提供时只比对 manifest 内嵌 `release_checksum == expected_checksum`,**不重算**;
  - 未提供时保留旧重算路径(纯磁盘独立场景与向后兼容),`verify_dataset_release`
    仍走重算(显式全量校验入口,严格性是目的);
  - 文件级完整性不受影响:两种路径都保留逐文件 `artifact_checksum` sha256。
- 消费端透传点:research_run worker(`build_signal_engine_adapter_factory` 用
  `manifest.dataset_releases` 建 release_id→checksum 映射)、feature_snapshot
  executor(`require_usable` 返回的 DB 行自带 `release_checksum`)、price-feature
  spawn 子进程(`initargs` 追加 `release.release_checksum`)、API/MCP 同步构建
  特征快照路径(同样持有 DB 行)。

## Why

- manifest 的意义是**发布时点冻结**:完整性应该锚定到发布时点已持久化的值
  (DB 列),而不是读取时点的代码输出。重算 = 用会漂移的尺子量不可变的东西。
- `code_version()`(`background_jobs/executors/_runtime.py`)优先读
  `FINBOARD_CODE_VERSION` 环境变量,否则 `git rev-parse --short=12`,工作区有
  已跟踪文件改动时附 `-dirty`。同一份 manifest 在干净 checkout 可读、在 dirty
  工作区不可读,纯粹是误报。

## How to apply

- **以后要删改 manifest(`as_dict()`)字段**:先递增 `schema_version` 让新发布
  用新 schema,旧发布继续按旧值锚定读取;禁止直接改 `as_dict()` 输出再指望
  读取端重算跟上来——旧 manifest 会立刻全部校验失败。兼容旧发布确需例外时,
  参照 `_release_checksum` 里 `dataset_kind` 的 pop 兜底并写明 issue 出处。
- **新增冻结发布消费端**:凡持有 DB 行或 manifest `FrozenArtifactRef` 的,一律
  传 `expected_checksum=release.release_checksum`;只有纯磁盘、无 DB 上下文的
  工具(如 `verify_dataset_release`)才允许走重算路径。
- 回滚:消费端不传 `expected_checksum` 即回到旧重算路径,行为完全兼容。
