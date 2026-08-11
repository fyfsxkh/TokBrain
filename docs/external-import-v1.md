# TokBrain 外部批量导入 API v1

> **高级程序化接口，不是普通用户的日常导入路径。** 如果已经用 F2 或其他工具取得视频，
> 请直接在 TokBrain 导入页的“外部视频数据包”选择下载文件夹或 ZIP。网页会自动识别
> `douyin_videos.db`、JSON、CSV 和文件名；普通用户无需制作 manifest、生成 Bearer
> 令牌或执行 `push_to_tokbrain.py`。

此接口用于让**同一台电脑上的可信工具**把已经取得、且用户有权处理的抖音视频推送到
TokBrain。它只负责“清单 → 本地视频上传 → 提交”，不会抓取抖音、访问 F2、读取调用方
给出的本机路径或下载远程 URL。

服务地址固定为 `http://127.0.0.1:8000`。不要通过局域网、公网、反向代理或隧道暴露
该接口。

## 1. 准备令牌

在 TokBrain“设置 → 外部导入令牌”中生成令牌。明文只显示一次，请立即保存到调用工具
自己的安全配置中；TokBrain 数据库只保存 SHA-256 哈希和便于识别的前缀。

- 生成或轮换令牌：`POST /api/settings/integration-token`
- 查看是否已配置及令牌前缀：`GET /api/settings/integration-token`
- 撤销令牌：`DELETE /api/settings/integration-token`

轮换或撤销后，旧令牌立即失效。除令牌管理接口外，以下所有 `/api/integrations/v1/*`
请求都必须发送：

```http
Authorization: Bearer <token>
```

不要把令牌写进仓库、清单、日志或命令行历史。调用示例优先从
`TOKBRAIN_IMPORT_TOKEN` 环境变量读取。

## 2. 调用流程

### 第一步：创建清单

```http
POST /api/integrations/v1/import-batches
Authorization: Bearer <token>
Idempotency-Key: <stable-key-for-this-manifest>
Content-Type: application/json
```

```json
{
  "rights_attested": true,
  "items": [
    {
      "client_item_id": "export-20260807-001",
      "platform_work_id": "7531234567890123456",
      "video_pending": true,
      "title": "示例标题",
      "description": "可选简介",
      "author_id": "optional-author-id",
      "author_name": "示例作者",
      "published_at": "2026-08-07T10:00:00+08:00",
      "source_url": "https://www.douyin.com/video/7531234567890123456",
      "duration_seconds": 42.5,
      "target_collection_id": 3,
      "expected_sha256": "64位小写十六进制SHA-256",
      "extra_metadata": {
        "source_tool": "my-exporter",
        "source_record_id": "row-42"
      }
    }
  ]
}
```

约束：

- `rights_attested` 必须为 `true`，表示调用方确认有权处理本批文件与元数据。
- 每批 1–100 条，整个 JSON 请求不超过 2 MB。
- `client_item_id` 由调用方生成，并在批次内唯一；后续上传通过它定位条目。
- `platform_work_id` 必须是纯数字抖音作品 ID；跨批次使用它去重。
- `video_pending` 必须为 `true`，明确表示该条目随后会上传本地视频；不接受纯元数据条目。
- `title` 必填；其余展示字段可选。`target_collection_id` 省略时使用默认收藏夹。
- `source_url` 省略时按作品 ID 生成；传入时只接受 HTTPS 抖音作品链接。
- `expected_sha256` 建议必传，可在上传前发现错配；辅助脚本会自动计算并填写。
- 单条 `extra_metadata` 序列化后最多 64 KB，只保存来源辅助信息，不能覆盖
  `media_policy`、导入来源、刷新策略或权利声明。
- 不接受 `video_path`、`media_url` 等路径或远程媒体字段，也不接受无视频的纯元数据条目。

响应会给出上传定位信息：

```json
{
  "batch_id": "批次ID",
  "replayed": false,
  "state": "succeeded",
  "items": [
    {
      "client_item_id": "export-20260807-001",
      "item_id": 123,
      "status": "needs_local_file",
      "existing_work_id": null,
      "upload_url": "/api/integrations/v1/import-batches/批次ID/items/export-20260807-001/asset",
      "error": null
    }
  ]
}
```

这里的批次 `state="succeeded"` 表示清单已经成功构建，不表示视频已上传或批次已提交。
条目创建后为 `needs_local_file`，上传完成后为 `ready`，提交新作品后为 `confirmed`。
已有作品或批内相同 `platform_work_id` 的后续条目为 `duplicate` 且不返回上传地址；批内
重复还会返回 `error.code="duplicate_platform_work_id"`。

`Idempotency-Key` 是必填的 8–200 字符请求头。相同键和相同清单会返回原批次并设置
`replayed=true`；相同键对应不同内容会返回 `409 idempotency_conflict`。调用工具应把
该键与自己的导出批次一起持久化，并在网络重试时复用。

### 第二步：逐条上传视频

```http
PUT /api/integrations/v1/import-batches/{batch_id}/items/{client_item_id}/asset?replace=false
Authorization: Bearer <token>
Content-Type: multipart/form-data
```

表单字段名固定为 `file`。单个文件最大 1 GB，服务端检查文件魔数、真实容器、视频轨、
SHA-256、ffprobe 时长和作品时长上限；改扩展名不能绕过检查。

