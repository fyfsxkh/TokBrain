# 更新日志 / Changelog

本文件记录 TokBrain 面向用户的重要版本变化。版本日期采用上海时区。

## [Unreleased]

当前没有尚未发布的用户功能。

## [1.0.0] - 2026-08-11

TokBrain 1.0 把导入、处理、整理和检索短视频知识的整个流程重新梳理了一遍。现在从
添加作品到真正入库，每一步都更清楚，也更容易在出错后继续。

### 导入方式更多，也更清楚

- 除了粘贴公开链接，现在还可以直接选择本地视频、整个下载文件夹或 ZIP 数据包。
- 所有预检结果都会先停在确认页面。只有明确点击入库后才开始处理，不会因为选中文件或
  预检完成就自动产生 AI 用量。
- 重复作品会自动识别；页面刷新或切换栏目后，尚未确认的结果仍会保留。
- “今日处理”和“今日入库剩余”现在只按成功入库的作品计算。预检、待入库和失败作品
  都不会被误算进去。

### 处理结果更可信

- 视频会综合字幕、语音、画面文字和关键画面整理内容，不再只依赖标题或简单元数据。
- 当原始材料不足时，作品会明确显示需要补充什么，并允许上传有权使用的本地视频或图片
  后继续处理。
- 没有足够依据的旧总结不会继续作为可靠知识参与检索，减少看似完整但没有来源的回答。
- 网络中断、服务超时、模型未配置和文件异常会给出更容易理解的提示，也可以安全重试。

### 知识库和问答更好用

- 知识库按待处理、已入库、待补件、异常和归档分类，支持收藏夹整理、批量操作和详情返回。
- 问答支持快速和深度模式，保留 Markdown、公式、来源与分段显示。
- Obsidian 导出会整理笔记和本地图片，重复导出时尽量保留用户自己写的内容。
- 页面拆分后加载更轻，长任务轮询、分页和流式回答也更顺畅。

### 日常使用更稳定

- 安装会重新准备干净环境并检查依赖，启动、停止和重启现在能识别本项目自己的进程，
  不会再因为旧记录或日志占用而误报成功、误停其他程序。
- 处理中断或电脑异常退出后，会尽量恢复任务、文件和用量记录，避免出现数据库显示成功
  但文件丢失，或失败任务被算作成功入库的情况。
- 设置页可以查看本地运行状态；链接预检、数据包导入和入库处理异常会分别显示。
- 本地密钥继续由 Windows 保护，服务默认只允许本机访问；日志中的敏感内容会被隐藏。

### 升级提醒

- 本版本为 **v1.0.0**，使用数据库 **schema v9** 和 **API contract 7**。
- 升级前建议备份整个 `data` 文件夹，然后双击“安装”，完成后再双击“启动”。
- 第一次启动会先备份旧数据库，再自动整理现有数据。数据较多时请耐心等待，不要强制关机。
- 如果更换过 Windows 用户，原来保存的模型或账单密钥可能无法读取，请在设置页重新输入。
- 本版本通过 GitHub 提供源码压缩包，不附带单独的安装程序。

## v0.5.0 开发阶段记录（已并入 v1.0.0）

### 证据可信度与 schema v9 迁移

- schema v9 新增证据与补件状态。已入库的受限视频若没有本地源文件，也没有字幕、转写、
  OCR 或视觉描述等原始证据，会被标记为“证据不足/需要补件”；迁移同时清除正文、总结、
  知识块和关键帧，避免标题、元数据或生成文本被误当作有来源知识继续检索。
- 升级前自动创建 `data/backups/douyin_rag-pre-v9-*.db`。恢复时必须先停止服务，并让
  数据库与 `source-assets/`、`media/`、`keyframes/` 使用同一备份时间点。
- API contract 7 对齐当前证据状态、本地补件与外部数据包导入契约；前端包版本同步为
  v0.5.0。

### 安装、启动与发布工程

- `setup.ps1` 不再复用可能混有遗留包的 `.venv`：它在临时目录创建干净的 Python 3.12
  环境，安装固定依赖，执行 `pip check` 并核验 F2 只能从 `.vendor/` 加载；验证成功后再
  切换环境。运行依赖、可选账单依赖和开发工具已拆分。
- 安装阶段生成 Next.js 生产构建，日常启动改用 `next start`；后端读取 `.env` 的
  `APP_HOST` / `APP_PORT`，修改端口后需重新安装以同步前端 API 地址。
- 启动状态改为原子写入，并记录实例 ID、工作区、端口、进程创建时间、可执行文件，以及
  Windows 允许读取时的命令行哈希。停止脚本只结束身份匹配的进程，不再根据陈旧 PID 或
  固定端口盲目终止程序。
- CI 现在走正式 Windows 安装脚本，要求 FFmpeg/ffprobe、`pip check`、F2 来源验证、
  Python 语法编译与覆盖率下限、前端测试/lint、严格 TypeScript typecheck、生产构建、
  依赖审计和干净提交发布检查；本地 `prepublish_check.ps1 -Full` 使用同一前端检查门槛。
