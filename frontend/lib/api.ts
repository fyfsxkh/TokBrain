import type * as Contract from "./contracts";
import {
  parseChatStreamEvent,
  parseCollections,
  parseHealth,
  parseImportBatch,
  parseJobs,
  parseProbe,
  parseRuntimeSettings,
  parseWorksPage,
  parseWorkSummaryDetail,
} from "./contractGuards";

export type {
  ChatSource,
  ChatStreamEvent,
  ChatTurn,
  Collection,
  Health,
  ImportBatch,
  ImportBatchCreated,
  ImportBatchCreateRequest,
  ImportItem,
  ImportItemUpdate,
  ImportItemUpdateResult,
  IntegrationTokenCreated,
  IntegrationTokenStatus,
  Job,
  LibrarySummary,
  LocalImportBatchRequest,
  LocalVideoUploadResult,
  PackageImportBatchRequest,
  ObsidianManifest,
  Probe,
  RuntimeSettings,
  RuntimeSettingsUpdate,
  Usage,
  Work,
  WorkSupplementResult,
  WorksPage,
  WorkSummaryDetail,
} from "./contracts";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";

const LOCAL_REQUEST_HEADER = "tokbrain-local";
const GET_RETRY_DELAY_MS = 300;
const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;
const UPLOAD_REQUEST_TIMEOUT_MS = 30 * 60_000;
const STREAM_INACTIVITY_TIMEOUT_MS = 60_000;

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function messageFromPayload(payload: unknown, fallback: string): string {
  if (payload === null || typeof payload !== "object") return fallback;
  const body = payload as {
    detail?: unknown;
    message?: unknown;
  };
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.detail)) {
    const first = body.detail.find((item) => item && typeof item === "object") as
      | { msg?: unknown; message?: unknown; loc?: unknown }
      | undefined;
    const message = typeof first?.message === "string"
      ? first.message
      : typeof first?.msg === "string"
        ? first.msg
        : "";
    if (message) {
      const location = Array.isArray(first?.loc)
        ? first.loc.filter((item) => !["body", "query", "path", "header"].includes(String(item))).join(".")
        : "";
      return location ? `${location}：${message}` : message;
    }
  }
  if (body.detail && typeof body.detail === "object") {
    const detail = body.detail as { message?: unknown; code?: unknown };
    if (typeof detail.message === "string") return detail.message;
    if (typeof detail.code === "string") return detail.code;
  }
  return typeof body.message === "string" ? body.message : fallback;
}

function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function requestOptions(init: RequestInit): RequestInit {
  const headers = new Headers(init.headers);
  headers.set("X-Requested-With", LOCAL_REQUEST_HEADER);
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  return { ...init, cache: "no-store", headers };
}

async function parsedBody(response: Response): Promise<unknown> {
  return response.json().catch(() => null);
}

class LocalApiClient {
  constructor(private readonly baseUrl: string) {}

  private endpoint(path: string): string {
    return `${this.baseUrl}${path}`;
  }

  private async fetchOnce(
    path: string,
    options: RequestInit,
    timeoutMs: number,
  ): Promise<Response> {
    if (!timeoutMs) return fetch(this.endpoint(path), options);
    const controller = new AbortController();
    const forwardAbort = () => controller.abort(options.signal?.reason);
    if (options.signal?.aborted) forwardAbort();
    else options.signal?.addEventListener("abort", forwardAbort, { once: true });
    const timer = window.setTimeout(
      () => controller.abort(new DOMException("请求超时", "TimeoutError")),
      timeoutMs,
    );
    try {
      return await fetch(this.endpoint(path), { ...options, signal: controller.signal });
    } finally {
      window.clearTimeout(timer);
      options.signal?.removeEventListener("abort", forwardAbort);
    }
  }

