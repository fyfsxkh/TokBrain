# 更新日志 / Changelog

本文件记录 TokBrain 面向用户的重要版本变化。版本日期采用上海时区。

## [0.4.0] - 2026-07-31

### 收藏夹专属总结提示词

- 每个收藏夹现在可以保存自己的总结提示词，提示词以正常、可编辑的文本显示。
- 新入库或手动“补齐/更新总结”时，使用作品最近加入的收藏夹提示词。
- 清除收藏夹提示词后会回退到设置页的全局总结提示词。

### 两阶段导入流程

- 预检完成后，可为每条作品选择一个已有收藏夹，默认选择“手动导入”。
- “确认加入待入库”只创建或关联知识库作品、保存收藏夹关系，不下载媒体、不调用 AI，
  也不自动创建入库任务。
- 确认成功后，按钮切换为“入库（数量）”，用户可在导入页直接处理刚才确认的作品，
  也可以前往“知识库 → 待入库”单条或批量开始入库。
- 同时存在新预检作品和已确认待入库作品时，两组勾选、数量、状态和操作按钮分别管理，
  不会把待确认作品误当成可入库作品。

### 自动去重与预检保留

- 同一次粘贴中的相同链接会在后台自动移除，重复链接不占用每批 10 条唯一作品的限额。
- 后续批次会与尚在预检区的作品比较并自动去重；并发提交相同链接时也只创建一条预检。
- 不同短链接最终解析到同一作品时，只保留一条可确认结果。
- 页面仅提示“已去掉 N 条重复链接”，不再显示误导性的重复作品卡片；其余唯一作品继续
  正常预检。
- 已完成的预检结果在页面切换后仍会保留，并可逐条删除。删除已确认条目的预检记录不会
  连带删除对应的知识库作品。

### 删除与数据清理

- 知识库的待入库、已入库、处理异常和已归档作品均可永久删除。
- 已完成总结的作品和总结详情页也提供永久删除入口；删除会同步清理总结、知识块、
  检索索引、关键帧和对应本地资产。
- **永久删除不可恢复。** 如需保留内容，请先备份 `data/` 中的数据库和资产目录。

### 数据库升级

- 数据库结构升级到 **schema v5**，新增收藏夹总结提示词字段。
- 升级前会在 `data/backups/` 自动创建
  `douyin_rag-pre-v5-YYYYMMDD-HHMMSS.db` 时间戳备份。
- 从 schema v4 升级不会重建或丢弃已有预检与入库队列。

### API 与兼容性

产品版本升级为 v0.4.0；`API_CONTRACT_VERSION` 继续保持为 `4`。原有 `item_ids`
确认请求仍然兼容，但确认响应和入库语义已经调整；本次包含以下接口扩展：

- 收藏夹响应以及 `PUT /api/library/collections/{collection_id}` 支持
  `summary_prompt`。
- `POST /api/import-batches` 响应增加 `queued_count` 和 `duplicate_count`。
- `POST /api/import-batches/{batch_id}/confirm` 支持逐条提交
  `items: [{item_id, collection_id}]`；原有 `item_ids` 仍然兼容，未指定收藏夹的作品
  继续加入“手动导入”。
- 确认接口现在返回 `confirmed_count`、`work_ids` 和
  `library_state: "pending"`，且不再自动创建入库任务。
- 正式批量入库通过 `POST /api/library/ingest/jobs` 启动。
- 新增 `DELETE /api/import-items/{item_id}`；知识库的
  `DELETE /api/library/works/{work_id}` 现在覆盖已完成总结的作品及其本地派生数据。

### 验证结果

- 后端测试：114 项通过。
- 前端契约测试：30 项通过。
- 前端 lint 与生产构建通过。
- 测试使用人工构造数据和模拟传输层，不访问真实抖音。

### 升级说明

1. 停止正在运行的 TokBrain，并建议额外备份整个 `data/` 目录。
2. 获取 v0.4.0 源码后运行 `安装.cmd` 更新依赖，再运行 `启动.cmd`。
3. 首次启动会自动备份并迁移数据库到 schema v5，请勿在迁移过程中强制结束进程。
4. 升级后检查收藏夹提示词，并确认导入页的“确认待入库”和“入库”两步操作符合预期。

本版本只通过 GitHub 提供自动生成的源码 ZIP 和 TAR 包，不附加二进制安装包。

### English summary

TokBrain v0.4.0 adds per-collection summary prompts, per-item collection
selection, and an explicit two-stage Confirm-to-Pending / Ingest workflow.
Selection and action states remain separate when new previews and previously
confirmed works coexist.

Duplicate links are removed automatically within a submission, across preview
batches, during concurrent submissions, and after different short links resolve
to the same work. Users see a concise removed-duplicate count while unique works
continue previewing normally. Completed previews persist across page switches
and can be deleted individually.

Works in every Library state, including fully summarized works, can now be
permanently deleted together with derived knowledge and local assets. This
operation cannot be undone. Database schema v5 adds collection prompts and
creates a timestamped SQLite backup before migration while preserving schema v4
preview and ingestion queues.

API contract version 4 is retained. The release adds compatible response fields
and per-item collection assignments, changes confirmation to create Pending
works without starting ingestion, and introduces explicit bulk-ingestion and
preview-deletion endpoints. The release provides GitHub-generated source
archives only; no binary installer is included.
