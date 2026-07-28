export const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

function errorMessage(payload: unknown, fallback: string) {
  if (!payload || typeof payload !== "object") return fallback;
  const body = payload as { detail?: unknown; message?: unknown };
  const detail = body.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const value = detail as { message?: unknown; code?: unknown };
    if (typeof value.message === "string") return value.message;
    if (typeof value.code === "string") return value.code;
  }
  return typeof body.message === "string" ? body.message : fallback;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method || "GET").toUpperCase();
  const isForm = init.body instanceof FormData;
  const options: RequestInit = {
    ...init,
    cache: "no-store",
    headers: {
      ...(isForm ? {} : { "Content-Type": "application/json" }),
      "X-Requested-With": "tokbrain-local",
      ...init.headers,
    },
  };
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, options);
  } catch {
    if (method === "GET") {
      await new Promise((resolve) => window.setTimeout(resolve, 300));
      try {
        response = await fetch(`${API_BASE}${path}`, options);
      } catch {
        throw new ApiError(0, "无法连接本机后端，请确认服务仍在运行");
      }
    } else {
      throw new ApiError(0, "本机后端未响应，请刷新确认后再重试，避免重复提交");
    }
  }
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(response.status, errorMessage(payload, `请求失败 (${response.status})`));
  }
  return payload as T;
}

export type Probe = {
  probe: string;
  status: "healthy" | "degraded" | "down";
  message: string;
  details: Record<string, unknown>;
};

export type Health = {
  overall: "healthy" | "degraded" | "down";
  summary: string;
  checked_at: string;
  probes: Probe[];
};

export type RuntimeSettings = {
  daily_media_minutes_limit: number;
  daily_llm_token_limit: number;
  monthly_warning_cny: number;
  scene_threshold: number;
  max_scene_candidates: number;
  max_keyframes: number;
  min_keyframe_gap_seconds: number;
  dpapi_warning: string;
  has_dashscope_key: boolean;
  has_bss_credentials: boolean;
  has_f2_cookie: boolean;
  security_cleanup_required: boolean;
  security_cleanup_message: string;
  default_answer_format: "rich" | "markdown" | "plain";
  summary_prompt: string;
  default_summary_prompt: string;
  processing_model: string;
  chat_fast_model: string;
  chat_deep_model: string;
  processing_model_options: string[];
  chat_model_options: string[];
  ocr_model: string;
  asr_model: string;
  embedding_model: string;
  import_batch_limit: number;
  import_daily_limit: number;
  import_worker_count: number;
  import_network_concurrency: number;
  import_cooldown_min_seconds: number;
  import_cooldown_max_seconds: number;
};

export type Usage = {
  month_estimated_cny: number;
  official_billed_cny: number | null;
  official_data_as_of: string | null;
  official_status: string;
  daily_works_used: number;
  daily_works_reserved: number;
  daily_links_used: number;
  daily_links_limit: number;
  daily_media_minutes_used: number;
  daily_media_minutes_reserved: number;
  daily_media_minutes_limit: number;
  daily_llm_tokens_used: number;
  daily_llm_tokens_reserved: number;
  daily_llm_tokens_limit: number;
  warning_reached: boolean;
  estimate_notice: string;
};

export type Job = {
  id: string;
  job_type: string;
  state: string;
  message: string;
  total_items: number;
  processed_items: number;
  failed_items: number;
  cancelled_items: number;
  deferred_items: number;
  cancel_requested: boolean;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  progress: Record<string, unknown>;
};

export type ImportItem = {
  id: number;
  ordinal: number;
  input_url: string;
  canonical_url: string | null;
  platform_work_id: string | null;
  kind: string | null;
  title: string | null;
  author_name: string | null;
  duration_seconds: number;
  cover_url: string | null;
  status: string;
  error_code: string | null;
  error_message: string | null;
  existing_work_id: number | null;
  worker_id: number | null;
  local_asset_count: number;
  has_public_media: boolean;
  download_permission: "allowed" | "denied" | "unknown";
  processing_mode: "full_media" | "subtitle_or_audio";
  has_audio_or_subtitle: boolean;
};