  private async fetchWithSafeRetry(
    path: string,
    init: RequestInit,
    timeoutMs: number,
  ): Promise<Response> {
    const options = requestOptions(init);
    const method = (options.method || "GET").toUpperCase();
    try {
      return await this.fetchOnce(path, options, timeoutMs);
    } catch (cause) {
      if (options.signal?.aborted) throw cause;
      if (method !== "GET") {
        throw new ApiError(
          0,
          cause instanceof DOMException && cause.name === "TimeoutError"
            ? "请求本机后端超时，请刷新确认结果后再重试，避免重复提交"
            : "本机后端未响应，请刷新确认后再重试，避免重复提交",
        );
      }
    }

    await wait(GET_RETRY_DELAY_MS);
    try {
      return await this.fetchOnce(path, options, timeoutMs);
    } catch (cause) {
      if (options.signal?.aborted) throw cause;
      throw new ApiError(
        0,
        cause instanceof DOMException && cause.name === "TimeoutError"
          ? "请求本机后端超时，请确认服务仍在运行"
          : "无法连接本机后端，请确认服务仍在运行",
      );
    }
  }

  private async request<T>(
    path: string,
    init: RequestInit = {},
    timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
  ): Promise<T> {
    const response = await this.fetchWithSafeRetry(path, init, timeoutMs);
    const payload = await parsedBody(response);
    if (!response.ok) {
      throw new ApiError(
        response.status,
        messageFromPayload(payload, `请求失败 (${response.status})`),
      );
    }
    return payload as T;
  }

  health(): Promise<Contract.Health> {
    return this.request<unknown>("/api/system-health").then(parseHealth);
  }

  healthProbe(probe: string): Promise<Contract.Probe> {
    return this.request<unknown>(`/api/system-health/probes/${encodeURIComponent(probe)}`).then(parseProbe);
  }

  settings(): Promise<Contract.RuntimeSettings> {
    return this.request<unknown>("/api/settings").then(parseRuntimeSettings);
  }

  saveSettings(
    body: Contract.RuntimeSettingsUpdate,
  ): Promise<Contract.RuntimeSettings> {
    return this.request<unknown>("/api/settings", {
      method: "PUT",
      body: JSON.stringify(body),
    }).then(parseRuntimeSettings);
  }

  clearAllKeys(): Promise<Contract.RuntimeSettings> {
    return this.request<unknown>("/api/settings/secrets", { method: "DELETE" }).then(parseRuntimeSettings);
  }

  usage(): Promise<Contract.Usage> {
    return this.request("/api/settings/usage");
  }

  refreshOfficialBill(): Promise<{ status: string; message: string }> {
    return this.request("/api/settings/official-bill/refresh", {
      method: "POST",
    });
  }