上传中断后可安全重试。已上传文件的 SHA-256 相同时直接返回成功；内容不同时默认返回
冲突。只有批次尚未提交且调用方明确使用 `replace=true` 时，才允许替换资产。

### 第三步：提交批次

```http
POST /api/integrations/v1/import-batches/{batch_id}/commit
Authorization: Bearer <token>
Content-Type: application/json
```

默认仅加入“待入库”：

```json
{"start_processing": false}
```

只有希望立即产生模型调用与费用时才使用：

```json
{"start_processing": true}
```

提交采用逐条结果，部分无效不会回滚其他有效条目：

```json
{
  "batch_id": "批次ID",
  "results": [
    {
      "client_item_id": "export-20260807-001",
      "status": "imported",
      "work_id": 456,
      "error": null
    }
  ],
  "work_ids": [456],
  "job": null
}
```

条目状态为 `imported`、`duplicate`、`invalid` 或 `missing_video`。重复提交不会重复创建
作品、收藏夹关系或入库任务；`start_processing=true` 也只会为尚未排队的待处理作品创建
一个任务。创建、上传和默认提交不消耗链接解析额度；正式 AI 入库仍受媒体时长、Token
和费用预算限制。

使用 `GET /api/integrations/v1/import-batches/{batch_id}` 可查询上传、提交和逐条状态。
GET 的结构与创建响应相同，但不包含只用于说明创建重放的 `replayed` 字段。

## 3. 一键 Python 示例

仓库提供仅依赖 Python 3.12 标准库的
[`scripts/push_to_tokbrain.py`](../scripts/push_to_tokbrain.py)。它会计算文件 SHA-256、
创建清单、流式逐条上传并提交批次。本地清单格式比 API 多一个仅供脚本读取的
`video_path`；该路径不会发送给 TokBrain，脚本会在发送时自动补上
`video_pending=true`。

```json
{
  "items": [
    {
      "client_item_id": "export-001",
      "platform_work_id": "7531234567890123456",
      "title": "示例标题",
      "video_path": "C:\\Users\\me\\Videos\\example.mp4",
      "author_name": "示例作者",
      "target_collection_id": 3,
      "extra_metadata": {"source_tool": "my-exporter"}
    }
  ]
}
```

```powershell
$env:TOKBRAIN_IMPORT_TOKEN = "刚生成的令牌"
py -3.12 .\scripts\push_to_tokbrain.py .\my-manifest.json --attest-rights
```

默认只进入待入库。如需立即处理：

```powershell
py -3.12 .\scripts\push_to_tokbrain.py .\my-manifest.json `
  --attest-rights --start-processing
```

脚本默认根据发送清单生成稳定幂等键，也可用 `--idempotency-key` 传入外部工具自己的
批次键。只有确认要覆盖尚未提交的不同资产时才使用 `--replace`。

## 4. curl.exe 分步示例

以下示例使用 PowerShell 与 Windows 自带的 `curl.exe`：

```powershell
$base = "http://127.0.0.1:8000"
$token = $env:TOKBRAIN_IMPORT_TOKEN
$key = "my-export-20260807-001"
$manifest = @'
{
  "rights_attested": true,
  "items": [{
    "client_item_id": "export-001",
    "platform_work_id": "7531234567890123456",
    "video_pending": true,
    "title": "示例标题"
  }]
}
'@

$created = $manifest | curl.exe --silent --show-error --fail-with-body `
  -X POST "$base/api/integrations/v1/import-batches" `
  -H "Authorization: Bearer $token" `
  -H "Idempotency-Key: $key" `
  -H "Content-Type: application/json" `
  --data-binary "@-" | ConvertFrom-Json

$batchId = $created.batch_id
curl.exe --silent --show-error --fail-with-body `
  -X PUT "$base/api/integrations/v1/import-batches/$batchId/items/export-001/asset" `
  -H "Authorization: Bearer $token" `
  -F "file=@C:/Users/me/Videos/example.mp4"

'{"start_processing":false}' | curl.exe --silent --show-error --fail-with-body `
  -X POST "$base/api/integrations/v1/import-batches/$batchId/commit" `
  -H "Authorization: Bearer $token" `
  -H "Content-Type: application/json" `
  --data-binary "@-"
```

## 5. 错误与安全边界

新接口的错误主体统一为：

```json
{
  "detail": {
    "code": "stable_error_code",
    "message": "可读说明",
    "retryable": false,
    "field": "items.0.platform_work_id"
  }
}
```

只有 `retryable=true` 的传输或暂时性错误适合自动重试，并且重试必须复用同一个
`Idempotency-Key`。校验错误、权利声明缺失、SHA 不匹配和幂等冲突应由调用方修正。

固定边界：

- 接口仅供本机可信工具使用，不是公网采集或解析 API。
- Bearer 令牌只能授权向本机 TokBrain 导入，不能代替内容权利或平台授权。
- 调用方必须先取得视频；TokBrain 不接受远程 URL、不访问调用方提供的文件路径。
- 所有视频都经过与界面本地导入相同的校验和预算检查。
- 外部导入作品的刷新策略为 `never`，后续入库不会访问 F2。
- 令牌若疑似进入日志、截图或仓库，应立即在设置页轮换。
