import type {
  ChatStreamEvent,
  Collection,
  Health,
  ImportBatch,
  Job,
  LibrarySummary,
  RuntimeSettings,
  Probe,
  WorksPage,
  WorkSummaryDetail,
} from "./contracts";

type UnknownRecord = Record<string, unknown>;

function isRecord(value: unknown): value is UnknownRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function invalid(label: string): never {
  throw new Error(`本机后端返回的${label}格式无效，请确认前后端版本一致`);
}

function hasFiniteNumbers(value: UnknownRecord, fields: readonly string[]) {
  return fields.every((field) => typeof value[field] === "number" && Number.isFinite(value[field]));
}

function hasStrings(value: UnknownRecord, fields: readonly string[]) {
  return fields.every((field) => typeof value[field] === "string");
}

function hasBooleans(value: UnknownRecord, fields: readonly string[]) {
  return fields.every((field) => typeof value[field] === "boolean");
}

export function parseRuntimeSettings(value: unknown): RuntimeSettings {
  if (
    !isRecord(value)
    || !hasFiniteNumbers(value, [
      "daily_media_minutes_limit",
      "daily_llm_token_limit",
      "monthly_warning_cny",
      "scene_threshold",
      "max_scene_candidates",
      "max_keyframes",
      "min_keyframe_gap_seconds",
      "import_batch_limit",
      "import_daily_limit",
      "import_worker_count",
      "import_network_concurrency",
      "import_cooldown_min_seconds",
      "import_cooldown_max_seconds",
    ])
    || !hasStrings(value, [
      "dpapi_warning",
      "security_cleanup_message",
      "summary_prompt",
      "default_summary_prompt",
      "processing_model",
      "chat_fast_model",
      "chat_deep_model",
      "ocr_model",
      "asr_model",
      "embedding_model",
    ])
    || !hasBooleans(value, [
      "has_dashscope_key",
      "has_bss_credentials",
      "has_f2_cookie",
      "security_cleanup_required",
    ])
    || !["rich", "markdown", "plain"].includes(String(value.default_answer_format))
    || !Array.isArray(value.processing_model_options)
    || !value.processing_model_options.every((item) => typeof item === "string")
    || !Array.isArray(value.chat_model_options)
    || !value.chat_model_options.every((item) => typeof item === "string")
  ) invalid("设置");
  return value as RuntimeSettings;
}

export function parseProbe(value: unknown): Probe {
  if (
    !isRecord(value)
    || typeof value.probe !== "string"
    || !["healthy", "degraded", "down"].includes(String(value.status))
    || typeof value.message !== "string"
    || !isRecord(value.details)
  ) invalid("健康检查");
  if (
    value.details.coordinators !== undefined
    && (
      !Array.isArray(value.details.coordinators)
      || !value.details.coordinators.every((item) =>
        isRecord(item)
        && typeof item.name === "string"
        && typeof item.alive === "boolean"
        && typeof item.workers_alive === "number"
        && typeof item.workers_expected === "number",
      )
    )
  ) invalid("协调器健康检查");
  return value as Probe;
}

export function parseHealth(value: unknown): Health {
  if (
    !isRecord(value)
    || !["healthy", "degraded", "down"].includes(String(value.overall))
    || typeof value.summary !== "string"
    || !Array.isArray(value.probes)
  ) invalid("健康状态");
  value.probes.forEach(parseProbe);
  return value as Health;
}

export function parseJobs(value: unknown): Job[] {
  if (
    !Array.isArray(value)
    || !value.every((job) =>
      isRecord(job)
      && typeof job.id === "string"
      && typeof job.job_type === "string"
      && typeof job.state === "string"
      && typeof job.message === "string"
      && isRecord(job.progress),
    )
  ) invalid("任务列表");
  return value as Job[];
}

export function parseCollections(value: unknown): {
  items: Collection[];
  summary: LibrarySummary;
} {
  if (
    !isRecord(value)
    || !Array.isArray(value.items)
    || !value.items.every((item) =>
      isRecord(item) && typeof item.id === "number" && typeof item.title === "string",
    )
    || !isRecord(value.summary)
    || typeof value.summary.candidate_count !== "number"
    || typeof value.summary.local_item_count !== "number"
  ) invalid("收藏夹");
  return value as { items: Collection[]; summary: LibrarySummary };
}

export function parseWorksPage(value: unknown): WorksPage {
  if (
    !isRecord(value)
    || !Array.isArray(value.items)
    || !value.items.every((item) =>
      isRecord(item) && typeof item.id === "number" && typeof item.title === "string",
    )
    || typeof value.total !== "number"
    || !(value.next_offset === null || typeof value.next_offset === "number")
  ) invalid("作品列表");
  return value as WorksPage;
}

export function parseImportBatch(value: unknown): ImportBatch {
  if (
    !isRecord(value)
    || typeof value.id !== "string"
    || typeof value.source_type !== "string"
    || typeof value.state !== "string"
    || !Array.isArray(value.items)
    || !value.items.every((item) =>
      isRecord(item) && typeof item.id === "number" && typeof item.status === "string",
    )
    || !Array.isArray(value.workers)
    || !isRecord(value.progress)
  ) invalid("导入批次");
  return value as ImportBatch;
}

export function parseWorkSummaryDetail(value: unknown): WorkSummaryDetail {
  if (
    !isRecord(value)
    || !isRecord(value.work)
    || typeof value.work.id !== "number"
    || !isRecord(value.summary)
    || !Array.isArray(value.summary.sections)
    || !Array.isArray(value.assets)
  ) invalid("作品总结");
  return value as WorkSummaryDetail;
}

export function parseChatStreamEvent(value: unknown): ChatStreamEvent {
  if (!isRecord(value) || typeof value.type !== "string") invalid("回答流事件");
  switch (value.type) {
    case "stage":
      if (typeof value.stage === "string" && typeof value.message === "string") return value as ChatStreamEvent;
      break;
    case "sources":
      if (
        Array.isArray(value.sources)
        && value.sources.every((source) =>
          isRecord(source) && typeof source.work_id === "number" && typeof source.title === "string",
        )
      ) return value as ChatStreamEvent;
      break;
    case "delta":
      if (typeof value.text === "string") return value as ChatStreamEvent;
      break;
    case "done":
      if (isRecord(value.timing_ms)) return value as ChatStreamEvent;
      break;
    case "error":
      if (typeof value.message === "string") return value as ChatStreamEvent;
      break;
  }
  return invalid("回答流事件");
}