  createImportBatch(
    body: Contract.ImportBatchCreateRequest,
  ): Promise<Contract.ImportBatchCreated> {
    return this.request("/api/import-batches", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  importBatch(id: string): Promise<Contract.ImportBatch> {
    return this.request<unknown>(`/api/import-batches/${encodeURIComponent(id)}`).then(parseImportBatch);
  }

  createLocalImportBatch(
    body: Contract.LocalImportBatchRequest,
  ): Promise<Contract.ImportBatch> {
    return this.request<unknown>("/api/local-import-batches", {
      method: "POST",
      body: JSON.stringify(body),
    }).then(parseImportBatch);
  }

  createPackageImportBatch(
    body: Contract.PackageImportBatchRequest,
  ): Promise<Contract.ImportBatch> {
    return this.request<unknown>("/api/package-import-batches", {
      method: "POST",
      body: JSON.stringify(body),
    }).then(parseImportBatch);
  }

  packageImportBatch(id: string): Promise<Contract.ImportBatch> {
    return this.request<unknown>(`/api/package-import-batches/${encodeURIComponent(id)}`).then(parseImportBatch);
  }

  uploadPackageImportFile(
    batchId: string,
    fileId: string,
    file: File,
  ): Promise<{ status: string; sha256: string; idempotent: boolean }> {
    const body = new FormData();
    body.append("file", file);
    return this.request<{ status: string; sha256: string; idempotent: boolean }>(
      `/api/package-import-batches/${encodeURIComponent(batchId)}/files/${encodeURIComponent(fileId)}`,
      { method: "PUT", body },
      UPLOAD_REQUEST_TIMEOUT_MS,
    );
  }

  analyzePackageImportBatch(id: string): Promise<Contract.ImportBatch> {
    return this.request<unknown>(
      `/api/package-import-batches/${encodeURIComponent(id)}/analyze`,
      { method: "POST" },
    ).then(parseImportBatch);
  }

  uploadLocalImportVideo(
    batchId: string,
    itemId: number,
    file: File,
  ): Promise<Contract.LocalVideoUploadResult> {
    const body = new FormData();
    body.append("file", file);
    return this.request(
      `/api/local-import-batches/${encodeURIComponent(batchId)}/items/${itemId}/video`,
      { method: "PUT", body },
      UPLOAD_REQUEST_TIMEOUT_MS,
    );
  }

  updateImportItem(
    itemId: number,
    body: Contract.ImportItemUpdate,
  ): Promise<Contract.ImportItemUpdateResult> {
    return this.request(`/api/import-items/${itemId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  cancelImportBatch(id: string): Promise<Contract.ImportBatch> {
    return this.request<unknown>(`/api/import-batches/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
    }).then(parseImportBatch);
  }

  confirmImportBatch(
    id: string,
    items: Array<{ item_id: number; collection_id?: number }>,
  ): Promise<Contract.ImportConfirmation> {
    const assigned = items.filter(
      (item): item is { item_id: number; collection_id: number } =>
        item.collection_id !== undefined,
    );
    return this.request(`/api/import-batches/${encodeURIComponent(id)}/confirm`, {
      method: "POST",
      body: JSON.stringify({
        item_ids: items
          .filter((item) => item.collection_id === undefined)
          .map((item) => item.item_id),
        items: assigned,
      }),
    });
  }

  uploadImportAssets(
    itemId: number,
    files: File[],
  ): Promise<{ item_id: number; status: string; kind: string; message: string }> {
    const body = new FormData();
    for (const file of files) body.append("files", file);
    return this.request(`/api/import-items/${itemId}/assets`, {
      method: "POST",
      body,
    }, UPLOAD_REQUEST_TIMEOUT_MS);
  }

  removeImportItem(itemId: number): Promise<Contract.ImportBatch> {
    return this.request<unknown>(`/api/import-items/${itemId}`, { method: "DELETE" }).then(parseImportBatch);
  }

  integrationToken(): Promise<Contract.IntegrationTokenStatus> {
    return this.request("/api/settings/integration-token");
  }

  createIntegrationToken(): Promise<Contract.IntegrationTokenCreated> {
    return this.request("/api/settings/integration-token", { method: "POST" });
  }

  revokeIntegrationToken(): Promise<Contract.IntegrationTokenStatus> {
    return this.request("/api/settings/integration-token", { method: "DELETE" });
  }

  jobs(): Promise<Contract.Job[]> {
    return this.request<unknown>("/api/jobs").then(parseJobs);
  }

  cancelJob(id: string): Promise<Contract.Job> {
    return this.request(`/api/jobs/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
    });
  }

  collections(): Promise<{
    items: Contract.Collection[];
    summary: Contract.LibrarySummary;
  }> {
    return this.request<unknown>("/api/library/collections").then(parseCollections);
  }

  createCollection(title: string): Promise<Contract.Collection> {
    return this.request("/api/library/collections", {
      method: "POST",
      body: JSON.stringify({ title }),
    });
  }

  updateCollectionSummaryPrompt(
    collectionId: number,
    summaryPrompt: string | null,
  ): Promise<Pick<Contract.Collection, "id" | "key" | "title" | "cover_url" | "summary_prompt">> {
    return this.request(`/api/library/collections/${collectionId}`, {
      method: "PUT",
      body: JSON.stringify({ summary_prompt: summaryPrompt }),
    });
  }

  addWorksToCollection(
    collectionId: number,
    workIds: number[],
  ): Promise<{
    collection_id: number;
    title: string;
    requested: number;
    eligible: number;
    added: number;
  }> {
    return this.request(`/api/library/collections/${collectionId}/works`, {
      method: "POST",
      body: JSON.stringify({ work_ids: workIds }),
    });
  }

  works(
    state: "pending" | "in_library" | "supplement" | "issues" | "archived",
    collectionId?: number,
    offset = 0,
  ): Promise<Contract.WorksPage> {
    const query = new URLSearchParams({
      library_state: state,
      offset: String(offset),
      limit: "60",
    });
    if (collectionId !== undefined) {
      query.set("collection_id", String(collectionId));
    }
    return this.request<unknown>(`/api/library/works?${query}`).then(parseWorksPage);
  }

  uploadWorkSupplement(
    id: number,
    files: File[],
  ): Promise<Contract.WorkSupplementResult> {
    const body = new FormData();
    body.append("rights_attested", "true");
    for (const file of files) body.append("files", file, file.name);
    return this.request(`/api/library/works/${id}/supplement`, {
      method: "POST",
      body,
    }, UPLOAD_REQUEST_TIMEOUT_MS);
  }

  restore(id: number): Promise<unknown> {
    return this.request(`/api/library/works/${id}/restore`, { method: "POST" });
  }

  retry(id: number): Promise<unknown> {
    return this.request(`/api/library/works/${id}/retry`, { method: "POST" });
  }

  ingest(workIds: number[]): Promise<Contract.Job> {
    return this.request("/api/library/ingest/jobs", {
      method: "POST",
      body: JSON.stringify({ work_ids: workIds }),
    });
  }

  summarize(workIds: number[]): Promise<Contract.Job> {
    return this.request("/api/library/summaries/jobs", {
      method: "POST",
      body: JSON.stringify({ work_ids: workIds }),
    });
  }

  workSummary(id: number): Promise<Contract.WorkSummaryDetail> {
    return this.request<unknown>(`/api/library/works/${id}/summary`).then(parseWorkSummaryDetail);
  }

  obsidianManifest(workIds: number[]): Promise<Contract.ObsidianManifest> {
    return this.request("/api/library/obsidian/manifest", {
      method: "POST",
      body: JSON.stringify({ work_ids: workIds }),
    });
  }

  remove(id: number): Promise<unknown> {
    return this.request(`/api/library/works/${id}`, { method: "DELETE" });
  }

  async askStream(
    question: string,
    history: Contract.ChatTurn[],
    mode: "fast" | "deep",
    onEvent: (event: Contract.ChatStreamEvent) => void,
    signal?: AbortSignal,
  ): Promise<void> {
    const controller = new AbortController();
    let timedOut = false;
    let inactivityTimer = 0;
    const forwardAbort = () => controller.abort(signal?.reason);
    const resetInactivityTimer = () => {
      window.clearTimeout(inactivityTimer);
      inactivityTimer = window.setTimeout(() => {
        timedOut = true;
        controller.abort(new DOMException("回答流超时", "TimeoutError"));
      }, STREAM_INACTIVITY_TIMEOUT_MS);
    };
    const cleanup = () => {
      window.clearTimeout(inactivityTimer);
      signal?.removeEventListener("abort", forwardAbort);
    };
    if (signal?.aborted) forwardAbort();
    else signal?.addEventListener("abort", forwardAbort, { once: true });
    resetInactivityTimer();
    let response: Response;
    try {
      response = await fetch(this.endpoint("/api/chat/ask/stream"), {
        method: "POST",
        cache: "no-store",
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          "X-Requested-With": LOCAL_REQUEST_HEADER,
        },
        body: JSON.stringify({
          question,
          top_k: mode === "deep" ? 8 : 6,
          history: history.slice(-12),
          mode,
        }),
      });
    } catch (reason) {
      cleanup();
      if (timedOut) throw new ApiError(0, "回答流等待超时，请重试");
      if (reason instanceof DOMException && reason.name === "AbortError") {
        throw reason;
      }
      throw new ApiError(0, "无法连接本机后端，请确认服务仍在运行");
    }

    if (!response.ok) {
      const payload = await parsedBody(response);
      cleanup();
      throw new ApiError(
        response.status,
        messageFromPayload(payload, `请求失败 (${response.status})`),
      );
    }
    if (!response.body) {
      cleanup();
      throw new ApiError(0, "浏览器无法读取流式回答");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let pending = "";
    try {
      while (true) {
        const chunk = await reader.read();
        resetInactivityTimer();
        pending += decoder.decode(chunk.value || new Uint8Array(), {
          stream: !chunk.done,
        });
        const lines = pending.split("\n");
        pending = lines.pop() || "";
        for (const line of lines) {
          if (line.trim()) onEvent(parseChatStreamEvent(JSON.parse(line)));
        }
        if (chunk.done) break;
      }
      if (pending.trim()) {
        onEvent(parseChatStreamEvent(JSON.parse(pending)));
      }
    } catch (reason) {
      if (timedOut) throw new ApiError(0, "回答流等待超时，请重试");
      throw reason;
    } finally {
      cleanup();
    }
  }
}

export const api = new LocalApiClient(API_BASE);