export type ImportBatch = {
  id: string;
  job_id: string;
  state: string;
  total_items: number;
  cancel_requested: boolean;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  completed_at: string | null;
  progress: Record<string, number>;
  workers: Array<{ worker_id: number; item_id: number; title: string }>;
  remaining_daily: number;
  daily_limit: number;
  circuit: {
    active: boolean;
    expires_at: string | null;
    error_code: string | null;
    message: string | null;
  };
  items: ImportItem[];
};

export type Collection = {
  id: number;
  key: string;
  title: string;
  cover_url?: string | null;
  item_count: number;
  local_item_count: number;
  pending_count: number;
  issue_count: number;
};

export type LibrarySummary = {
  candidate_count: number;
  selected_count: number;
  local_item_count: number;
  issue_count: number;
  archived_count: number;
  known_distinct_count: number;
  remote_folder_item_sum: number;
};

export type Work = {
  id: number;
  platform_work_id: string;
  kind: string;
  title: string;
  author_name?: string;
  duration_seconds: number;
  cover_url?: string;
  source_url?: string;
  processing_state: string;
  library_state: string;
  selected: boolean;
  process_error?: string;
  error_code?: string;
  last_seen_at: string;
  collections: string[];
  summary_state: "missing" | "ready" | "failed" | "generating";
  summary_excerpt?: string | null;
};

export type WorksPage = {
  items: Work[];
  total: number;
  selected_count: number;
  account_selected_count: number;
  next_offset: number | null;
};

export type ChatTurn = { role: "user" | "assistant"; content: string };
export type ChatSource = {
  work_id: number;
  platform_work_id: string;
  title: string;
  collection: string | null;
  timestamp_seconds: number | null;
  external_url: string | null;
  source_kind: string;
};
export type ChatAnswer = { answer: string; sources: ChatSource[] };
export type ChatStreamEvent =
  | { type: "stage"; stage: string; message: string }
  | { type: "sources"; sources: ChatSource[] }
  | { type: "delta"; text: string }
  | { type: "done"; timing_ms: Record<string, number | null> }
  | { type: "error"; message: string };

export type WorkSummaryDetail = {
  work: {
    id: number;
    platform_work_id: string;
    title: string;
    author_name?: string | null;
    cover_url?: string | null;
    source_url?: string | null;
    kind: string;
    duration_seconds: number;
    collections: string[];
  };
  summary: {
    status: string;
    one_sentence: string;
    sections: Array<{ kind: string; title: string; body: string }>;
    tags: string[];
    asset_ids: string[];
    generated_at?: string | null;
    model: string;
  };
  assets: Array<{ name: string; url: string }>;
};

export type ObsidianManifest = {
  items: Array<{
    work_id: number;
    platform_work_id: string;
    filename: string;
    markdown: string;
    assets: Array<{ name: string; export_name: string; url: string }>;
  }>;
};