- 系统健康检查新增链接预检、数据包导入和本地处理协调器状态，显示存活/期望 worker 与
  脱敏后的最近错误；启动就绪检查也要求三个协调器全部健康。
- 处理结果采用数据库事务与媒体文件代际共同提交。数据库提交失败或取消时恢复旧文件代际，
  预算预留按是否已经产生真实模型用量释放或结算；异常退出遗留预留在协调器启动时回收。
- `scripts/audit_library.py` 现可从仓库根目录直接运行，以 SQLite `mode=ro` 检查 schema v9
  的检索证据、补件状态、孤儿知识块和错误分布，不会因数据库不存在而创建空文件。

### 链接入口回退与外部视频数据包

- 链接入口恢复为“预检 → 手工确认加入待入库 → 用户主动入库”，移除自动确认和自动
  AI 入库选项；旧批次中遗留的自动化标记也不会在重启后恢复执行。
- 导入页新增“外部视频数据包”：可选择其他工具生成的整个下载文件夹或 ZIP，一次最多
  100 个视频；后端自动识别清单 JSON/CSV、F2 `douyin_videos.db`、同名侧车文件和文件名
  中的抖音 ID。无法可靠匹配时按文件名和 SHA-256 作为本地视频继续，而不是导入失败。
- 文件夹/ZIP 上传后只做校验和预检，结果继续使用原有手工确认与主动入库流程；不访问 F2、
  不消耗链接额度，也不会在检测完成后自动产生模型费用。
- ZIP 拒绝加密、嵌套、符号链接、绝对路径、`..`、路径碰撞和异常压缩比；不支持远程
  媒体 URL、任意本机路径、纯元数据和图文作品。
- 可选 F2 Cookie 只需在设置页保存一次，后续应用内链接导入会自动复用 DPAPI 加密值。
- 日常使用不再推荐“F2 CLI → manifest → 外部令牌 → 推送脚本”流程。外部批量导入 API
  与 `push_to_tokbrain.py` 继续保留给同机可信工具的高级程序化集成。

### 本地视频与外部批量导入

- 导入页新增独立“本地视频”入口，一次可选择或拖入最多 10 个视频；一文件一作品，标题
  默认取文件名，并可逐条修改标题、描述和目标收藏夹。上传成功后默认只进入待确认，继续
  复用原有“确认加入待入库 → 主动入库”流程。
- 本地视频经过魔数、真实容器、视频轨、大小、SHA-256、ffprobe 时长和预算预检；同一
  视频自动去重。本地与外部导入不会访问 F2，也不消耗每日链接解析额度。
- 新增同机外部批量导入 API v1：调用方使用 Bearer 令牌创建 JSON 清单、逐条上传本地
  视频并提交批次。每批最多 100 条，默认只进入待入库；显式
  `start_processing=true` 时才创建 AI 入库任务。
- 外部批次要求权利声明与 `Idempotency-Key`，支持创建、上传和提交重试；重复提交不会
  重复创建作品、收藏夹关系或任务。接口不接受远程媒体 URL、任意本机路径和纯元数据条目。
- 设置页可生成、轮换和撤销外部导入令牌；令牌明文只显示一次，数据库只保存 SHA-256
  哈希和前缀。服务继续只监听 `127.0.0.1`。
- 数据库结构升级到 **schema v8**，新增可恢复的数据包文件暂存表；升级前自动创建
  `douyin_rag-pre-v8-*.db` 备份。旧作品默认保持
  `link / f2`，本地及外部作品使用 `never` 刷新策略。
- API contract 升级到 `6`。新增网页数据包创建、逐文件上传、检测和状态接口；
  外部接口错误统一返回包含 `code`、`message`、`retryable` 和 `field` 的 `detail` 对象。
- 新增[外部批量导入 API 文档](docs/external-import-v1.md)和仅依赖 Python 标准库的
  [`push_to_tokbrain.py`](scripts/push_to_tokbrain.py) 一键推送示例。

### 音画协同关键帧

- 允许下载的视频不再只按场景突变选图；候选池同时包含转场后的稳定画面和全时段均匀
  采样，并以视频文件真实时长为准。
- ASR 保留句级时间戳，模型据此生成视觉证据需求；候选画面先提取文字和客观视觉描述，
  再由模型按候选编号重排，模型异常时自动回退到清晰度与时间覆盖规则。
- 最终入库画面执行精确二次抽帧，减少转场帧和时间偏移；数据库记录候选来源、选择分数、
  选择理由、OCR 和视觉描述，便于排查。
- 数据库结构升级到 **schema v6**；从 schema v5 升级只增加关键帧解释字段，不重建
  或丢弃已有数据，升级前自动创建 `douyin_rag-pre-v6-*.db` 备份。

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
