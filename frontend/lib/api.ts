import type * as Contract from "./contracts";

export type {
  ChatAnswer,
  ChatSource,
  ChatStreamEvent,
  ChatTurn,
  Collection,
  Health,
  ImportBatch,
  ImportItem,
  Job,
  LibrarySummary,
  ObsidianManifest,
  Probe,
  RuntimeSettings,
  Usage,
  Work,
  WorksPage,
  WorkSummaryDetail,
} from "./contracts";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";

const LOCAL_REQUEST_HEADER = "tokbrain-local";
const GET_RETRY_DELAY_MS = 300;

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

  private async fetchWithSafeRetry(
    path: string,
    init: RequestInit,
  ): Promise<Response> {
    const options = requestOptions(init);
    const method = (options.method || "GET").toUpperCase();
    try {
      return await fetch(this.endpoint(path), options);
    } catch {
      if (method !== "GET") {
        throw new ApiError(
          0,
          "本机后端未响应，请刷新确认后再重试，避免重复提交",
        );
      }
    }

    await wait(GET_RETRY_DELAY_MS);
    try {
      return await fetch(this.endpoint(path), options);
    } catch {
      throw new ApiError(0, "无法连接本机后端，请确认服务仍在运行");
    }
  }

  private async request<T>(
    path: string,
    init: RequestInit = {},
  ): Promise<T> {
    const response = await this.fetchWithSafeRetry(path, init);
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
    return this.request("/api/system-health");
  }

  healthProbe(probe: string): Promise<Contract.Probe> {
    return this.request(`/api/system-health/probes/${encodeURIComponent(probe)}`);
  }

  settings(): Promise<Contract.RuntimeSettings> {
    return this.request("/api/settings");
  }

  saveSettings(
    body: Record<string, unknown>,
  ): Promise<Contract.RuntimeSettings> {
    return this.request("/api/settings", {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }

  clearAllKeys(): Promise<Contract.RuntimeSettings> {
    return this.request("/api/settings/secrets", { method: "DELETE" });
  }

  usage(): Promise<Contract.Usage> {
    return this.request("/api/settings/usage");
  }

  refreshOfficialBill(): Promise<{ status: string; message: string }> {
    return this.request("/api/settings/official-bill/refresh", {
      method: "POST",
    });
  }

  createImportBatch(text: string): Promise<{
    batch_id: string;
    job_id: string;
    accepted_count: number;
    rejected_count: number;
  }> {
    return this.request("/api/import-batches", {
      method: "POST",
      body: JSON.stringify({ text }),
    });
  }

  importBatch(id: string): Promise<Contract.ImportBatch> {
    return this.request(`/api/import-batches/${encodeURIComponent(id)}`);
  }

  cancelImportBatch(id: string): Promise<Contract.ImportBatch> {
    return this.request(`/api/import-batches/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
    });
  }

  confirmImportBatch(
    id: string,
    itemIds: number[],
  ): Promise<Contract.Job> {
    return this.request(`/api/import-batches/${encodeURIComponent(id)}/confirm`, {
      method: "POST",
      body: JSON.stringify({ item_ids: itemIds }),
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
    });
  }

  jobs(): Promise<Contract.Job[]> {
    return this.request("/api/jobs");
  }

  job(id: string): Promise<Contract.Job> {
    return this.request(`/api/jobs/${encodeURIComponent(id)}`);
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
    return this.request("/api/library/collections");
  }

  createCollection(title: string): Promise<Contract.Collection> {
    return this.request("/api/library/collections", {
      method: "POST",
      body: JSON.stringify({ title }),
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
    state: "pending" | "in_library" | "issues" | "archived",
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
    return this.request(`/api/library/works?${query}`);
  }

  restore(id: number): Promise<unknown> {
    return this.request(`/api/library/works/${id}/restore`, { method: "POST" });
  }

  retry(id: number): Promise<unknown> {
    return this.request(`/api/library/works/${id}/retry`, { method: "POST" });
  }

  retryBatch(body: {
    work_ids?: number[];
    error_code?: string;
    collection_id?: number;
  }): Promise<{ changed: number; model_called: boolean }> {
    return this.request("/api/library/works/retry-batch", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  summarize(workIds: number[]): Promise<Contract.Job> {
    return this.request("/api/library/summaries/jobs", {
      method: "POST",
      body: JSON.stringify({ work_ids: workIds }),
    });
  }

  workSummary(id: number): Promise<Contract.WorkSummaryDetail> {
    return this.request(`/api/library/works/${id}/summary`);
  }

  obsidianManifest(workIds: number[]): Promise<Contract.ObsidianManifest> {
    return this.request("/api/library/obsidian/manifest", {
      method: "POST",
      body: JSON.stringify({ work_ids: workIds }),
    });
  }

  workLocation(
    id: number,
    pageSize = 60,
  ): Promise<{
    work_id: number;
    index: number;
    offset: number;
    page_size: number;
    total: number;
  }> {
    return this.request(
      `/api/library/works/${id}/location?page_size=${pageSize}`,
    );
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
    let response: Response;
    try {
      response = await fetch(this.endpoint("/api/chat/ask/stream"), {
        method: "POST",
        cache: "no-store",
        signal,
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
      if (reason instanceof DOMException && reason.name === "AbortError") {
        throw reason;
      }
      throw new ApiError(0, "无法连接本机后端，请确认服务仍在运行");
    }

    if (!response.ok) {
      const payload = await parsedBody(response);
      throw new ApiError(
        response.status,
        messageFromPayload(payload, `请求失败 (${response.status})`),
      );
    }
    if (!response.body) {
      throw new ApiError(0, "浏览器无法读取流式回答");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let pending = "";
    while (true) {
      const chunk = await reader.read();
      pending += decoder.decode(chunk.value || new Uint8Array(), {
        stream: !chunk.done,
      });
      const lines = pending.split("\n");
      pending = lines.pop() || "";
      for (const line of lines) {
        if (line.trim()) onEvent(JSON.parse(line) as Contract.ChatStreamEvent);
      }
      if (chunk.done) break;
    }
    if (pending.trim()) {
      onEvent(JSON.parse(pending) as Contract.ChatStreamEvent);
    }
  }
}

export const api = new LocalApiClient(API_BASE);