export const api = {
  health: () => request<Health>("/api/system-health"),
  healthProbe: (probe: string) => request<Probe>(`/api/system-health/probes/${probe}`),
  settings: () => request<RuntimeSettings>("/api/settings"),
  saveSettings: (body: Record<string, unknown>) =>
    request<RuntimeSettings>("/api/settings", { method: "PUT", body: JSON.stringify(body) }),
  clearAllKeys: () => request<RuntimeSettings>("/api/settings/secrets", { method: "DELETE" }),
  usage: () => request<Usage>("/api/settings/usage"),
  refreshOfficialBill: () =>
    request<{ status: string; message: string }>("/api/settings/official-bill/refresh", { method: "POST" }),
  createImportBatch: (text: string) =>
    request<{ batch_id: string; job_id: string; accepted_count: number; rejected_count: number }>(
      "/api/import-batches",
      { method: "POST", body: JSON.stringify({ text }) },
    ),
  importBatch: (id: string) => request<ImportBatch>(`/api/import-batches/${id}`),
  cancelImportBatch: (id: string) =>
    request<ImportBatch>(`/api/import-batches/${id}/cancel`, { method: "POST" }),
  confirmImportBatch: (id: string, itemIds: number[]) =>
    request<Job>(`/api/import-batches/${id}/confirm`, {
      method: "POST",
      body: JSON.stringify({ item_ids: itemIds }),
    }),
  uploadImportAssets: (itemId: number, files: File[]) => {
    const form = new FormData();
    files.forEach((file) => form.append("files", file));
    return request<{ item_id: number; status: string; kind: string; message: string }>(
      `/api/import-items/${itemId}/assets`,
      { method: "POST", body: form },
    );
  },
  jobs: () => request<Job[]>("/api/jobs"),
  job: (id: string) => request<Job>(`/api/jobs/${id}`),
  cancelJob: (id: string) => request<Job>(`/api/jobs/${id}/cancel`, { method: "POST" }),
  collections: () =>
    request<{ items: Collection[]; summary: LibrarySummary }>("/api/library/collections"),
  createCollection: (title: string) =>
    request<Collection>("/api/library/collections", {
      method: "POST",
      body: JSON.stringify({ title }),
    }),
  addWorksToCollection: (collectionId: number, workIds: number[]) =>
    request<{ collection_id: number; title: string; requested: number; eligible: number; added: number }>(
      `/api/library/collections/${collectionId}/works`,
      { method: "POST", body: JSON.stringify({ work_ids: workIds }) },
    ),
  works: (state: "pending" | "in_library" | "issues" | "archived", collectionId?: number, offset = 0) => {
    const query = new URLSearchParams({ library_state: state, offset: String(offset), limit: "60" });
    if (collectionId != null) query.set("collection_id", String(collectionId));
    return request<WorksPage>(`/api/library/works?${query.toString()}`);
  },
  restore: (id: number) => request(`/api/library/works/${id}/restore`, { method: "POST" }),
  retry: (id: number) => request(`/api/library/works/${id}/retry`, { method: "POST" }),
  retryBatch: (body: { work_ids?: number[]; error_code?: string; collection_id?: number }) =>
    request<{ changed: number; model_called: boolean }>("/api/library/works/retry-batch", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  summarize: (workIds: number[]) =>
    request<Job>("/api/library/summaries/jobs", {
      method: "POST",
      body: JSON.stringify({ work_ids: workIds }),
    }),
  workSummary: (id: number) => request<WorkSummaryDetail>(`/api/library/works/${id}/summary`),
  obsidianManifest: (workIds: number[]) =>
    request<ObsidianManifest>("/api/library/obsidian/manifest", {
      method: "POST",
      body: JSON.stringify({ work_ids: workIds }),
    }),
  workLocation: (id: number, pageSize = 60) =>
    request<{ work_id: number; index: number; offset: number; page_size: number; total: number }>(
      `/api/library/works/${id}/location?page_size=${pageSize}`,
    ),
  remove: (id: number) => request(`/api/library/works/${id}`, { method: "DELETE" }),
  askStream: async (
    question: string,
    history: ChatTurn[],
    mode: "fast" | "deep",
    onEvent: (event: ChatStreamEvent) => void,
    signal?: AbortSignal,
  ) => {
    let response: Response;
    try {
      response = await fetch(`${API_BASE}/api/chat/ask/stream`, {
        method: "POST",
        cache: "no-store",
        signal,
        headers: { "Content-Type": "application/json", "X-Requested-With": "tokbrain-local" },
        body: JSON.stringify({ question, top_k: mode === "deep" ? 8 : 6, history: history.slice(-12), mode }),
      });
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === "AbortError") throw reason;
      throw new ApiError(0, "无法连接本机后端，请确认服务仍在运行");
    }
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      throw new ApiError(response.status, errorMessage(payload, `请求失败 (${response.status})`));
    }
    if (!response.body) throw new ApiError(0, "浏览器无法读取流式回答");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) if (line.trim()) onEvent(JSON.parse(line) as ChatStreamEvent);
      if (done) break;
    }
    if (buffer.trim()) onEvent(JSON.parse(buffer) as ChatStreamEvent);
  },
};
