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

export type ChatAnswer = {
  answer: string;
  sources: ChatSource[];
};

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
