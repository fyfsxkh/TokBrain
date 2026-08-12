"use client";

/* eslint-disable @next/next/no-img-element -- public covers and local assets are user-selected sources. */

import dynamic from "next/dynamic";
import { DragEvent, FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { Library } from "../components/Library";
import { Settings, ThemePicker } from "../components/Settings";
import {
  api,
  Collection,
  Health,
  ImportBatch,
  ImportItem,
  ImportItemUpdate,
  Job,
  LibrarySummary,
  RuntimeSettings,
  Usage,
  WorksPage,
} from "../lib/api";
import type { ChatMessage } from "../components/Chat";
import { localAssetUrl } from "../lib/assets";
import { isUserCancelled, reason } from "../lib/errors";
import { removeConfirmedImportItems } from "../lib/importBatches";
import {
  rememberImportBatch,
  rememberLocalImportBatch,
  rememberLocalImportRightsAttestation,
  rememberPackageBatch,
  storedImportBatchIds,
  storedLocalImportRightsAttestation,
  storedPackageBatches,
} from "../lib/importBatchStorage";
import {
  applyChatStreamEvent,
  createChatStreamState,
  visibleChatStreamContent,
} from "../lib/chatStream";
import {
  clearLibraryReturnContext,
  readLibraryReturnContext,
} from "../lib/libraryReturn";
import { mergeWorksPage } from "../lib/libraryPagination";
import { activeProcessingJobIds, didAnyJobReachTerminal, isActiveProcessingJob, operationIncludesJob } from "../lib/jobPolling";
import type { LibraryState } from "../lib/uiTypes";
import { useTheme } from "../themes/ThemeProvider";

const Chat = dynamic(
  () => import("../components/Chat").then((module) => module.Chat),
  { loading: () => <p className="muted">正在加载对话…</p>, ssr: false },
);

type Tab = "import" | "library" | "chat" | "settings";
type LocalUploadState = "pending" | "creating" | "uploading" | "ready" | "duplicate" | "failed";
type LocalVideoDraft = {
  clientItemId: string;
  file: File;
  title: string;
  targetCollectionId: number | null;
  status: LocalUploadState;
  message: string;
  batchId?: string;
  serverItemId?: number;
};
type PackageFileDraft = {
  clientFileId: string;
  file: File;
  relativePath: string;
  status: "pending" | "uploading" | "uploaded" | "failed";
};

const EMPTY_SUMMARY: LibrarySummary = {
  candidate_count: 0,
  selected_count: 0,
  local_item_count: 0,
  issue_count: 0,
  archived_count: 0,
  known_distinct_count: 0,
  remote_folder_item_sum: 0,
};
const EMPTY_WORKS: WorksPage = {
  items: [],
  total: 0,
  selected_count: 0,
  account_selected_count: 0,
  next_offset: null,
};
const TERMINAL_BATCH_STATES = new Set(["succeeded", "partial", "failed", "cancelled"]);

function batchIsTerminal(batch: ImportBatch) {
  return TERMINAL_BATCH_STATES.has(batch.state);
}

function loadImportBatch(batch: Pick<ImportBatch, "id" | "source_type">) {
  return batch.source_type === "package_upload"
    ? api.packageImportBatch(batch.id)
    : api.importBatch(batch.id);
}

function visibleErrorCode(value: string) {
  return value.startsWith("f2_") ? value.slice(3) : value;
}

const RETAINED_IMPORT_STATUSES = new Set(["ready", "needs_local_file"]);

function jobLabel(job: Job) {
  if (job.job_type === "link_preview") return "视频链接预检";
  if (job.job_type === "ingest") return "作品入库";
  if (job.job_type === "summarize") return "整理作品精华";
  return "本地任务";
}

function stateLabel(state: string) {
  return (
    {
      queued: "等待中",
      running: "进行中",
      automating: "自动确认中",
      cancelling: "安全停止中",
      cancelled: "已中断",
      succeeded: "已完成",
      partial: "部分完成",
      failed: "失败",
    }[state] || state
  );
}

function itemStatus(item: ImportItem) {
  if (item.status === "duplicate") {
    return item.error_code === "duplicate_input" ? "预检重复" : "已在知识库";
  }
  return (
    {
      queued: "排队中",
      resolving: `Worker ${item.worker_id || "?"} 解析中`,
      ready: "可以确认",
      needs_local_file: "需要本地补件",
      confirmed: "已加入待入库",
      failed: "解析失败",
      blocked: "因风控停止",
      cancelled: "已中断",
    }[item.status] || item.status
  );
}

function mediaPolicyLabel(item: ImportItem) {
  if (item.platform === "local") {
    return item.status === "duplicate" ? "本地视频指纹去重 · 已存在" : "本地视频 · 完整处理";
  }
  if (item.local_asset_count > 0) return "本地素材完整处理";
  if (item.download_permission === "allowed") return "作者允许下载 · 完整视频处理";
  if (item.has_audio_or_subtitle) return "作者未允许下载 · 仅字幕/音频";
  return "作者未允许下载或状态未知 · 仅基础信息";
}


function samePreviewWork(left: ImportItem, right: ImportItem) {
  if (
    left.platform_work_id
    && right.platform_work_id
    && left.platform_work_id === right.platform_work_id
  ) {
    return true;
  }
  return Boolean(
    left.normalized_url
    && right.normalized_url
    && left.normalized_url === right.normalized_url,
  );
}

export default function Home() {
  const { theme } = useTheme();
  const [tab, setTab] = useState<Tab>("import");
  const [health, setHealth] = useState<Health | null>(null);
  const [settings, setSettings] = useState<RuntimeSettings | null>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [collections, setCollections] = useState<Collection[]>([]);
  const [summary, setSummary] = useState<LibrarySummary>(EMPTY_SUMMARY);
  const [worksPage, setWorksPage] = useState<WorksPage>(EMPTY_WORKS);
  const [libraryLoading, setLibraryLoading] = useState(false);
  const [libraryState, setLibraryState] = useState<LibraryState>("in_library");
  const [collectionId, setCollectionId] = useState<number | null>(null);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [importText, setImportText] = useState("");
  const [rightsAttested, setRightsAttested] = useState(false);
  const [batch, setBatch] = useState<ImportBatch | null>(null);
  const [retainedBatches, setRetainedBatches] = useState<ImportBatch[]>([]);
  const [selectedItems, setSelectedItems] = useState<Set<number>>(new Set());
  const [itemCollections, setItemCollections] = useState<Map<number, number>>(new Map());
  const [confirmedWorkIds, setConfirmedWorkIds] = useState<number[]>([]);
  const [question, setQuestion] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [chatMode, setChatMode] = useState<"fast" | "deep">("fast");
  const submittedImportTexts = useRef(new Map<string, string>());
  const restoredLibraryPosition = useRef(false);
  const batchRef = useRef<ImportBatch | null>(null);
  const libraryRequestRef = useRef(0);
  const libraryLoadingRef = useRef(false);
  const worksPageLengthRef = useRef(worksPage.items.length);
  const activeOperationRef = useRef("");
  worksPageLengthRef.current = worksPage.items.length;

  useEffect(() => {
    batchRef.current = batch;
  }, [batch]);

  const loadHealth = useCallback(async () => {
    setHealth(await api.health());
  }, []);

  const loadSettings = useCallback(async () => {
    setSettings(await api.settings());
  }, []);

  const loadUsage = useCallback(async () => {
    setUsage(await api.usage());
  }, []);

  const loadJobs = useCallback(async () => {
    const next = await api.jobs();
    setJobs(next);
    return next;
  }, []);

  const loadCollections = useCallback(async () => {
    const groups = await api.collections();
    setCollections(groups.items);
    setSummary(groups.summary);
  }, []);

  const refreshSharedState = useCallback(async () => {
    await Promise.allSettled([
      loadSettings(),
      loadUsage(),
      loadJobs(),
      loadCollections(),
    ]);
  }, [loadCollections, loadJobs, loadSettings, loadUsage]);

  const loadLibrary = useCallback(
    async (append = false) => {
      if (append && libraryLoadingRef.current) return;
      const requestId = ++libraryRequestRef.current;
      libraryLoadingRef.current = true;
      setLibraryLoading(true);
      const offset = append ? worksPageLengthRef.current : 0;
      try {
        const page = await api.works(libraryState, collectionId ?? undefined, offset);
        if (requestId !== libraryRequestRef.current) return;
        setWorksPage((current) => mergeWorksPage(current, page, append));
      } catch (value) {
        if (requestId === libraryRequestRef.current) throw value;
      } finally {
        if (requestId === libraryRequestRef.current) {
          libraryLoadingRef.current = false;
          setLibraryLoading(false);
        }
      }
    },
    [collectionId, libraryState],
  );

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("tab") === "library") {
      setTab("library");
      const state = params.get("state");
      if (state && ["pending", "in_library", "supplement", "issues", "archived"].includes(state)) {
        setLibraryState(state as LibraryState);
      }
      const restoredCollectionId = Number(params.get("collection_id"));
      if (Number.isInteger(restoredCollectionId) && restoredCollectionId > 0) {
        setCollectionId(restoredCollectionId);
      }
    }
  }, []);

  useEffect(() => {
    Promise.allSettled([loadHealth(), refreshSharedState()]).catch(() => undefined);
  }, [loadHealth, refreshSharedState]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      setRightsAttested(storedLocalImportRightsAttestation());
    });
    return () => window.cancelAnimationFrame(frame);
  }, []);

  function changeRightsAttestation(attested: boolean) {
    setRightsAttested(attested);
    rememberLocalImportRightsAttestation(attested);
  }

  useEffect(() => {
    let cancelled = false;
    async function recoverImports() {
      const packageIds = new Set(storedPackageBatches());
      const ids = [...new Set([...storedImportBatchIds(), ...packageIds])];
      if (!ids.length) return;
      const settled = await Promise.allSettled(ids.map((id) =>
        packageIds.has(id) ? api.packageImportBatch(id) : api.importBatch(id),
      ));
      if (cancelled) return;
      const loaded = settled
        .filter((result): result is PromiseFulfilledResult<ImportBatch> => result.status === "fulfilled")
        .map((result) => result.value)
        .sort((left, right) => new Date(left.created_at).getTime() - new Date(right.created_at).getTime());
      const loadedIds = new Set(loaded.map((entry) => entry.id));
      const recovered = loaded
        .map((entry) => ({
          ...entry,
          items: batchIsTerminal(entry)
            ? entry.items.filter((item) => RETAINED_IMPORT_STATUSES.has(item.status))
            : entry.items,
        }))
        .filter((entry) => !batchIsTerminal(entry) || entry.items.length);
      if (!recovered.length) {
        setRetainedBatches((current) => current.filter((entry) => !loadedIds.has(entry.id)));
        return;
      }
      const latestActive = recovered.findLast((entry) => !batchIsTerminal(entry));
      const latest = latestActive || recovered.at(-1)!;
      const currentBatch = batchRef.current;
      const primary = currentBatch || latest;
      setBatch((current) => current || latest);
      setRetainedBatches((current) => {
        const merged = new Map(
          current
            .filter((entry) => !loadedIds.has(entry.id))
            .map((entry) => [entry.id, entry]),
        );
        for (const entry of recovered) {
          if (entry.id !== primary.id) merged.set(entry.id, entry);
        }
        return [...merged.values()];
      });
      const readyItems = recovered.flatMap((entry) =>
        entry.items.filter((item) => item.status === "ready"),
      );
      setSelectedItems((current) => new Set([
        ...current,
        ...readyItems.map((item) => item.id),
      ]));
      setItemCollections((current) => {
        const updated = new Map(current);
        for (const item of readyItems) {
          if (item.target_collection_id != null) {
            updated.set(item.id, item.target_collection_id);
          }
        }
        return updated;
      });
      setNotice("已恢复刷新前的导入批次；可继续查看进度、补件或确认入库");
    }
    recoverImports().catch(() => undefined);
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 5000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    if (tab === "library") loadLibrary(false).catch((value) => setError(reason(value)));
  }, [loadLibrary, tab]);

  useEffect(() => {
    if (restoredLibraryPosition.current || tab !== "library" || !worksPage.items.length) return;
    const context = readLibraryReturnContext();
    if (
      !context
      || context.state !== libraryState
      || context.collectionId !== collectionId
    ) return;
    restoredLibraryPosition.current = true;
    const frame = window.requestAnimationFrame(() => {
      window.scrollTo({ top: context.scrollY, behavior: "auto" });
      clearLibraryReturnContext();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [collectionId, libraryState, tab, worksPage.items.length]);

  useEffect(() => {
    if (!batch || batchIsTerminal(batch)) return;
    const timer = window.setTimeout(async () => {
      try {
        const next = await loadImportBatch(batch);
        setBatch(next);
        if (batchIsTerminal(next)) {
          const readyIds = next.items
            .filter((item) => item.status === "ready")
            .map((item) => item.id);
          const duplicateCount = Number(next.progress.duplicates || 0);
          const confirmableCount = next.items.filter((item) =>
            RETAINED_IMPORT_STATUSES.has(item.status),
          ).length;
          setSelectedItems((current) => new Set([...current, ...readyIds]));
          const submitted = submittedImportTexts.current.get(next.id);
          const fullySuccessful =
            (readyIds.length > 0 || duplicateCount > 0)
            && !next.items.some((item) =>
              ["failed", "blocked", "cancelled", "needs_local_file"].includes(item.status),
            );
          if (fullySuccessful && submitted) {
            setImportText((current) => current.trim() === submitted.trim() ? "" : current);
          }
          submittedImportTexts.current.delete(next.id);
          if (duplicateCount > 0) {
            setNotice(
              confirmableCount > 0
                ? `已去掉 ${duplicateCount} 条重复链接，${confirmableCount} 个不同作品预检完成`
                : `已去掉 ${duplicateCount} 条重复链接，没有新的作品需要预检`,
            );
          }
          await Promise.allSettled([loadUsage(), loadCollections()]);
        }
      } catch (value) {
        setError(reason(value, "读取解析进度失败"));
      }
    }, 1000);
    return () => window.clearTimeout(timer);
  }, [batch, loadCollections, loadUsage]);

  useEffect(() => {
    const activeIds = activeProcessingJobIds(jobs);
    if (!activeIds.size) return;
    const timer = window.setTimeout(async () => {
      try {
        const next = await api.jobs();
        setJobs(next);
        const reachedTerminal = didAnyJobReachTerminal(jobs, next);
        if (reachedTerminal) {
          await Promise.allSettled([
            loadUsage(),
            loadCollections(),
            ...(tab === "library" ? [loadLibrary(false)] : []),
          ]);
        }
      } catch {
        // The next polling cycle or a manual refresh will retry local status reads.
      }
    }, 2000);
    return () => window.clearTimeout(timer);
  }, [jobs, loadCollections, loadLibrary, loadUsage, tab]);

  function beginOperation(name: string) {
    if (activeOperationRef.current) return false;
    activeOperationRef.current = name;
    setBusy(name);
    setError("");
    return true;
  }

  function endOperation(name: string) {
    if (activeOperationRef.current !== name) return;
    activeOperationRef.current = "";
    setBusy("");
  }

  async function perform(name: string, operation: () => Promise<unknown>, success: string) {
    if (!beginOperation(name)) return false;
    try {
      const result = await operation();
      setNotice(success);
      if (operationIncludesJob(result)) {
        await loadJobs();
      } else if (name !== "health-check" && name !== "obsidian-image-folder") {
        await refreshSharedState();
      }
      return true;
    } catch (value) {
      if (isUserCancelled(value)) return false;
      setError(reason(value));
      return false;
    } finally {
      endOperation(name);
    }
  }

  async function submitImport(event: FormEvent) {
    event.preventDefault();
    if (!importText.trim() || !beginOperation("import")) return;
    const submittedText = importText;
    try {
      const created = await api.createImportBatch({ text: submittedText });
      rememberImportBatch(created.batch_id);
      const next = await api.importBatch(created.batch_id);
      const duplicateOnly =
        created.queued_count === 0
        && created.duplicate_count > 0
        && next.items.every(
          (item) => item.status === "duplicate" && item.error_code === "duplicate_input",
        );
      if (duplicateOnly) {
        setImportText((current) => current.trim() === submittedText.trim() ? "" : current);
        setNotice(
          `已去掉 ${created.duplicate_count} 条重复链接，没有新的作品需要预检`,
        );
        await refreshSharedState();
        return;
      }
      if (batch) {
        const retainedItems = batch.items.filter((item) =>
          RETAINED_IMPORT_STATUSES.has(item.status),
        );
        if (retainedItems.length) {
          setRetainedBatches((current) => [
            ...current.filter((item) => item.id !== batch.id),
            { ...batch, items: retainedItems },
          ]);
        }
      }
      submittedImportTexts.current.set(next.id, submittedText);
      setBatch(next);
      const messages: string[] = [];
      if (created.duplicate_count > 0) {
        messages.push(`已去掉 ${created.duplicate_count} 条重复链接`);
      }
      if (created.queued_count > 0) {
        messages.push(`${created.queued_count} 个不同作品已进入预检`);
      }
      if (created.rejected_count > 0) {
        messages.push(`超出批量上限的 ${created.rejected_count} 条未入队`);
      }
      setNotice(messages.join("；") || "链接预检已完成，请查看结果");
    } catch (value) {
      setError(reason(value));
    } finally {
      endOperation("import");
    }
  }

  async function cancelBatch() {
    if (!batch || !beginOperation("cancel-batch")) return;
    try {
      setBatch(await api.cancelImportBatch(batch.id));
      setNotice("正在安全停止；已成功解析的结果会继续保留");
    } catch (value) {
      setError(reason(value));
    } finally {
      endOperation("cancel-batch");
    }
  }

  async function upload(item: ImportItem, files: File[]) {
    if (!files.length) return;
    const owner = [batch, ...retainedBatches].find((candidate) =>
      candidate?.items.some((entry) => entry.id === item.id),
    );
    const operationName = `upload-${item.id}`;
    if (!owner || !beginOperation(operationName)) return;
    try {
      if (owner.source_type === "local_upload") {
        if (files.length !== 1) throw new Error("本地视频作品一次只能上传一个视频文件");
        await api.uploadLocalImportVideo(owner.id, item.id, files[0]);
      } else {
        await api.uploadImportAssets(item.id, files);
      }
      if (item.existing_work_id) {
        await api.retry(item.existing_work_id);
      }
      const next = await loadImportBatch(owner);
      if (batch?.id === owner.id) {
        setBatch(next);
      } else {
        setRetainedBatches((current) =>
          current.map((entry) => entry.id === owner.id ? next : entry),
        );
      }
      if (!item.existing_work_id) {
        setSelectedItems((current) => new Set(current).add(item.id));
      }
      setNotice(item.existing_work_id ? "本地补件已验证，已创建继续处理任务" : "本地补件已验证，可以确认入库");
    } catch (value) {
      setError(reason(value, "本地补件失败"));
    } finally {
      endOperation(operationName);
    }
  }

  async function acceptLocalBatch(next: ImportBatch) {
    const currentBatch = batchRef.current;
    if (currentBatch && currentBatch.id !== next.id) {
      const retainedItems = currentBatch.items.filter((item) =>
        RETAINED_IMPORT_STATUSES.has(item.status),
      );
      if (retainedItems.length) {
        setRetainedBatches((current) => [
          ...current.filter((entry) => entry.id !== currentBatch.id && entry.id !== next.id),
          { ...currentBatch, items: retainedItems },
        ]);
      }
    }
    batchRef.current = next;
    setBatch(next);
    const readyItems = next.items.filter((item) => item.status === "ready");
    setSelectedItems((current) => new Set([
      ...current,
      ...readyItems.map((item) => item.id),
    ]));
    setItemCollections((current) => {
      const updated = new Map(current);
      for (const item of readyItems) {
        if (item.target_collection_id != null) {
          updated.set(item.id, item.target_collection_id);
        }
      }
      return updated;
    });
    const failed = next.items.filter((item) => item.status === "failed").length;
    const sourceLabel = next.source_type === "package_upload" ? "数据包视频" : "本地视频";
    setNotice(
      failed > 0
        ? `${sourceLabel}已验证 ${readyItems.length} 个，${failed} 个失败；成功项已自动勾选`
        : `${readyItems.length} 个${sourceLabel}已验证并自动勾选，可以确认加入待入库`,
    );
    await refreshSharedState();
  }

  async function editPreviewItem(item: ImportItem, changes: ImportItemUpdate) {
    const owner = [batch, ...retainedBatches].find((candidate) =>
      candidate?.items.some((entry) => entry.id === item.id),
    );
    const operationName = `edit-preview-${item.id}`;
    if (!owner || !beginOperation(operationName)) return;
    try {
      await api.updateImportItem(item.id, changes);
      const next = await loadImportBatch(owner);
      if (batch?.id === owner.id) {
        setBatch(next);
      } else {
        setRetainedBatches((current) =>
          current.map((entry) => entry.id === owner.id ? next : entry),
        );
      }
      setNotice("本地视频信息已保存");
    } catch (value) {
      setError(reason(value, "保存本地视频信息失败"));
    } finally {
      endOperation(operationName);
    }
  }

  async function removePreviewItem(item: ImportItem) {
    if (!window.confirm("确认删除这条预检结果？已加入知识库的作品不会在这里被删除。")) return;
    const owner = [batch, ...retainedBatches].find((candidate) =>
      candidate?.items.some((entry) => entry.id === item.id),
    );
    const operationName = `remove-preview-${item.id}`;
    if (!owner || !beginOperation(operationName)) return;
    try {
      const next = await api.removeImportItem(item.id);
      if (batch?.id === owner.id) {
        setBatch(next.items.length ? next : null);
      } else {
        setRetainedBatches((current) =>
          current.flatMap((entry) => {
            if (entry.id !== owner.id) return [entry];
            const retained = {
              ...next,
              items: next.items.filter((candidate) =>
                RETAINED_IMPORT_STATUSES.has(candidate.status),
              ),
            };
            return retained.items.length ? [retained] : [];
          }),
        );
      }
      setSelectedItems((current) => {
        const updated = new Set(current);
        updated.delete(item.id);
        return updated;
      });
      setItemCollections((current) => {
        const updated = new Map(current);
        updated.delete(item.id);
        return updated;
      });
      setNotice("预检结果已删除");
    } catch (value) {
      setError(reason(value, "删除预检结果失败"));
    } finally {
      endOperation(operationName);
    }
  }

  async function confirmBatch(itemIds: number[]) {
    if (!itemIds.length || activeOperationRef.current) return;
    const requestedItems = new Set(itemIds);
    const candidates = [...retainedBatches, ...(batch ? [batch] : [])];
    const manualCollectionId = collections.find((group) => group.key === "manual-import")?.id;
    const uniqueItems: ImportItem[] = [];
    const groups = candidates
      .map((entry) => ({
        batch: entry,
        items: entry.items
          .filter((item) => {
            if (
              !requestedItems.has(item.id)
              || !RETAINED_IMPORT_STATUSES.has(item.status)
              || uniqueItems.some((candidate) => samePreviewWork(candidate, item))
            ) {
              return false;
            }
            uniqueItems.push(item);
            return true;
          })
          .map((item) => ({
            item_id: item.id,
            collection_id: itemCollections.get(item.id) ?? manualCollectionId,
          })),
      }))
      .filter((entry) => entry.items.length);
    if (!groups.length) return;
    const confirmedIds: number[] = [];
    const confirmedItemIds = new Set<number>();
    const failures: string[] = [];
    if (!beginOperation("confirm")) return;
    try {
      for (const group of groups) {
        try {
          const result = await api.confirmImportBatch(group.batch.id, group.items);
          confirmedIds.push(...result.work_ids);
          group.items.forEach((item) => confirmedItemIds.add(item.item_id));
        } catch (value) {
          failures.push(`${group.batch.id}: ${reason(value, "确认失败")}`);
        }
      }

      if (!confirmedItemIds.size) {
        setError(failures.join("；") || "确认入库失败");
        return;
      }

      const uniqueConfirmedIds = [...new Set(confirmedIds)];
      setNotice(
        failures.length
          ? `已确认 ${uniqueConfirmedIds.length} 个不同作品；${failures.length} 个批次失败，失败项仍保留`
          : `已将 ${uniqueConfirmedIds.length} 个不同作品加入待入库，可直接点击“入库”开始处理`,
      );
      setConfirmedWorkIds((current) => [
        ...new Set([...current, ...uniqueConfirmedIds]),
      ]);
      setSelectedItems((current) => {
        const next = new Set(current);
        confirmedItemIds.forEach((id) => next.delete(id));
        return next;
      });
      setItemCollections((current) => {
        const next = new Map(current);
        confirmedItemIds.forEach((id) => next.delete(id));
        return next;
      });

      const settled = await Promise.allSettled(candidates.map(loadImportBatch));
      const refreshed = candidates.map((entry, index) => {
        const result = settled[index];
        if (result.status === "fulfilled") return result.value;
        return removeConfirmedImportItems(entry, confirmedItemIds);
      });
      const current = batch
        ? refreshed.find((entry) => entry.id === batch.id) || null
        : null;
      setBatch(current);
      setRetainedBatches(
        refreshed
          .filter((entry) => entry.id !== batch?.id)
          .map((entry) => ({
            ...entry,
            items: entry.items.filter((item) =>
              RETAINED_IMPORT_STATUSES.has(item.status),
            ),
          }))
          .filter((entry) => entry.items.length),
      );
      if (failures.length) setError(failures.join("；"));
      await refreshSharedState();
    } finally {
      endOperation("confirm");
    }
  }

  async function ingestConfirmedWorks() {
    if (!confirmedWorkIds.length) return;
    const workIds = [...confirmedWorkIds];
    const accepted = await perform(
      "ingest-confirmed",
      () => api.ingest(workIds),
      `已创建 ${workIds.length} 个作品的入库任务`,
    );
    if (accepted) {
      setConfirmedWorkIds((current) =>
        current.filter((workId) => !workIds.includes(workId)),
      );
    }
  }

  async function ask(event: FormEvent) {
    event.preventDefault();
    const text = question.trim();
    if (!text) return;
    const history = messages
      .filter((message) => !message.status)
      .map((message) => ({ role: message.role, content: message.content }));
    if (!beginOperation("chat")) return;
    const user: ChatMessage = { id: crypto.randomUUID(), role: "user", content: text };
    const assistantId = crypto.randomUUID();
    setQuestion("");
    setMessages((current) => [
      ...current,
      user,
      { id: assistantId, role: "assistant", content: "正在查找相关作品…", status: "pending" },
    ]);
    let streamState = createChatStreamState();
    let streamRenderFrame = 0;
    const renderStreamState = () => {
      streamRenderFrame = 0;
      const snapshot = streamState;
      setMessages((current) =>
        current.map((message) =>
          message.id === assistantId
            ? {
                ...message,
                content: visibleChatStreamContent(snapshot),
                sources: snapshot.sources,
                status: snapshot.completed ? undefined : "pending",
              }
            : message,
        ),
      );
    };
    const scheduleStreamRender = (immediate = false) => {
      if (immediate && streamRenderFrame) {
        window.cancelAnimationFrame(streamRenderFrame);
        streamRenderFrame = 0;
      }
      if (immediate) {
        renderStreamState();
      } else if (!streamRenderFrame) {
        streamRenderFrame = window.requestAnimationFrame(renderStreamState);
      }
    };
    try {
      await api.askStream(text, history, chatMode, (streamEvent) => {
        streamState = applyChatStreamEvent(streamState, streamEvent);
        scheduleStreamRender(streamState.completed);
      });
      if (!streamState.completed) throw new Error("回答流意外中断，请重试");
      if (!streamState.content) throw new Error("回答为空，请重试");
    } catch (value) {
      if (streamRenderFrame) window.cancelAnimationFrame(streamRenderFrame);
      setMessages((current) =>
        current.map((message) =>
          message.id === assistantId
            ? { ...message, content: reason(value, "回答失败"), status: "error" }
            : message,
        ),
      );
    } finally {
      if (streamRenderFrame) window.cancelAnimationFrame(streamRenderFrame);
      endOperation("chat");
    }
  }

  const activeJobs = jobs
    .filter(isActiveProcessingJob)
    .sort((left, right) => {
      const priority = (job: Job) => job.state === "running" || job.state === "cancelling" ? 0 : 1;
      return priority(left) - priority(right)
        || new Date(left.created_at).getTime() - new Date(right.created_at).getTime();
    });
  const titles: Record<Tab, string> = {
    import: "导入视频",
    library: theme.copy.pages.library,
    chat: theme.copy.pages.chat,
    settings: theme.copy.pages.settings,
  };

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">{theme.mark}</span>
          <div><strong>TokBrain</strong><small>{theme.copy.brandTagline}</small></div>
        </div>
        <nav aria-label="主要功能">
          <button type="button" aria-current={tab === "import" ? "page" : undefined} className={tab === "import" ? "active" : ""} onClick={() => setTab("import")}>导入<i /></button>
          <button type="button" aria-current={tab === "library" ? "page" : undefined} className={tab === "library" ? "active" : ""} onClick={() => setTab("library")}>知识库<i /></button>
          <button type="button" aria-current={tab === "chat" ? "page" : undefined} className={tab === "chat" ? "active" : ""} onClick={() => setTab("chat")}>对话<i /></button>
          <button type="button" aria-current={tab === "settings" ? "page" : undefined} className={tab === "settings" ? "active" : ""} onClick={() => setTab("settings")}>设置<i /></button>
        </nav>
        <div className="theme-ambient" aria-hidden="true">
          <span className="ambient-symbol" />
          {Array.from({ length: 7 }, (_, index) => <i key={index} />)}
        </div>
        <div className="sidebar-foot">
          <span className={`signal ${health?.overall || "degraded"}`} />
          <div><strong>本地模式</strong><small>{health?.summary || "正在检查本地环境"}</small></div>
        </div>
      </aside>
      <main>
        <header className="topbar">
          <div><span className="eyebrow">LOCAL · USER INITIATED</span><h1>{titles[tab]}</h1></div>
          {tab === "import" && batch && (
            <span className={`pill ${batch.state}`}>{stateLabel(batch.state)}</span>
          )}
        </header>
        {notice && <div className="toast ok" role="status" aria-live="polite">{notice}<button type="button" aria-label="关闭成功提示" onClick={() => setNotice("")}>×</button></div>}
        {error && <div className="toast error" role="alert">{error}<button type="button" aria-label="关闭错误提示" onClick={() => setError("")}>×</button></div>}
        {!!activeJobs.length && (
          <div className="job-queue">
            {activeJobs.map((job, index) => (
              <JobProgress
                key={job.id}
                job={job}
                queuePosition={job.state === "queued" ? index + 1 : undefined}
                onCancel={() => perform(`cancel-job-${job.id}`, () => api.cancelJob(job.id), job.state === "queued" ? "排队任务已取消" : "正在安全停止任务")}
              />
            ))}
          </div>
        )}
        <ImportWorkspace
          hidden={tab !== "import"}
          text={importText}
          setText={setImportText}
          rightsAttested={rightsAttested}
          setRightsAttested={changeRightsAttestation}
          batch={batch}
          retainedBatches={retainedBatches}
          selected={selectedItems}
          setSelected={setSelectedItems}
          itemCollections={itemCollections}
          setItemCollections={setItemCollections}
          busy={busy}
          onSubmit={submitImport}
          onCancel={cancelBatch}
          onConfirm={confirmBatch}
          onIngestConfirmed={ingestConfirmedWorks}
          onUpload={upload}
          onLocalBatchReady={acceptLocalBatch}
          onEdit={editPreviewItem}
          onDelete={removePreviewItem}
          settings={settings}
          usage={usage}
          summary={summary}
          collections={collections}
          confirmedWorkIds={confirmedWorkIds}
        />
        {tab === "library" && (
          <Library
            key={`${libraryState}:${collectionId ?? "all"}`}
            state={libraryState}
            setState={(value) => {
              if (value === libraryState) return;
              libraryRequestRef.current += 1;
              setWorksPage(EMPTY_WORKS);
              setLibraryState(value);
            }}
            collectionId={collectionId}
            setCollectionId={(value) => {
              if (value === collectionId) return;
              libraryRequestRef.current += 1;
              setWorksPage(EMPTY_WORKS);
              setCollectionId(value);
            }}
            collections={collections}
            summary={summary}
            page={worksPage}
            reload={() => loadLibrary(false)}
            loadMore={() => loadLibrary(true)}
            loading={libraryLoading}
            perform={perform}
            globalSummaryPrompt={settings?.summary_prompt || ""}
            rightsAttested={rightsAttested}
            setRightsAttested={changeRightsAttestation}
          />
        )}
        {tab === "chat" && (
          <Chat
            question={question}
            setQuestion={setQuestion}
            messages={messages}
            busy={busy === "chat"}
            mode={chatMode}
            setMode={setChatMode}
            initialFormat={settings?.default_answer_format || "rich"}
            onSubmit={ask}
          />
        )}
        {tab === "settings" && (
          settings ? (
            <Settings
              settings={settings}
              usage={usage}
              health={health}
              onHealthChange={setHealth}
              busy={busy}
              perform={perform}
            />
          ) : (
            <div className="stack">
              <ThemePicker />
              <section className="card">
                <p className="muted">本机后端暂未连接；主题仍可在当前设备上切换。</p>
              </section>
            </div>
          )
        )}
      </main>
    </div>
  );
}

function JobProgress({ job, queuePosition, onCancel }: { job: Job; queuePosition?: number; onCancel: () => void }) {
  const completed = Number(job.progress.items_completed || job.progress.completed || 0);
  const total = job.total_items || Number(job.progress.items_total || 0);
  const percent = total ? Math.min(100, (completed / total) * 100) : 0;
  return (
    <section className="job-progress">
      <div className="progress-copy">
        <span className="kicker">{jobLabel(job)}{queuePosition ? ` · 队列第 ${queuePosition} 位` : ""}</span>
        <strong>{job.message}</strong>
        <small>{completed}/{total || "?"}</small>
      </div>
      <div className="progress-track" role="progressbar" aria-label={job.message} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(percent)}><i style={{ width: `${percent}%` }} /></div>
      <span className={`pill ${job.state}`}>{stateLabel(job.state)}</span>
      <button className="danger" disabled={job.state === "cancelling"} onClick={onCancel}>安全停止</button>
    </section>
  );
}

const LOCAL_VIDEO_LIMIT = 10;
const LOCAL_VIDEO_MAX_BYTES = 1024 * 1024 * 1024;
const LOCAL_VIDEO_EXTENSIONS = new Set(["mp4", "mov", "mkv", "webm"]);

function localVideoTitle(filename: string) {
  const title = filename.replace(/\.[^.]+$/, "").trim();
  return title || filename;
}

function readableBytes(bytes: number) {
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function localFileError(file: File) {
  const extension = file.name.split(".").pop()?.toLowerCase() || "";
  if (!LOCAL_VIDEO_EXTENSIONS.has(extension)) return "仅支持 MP4、MOV、MKV、WebM 视频";
  if (file.size <= 0) return "文件为空";
  if (file.size > LOCAL_VIDEO_MAX_BYTES) return "文件超过 1 GB 上限";
  return "";
}

function LocalVideoImporter({
  collections,
  disabled,
  rightsAttested,
  setRightsAttested,
  onBatchReady,
  onUploadingChange,
}: {
  collections: Collection[];
  disabled: boolean;
  rightsAttested: boolean;
  setRightsAttested: (value: boolean) => void;
  onBatchReady: (batch: ImportBatch) => Promise<void>;
  onUploadingChange: (value: boolean) => void;
}) {
  const suggestedCollectionId = collections.find((group) => group.key === "manual-import")?.id
    ?? collections[0]?.id
    ?? null;
  const [defaultCollectionId, setDefaultCollectionId] = useState<number | null>(suggestedCollectionId);
  const [drafts, setDrafts] = useState<LocalVideoDraft[]>([]);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [localError, setLocalError] = useState("");
  const effectiveDefaultCollectionId = defaultCollectionId ?? suggestedCollectionId;

  function appendFiles(files: File[]) {
    setLocalError("");
    if (!files.length) return;
    const remaining = Math.max(0, LOCAL_VIDEO_LIMIT - drafts.length);
    if (remaining === 0) {
      setLocalError(`每批最多选择 ${LOCAL_VIDEO_LIMIT} 个视频`);
      return;
    }
    if (files.length > remaining) {
      setLocalError(`每批最多选择 ${LOCAL_VIDEO_LIMIT} 个视频，本次仅加入前 ${remaining} 个`);
    }
    const next = files.slice(0, remaining).map((file): LocalVideoDraft => {
      const validationError = localFileError(file);
      return {
        clientItemId: crypto.randomUUID(),
        file,
        title: localVideoTitle(file.name),
        targetCollectionId: effectiveDefaultCollectionId,
        status: validationError ? "failed" : "pending",
        message: validationError || "等待上传",
      };
    });
    setDrafts((current) => [...current, ...next]);
  }

  function onDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setDragging(false);
    if (!disabled && !uploading) appendFiles(Array.from(event.dataTransfer.files));
  }

  function updateDraft(clientItemId: string, change: Partial<LocalVideoDraft>) {
    setDrafts((current) => current.map((draft) =>
      draft.clientItemId === clientItemId ? { ...draft, ...change } : draft,
    ));
  }

  async function uploadVideos() {
    const candidates = drafts.filter((draft) =>
      draft.status !== "ready"
      && draft.status !== "duplicate"
      && !localFileError(draft.file),
    );
    if (!rightsAttested || !candidates.length || disabled || uploading) return;
    setUploading(true);
    onUploadingChange(true);
    setLocalError("");
    setDrafts((current) => current.map((draft) =>
      candidates.some((candidate) => candidate.clientItemId === draft.clientItemId)
        ? { ...draft, status: "creating", message: "正在创建导入条目" }
        : draft,
    ));
    try {
      const created = await api.createLocalImportBatch({
        rights_attested: true,
        items: candidates.map((draft) => ({
          client_item_id: draft.clientItemId,
          filename: draft.file.name,
          size_bytes: draft.file.size,
          ...(draft.targetCollectionId != null
            ? { target_collection_id: draft.targetCollectionId }
            : {}),
        })),
      });
      rememberLocalImportBatch(created.id);
      const matchedItems = new Map<string, ImportItem>();
      candidates.forEach((draft, index) => {
        const item = created.items.find((entry) => entry.client_item_id === draft.clientItemId)
          ?? created.items.find((entry) => entry.ordinal === index + 1);
        if (item) matchedItems.set(draft.clientItemId, item);
      });
      setDrafts((current) => current.map((draft) => {
        const item = matchedItems.get(draft.clientItemId);
        if (!item) {
          return candidates.some((candidate) => candidate.clientItemId === draft.clientItemId)
            ? { ...draft, status: "failed", message: "后台未返回对应导入条目" }
            : draft;
        }
        return {
          ...draft,
          batchId: created.id,
          serverItemId: item.id,
          status: "uploading",
          message: "正在验证并上传",
        };
      }));

      let cursor = 0;
      async function worker() {
        while (cursor < candidates.length) {
          const draft = candidates[cursor++];
          const item = matchedItems.get(draft.clientItemId);
          if (!item) continue;
          try {
            await api.updateImportItem(item.id, {
              title: draft.title.trim() || localVideoTitle(draft.file.name),
              target_collection_id: draft.targetCollectionId,
            });
            const result = await api.uploadLocalImportVideo(created.id, item.id, draft.file);
            const duplicate = result.status === "duplicate" || result.existing_work_id != null;
            updateDraft(draft.clientItemId, {
              status: duplicate ? "duplicate" : "ready",
              message: duplicate ? "知识库中已有相同视频" : "上传与视频校验完成",
            });
          } catch (value) {
            updateDraft(draft.clientItemId, {
              status: "failed",
              message: reason(value, "上传失败"),
            });
          }
        }
      }
      await Promise.all([worker(), worker()]);
      const refreshed = await api.importBatch(created.id);
      setDrafts((current) => current.map((draft) => {
        const item = refreshed.items.find((entry) => entry.client_item_id === draft.clientItemId);
        if (!item || draft.status === "failed") return draft;
        if (item.status === "duplicate") {
          return { ...draft, status: "duplicate", message: "知识库中已有相同视频" };
        }
        if (item.status === "ready") {
          return { ...draft, status: "ready", message: "上传与视频校验完成" };
        }
        return draft;
      }));
      await onBatchReady(refreshed);
    } catch (value) {
      const message = reason(value, "创建本地视频批次失败");
      setLocalError(message);
      setDrafts((current) => current.map((draft) =>
        draft.status === "creating" ? { ...draft, status: "failed", message } : draft,
      ));
    } finally {
      setUploading(false);
      onUploadingChange(false);
    }
  }

  const uploadableCount = drafts.filter((draft) =>
    draft.status !== "ready"
    && draft.status !== "duplicate"
    && !localFileError(draft.file),
  ).length;
  const localStatusLabel: Record<LocalUploadState, string> = {
    pending: "待上传",
    creating: "准备中",
    uploading: "上传中",
    ready: "可确认",
    duplicate: "已存在",
    failed: "失败",
  };

  return (
    <section className="card local-import-card">
      <div className="section-head">
        <div><span className="kicker">本地视频</span><h2>导入已经下载好的视频</h2></div>
        <small>每批最多 10 个 · 同时上传 2 个 · 不访问抖音</small>
      </div>
      <div className="local-import-controls">
        <label
          className={`local-dropzone ${dragging ? "dragging" : ""} ${disabled ? "disabled" : ""}`}
          onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
        >
          <strong>选择或拖入本地视频</strong>
          <span>支持 MP4、MOV、MKV、WebM，单文件不超过 1 GB</span>
          <input
            type="file"
            multiple
            accept=".mp4,.mov,.mkv,.webm,video/mp4,video/quicktime,video/webm,video/x-matroska"
            disabled={disabled || uploading || drafts.length >= LOCAL_VIDEO_LIMIT}
            onChange={(event) => {
              appendFiles(Array.from(event.target.files || []));
              event.target.value = "";
            }}
          />
        </label>
        <label className="local-default-collection">
          <span>本批默认收藏夹</span>
          <select
            value={effectiveDefaultCollectionId ?? ""}
            disabled={uploading || !collections.length}
            onChange={(event) => {
              const nextId = event.target.value ? Number(event.target.value) : null;
              setDefaultCollectionId(nextId);
              setDrafts((current) => current.map((draft) =>
                ["pending", "failed"].includes(draft.status)
                  ? { ...draft, targetCollectionId: nextId }
                  : draft,
              ));
            }}
          >
            {!collections.length && <option value="">暂无收藏夹</option>}
            {collections.map((group) => (
              <option key={group.id} value={group.id}>{group.title}</option>
            ))}
          </select>
        </label>
      </div>
      {!!drafts.length && (
        <div className="local-file-list">
          {drafts.map((draft, index) => (
            <article className={`local-file status-${draft.status}`} key={draft.clientItemId}>
              <span className="local-file-order">{index + 1}</span>
              <div className="local-file-fields">
                <label>
                  <span>标题</span>
                  <input
                    value={draft.title}
                    disabled={uploading || draft.status === "ready" || draft.status === "duplicate"}
                    onChange={(event) => updateDraft(draft.clientItemId, { title: event.target.value })}
                  />
                </label>
                <label>
                  <span>收藏夹</span>
                  <select
                    value={draft.targetCollectionId ?? ""}
                    disabled={uploading || draft.status === "ready" || draft.status === "duplicate" || !collections.length}
                    onChange={(event) => updateDraft(draft.clientItemId, {
                      targetCollectionId: event.target.value ? Number(event.target.value) : null,
                    })}
                  >
                    {!collections.length && <option value="">暂无收藏夹</option>}
                    {collections.map((group) => <option key={group.id} value={group.id}>{group.title}</option>)}
                  </select>
                </label>
              </div>
              <div className="local-file-state">
                <span className={`pill ${draft.status}`}>{localStatusLabel[draft.status]}</span>
                <small>{draft.file.name} · {readableBytes(draft.file.size)}</small>
                <small>{draft.message}</small>
              </div>
              {!uploading && (
                <button
                  type="button"
                  className="link"
                  aria-label={`移除 ${draft.file.name}`}
                  onClick={() => setDrafts((current) => current.filter((item) => item.clientItemId !== draft.clientItemId))}
                >
                  移除
                </button>
              )}
            </article>
          ))}
        </div>
      )}
      {localError && <p className="local-import-error">{localError}</p>}
      <div className="local-import-foot">
        <label className="rights-attestation">
          <input
            type="checkbox"
            checked={rightsAttested}
            disabled={uploading}
            onChange={(event) => setRightsAttested(event.target.checked)}
          />
          <span>我确认有权处理这些视频文件，并授权 TokBrain 在本机进行解析与 AI 入库处理。</span>
        </label>
        <button
          type="button"
          className="primary"
          disabled={!rightsAttested || !uploadableCount || disabled || uploading}
          onClick={uploadVideos}
        >
          {uploading ? "正在上传与验证…" : `上传并预检（${uploadableCount}）`}
        </button>
      </div>
      {disabled && <p className="muted local-import-disabled">当前链接预检完成后即可导入本地视频。</p>}
    </section>
  );
}

const PACKAGE_SUPPORTED_EXTENSIONS = new Set(["mp4", "mov", "mkv", "webm", "json", "csv", "db", "sqlite", "sqlite3"]);

function PackageVideoImporter({
  collections,
  disabled,
  rightsAttested,
  setRightsAttested,
  onBatchReady,
  onUploadingChange,
}: {
  collections: Collection[];
  disabled: boolean;
  rightsAttested: boolean;
  setRightsAttested: (value: boolean) => void;
  onBatchReady: (batch: ImportBatch) => Promise<void>;
  onUploadingChange: (value: boolean) => void;
}) {
  const suggestedCollectionId = collections.find((group) => group.key === "manual-import")?.id
    ?? collections[0]?.id
    ?? null;
  const [mode, setMode] = useState<"folder" | "zip">("folder");
  const [targetCollectionId, setTargetCollectionId] = useState<number | null>(suggestedCollectionId);
  const [drafts, setDrafts] = useState<PackageFileDraft[]>([]);
  const [activeBatch, setActiveBatch] = useState<ImportBatch | null>(null);
  const [uploading, setUploading] = useState(false);
  const [packageError, setPackageError] = useState("");
  const onBatchReadyRef = useRef(onBatchReady);
  const effectiveCollectionId = targetCollectionId ?? suggestedCollectionId;
  const analyzing = Boolean(activeBatch && ["queued", "running"].includes(activeBatch.state));

  useEffect(() => {
    onBatchReadyRef.current = onBatchReady;
  }, [onBatchReady]);

  useEffect(() => {
    let cancelled = false;
    const ids = storedPackageBatches();
    const latest = ids.at(-1);
    if (!latest) return;
    api.packageImportBatch(latest).then((value) => {
      if (cancelled) return;
      setActiveBatch(value);
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!activeBatch || !["queued", "running"].includes(activeBatch.state)) return;
    const timer = window.setTimeout(async () => {
      try {
        const next = await api.packageImportBatch(activeBatch.id);
        setActiveBatch(next);
        if (TERMINAL_BATCH_STATES.has(next.state) && next.items.length) {
          await onBatchReadyRef.current(next);
        }
      } catch (value) {
        setPackageError(reason(value, "读取数据包检测进度失败"));
      }
    }, 1000);
    return () => window.clearTimeout(timer);
  }, [activeBatch]);

  function selectFiles(nextMode: "folder" | "zip", selected: File[]) {
    setPackageError("");
    setDrafts([]);
    setMode(nextMode);
    if (nextMode === "zip") {
      const file = selected[0];
      if (!file || !file.name.toLowerCase().endsWith(".zip")) {
        setPackageError("请选择一个 ZIP 数据包");
        setDrafts([]);
        return;
      }
      if (file.size > 20 * 1024 * 1024 * 1024) {
        setPackageError("ZIP 数据包不能超过 20 GB");
        return;
      }
      setDrafts([{ clientFileId: crypto.randomUUID(), file, relativePath: file.name, status: "pending" }]);
      return;
    }
    const supported = selected.filter((file) => PACKAGE_SUPPORTED_EXTENSIONS.has(file.name.split(".").pop()?.toLowerCase() || ""));
    const videoCount = supported.filter((file) => LOCAL_VIDEO_EXTENSIONS.has(file.name.split(".").pop()?.toLowerCase() || "")).length;
    if (!videoCount) {
      setPackageError("所选文件夹中没有支持的视频（MP4/MOV/MKV/WebM）");
      setDrafts([]);
      return;
    }
    if (videoCount > 100) {
      setPackageError("每批最多导入 100 个视频");
      return;
    }
    if (supported.length > 1000) {
      setPackageError("可识别文件超过 1000 个，请拆分后导入");
      return;
    }
    const total = supported.reduce((sum, file) => sum + file.size, 0);
    if (total > 20 * 1024 * 1024 * 1024) {
      setPackageError("本批文件总大小超过 20 GB");
      return;
    }
    const oversized = supported.find((file) => LOCAL_VIDEO_EXTENSIONS.has(file.name.split(".").pop()?.toLowerCase() || "") && file.size > LOCAL_VIDEO_MAX_BYTES);
    if (oversized) {
      setPackageError(`${oversized.name} 超过单视频 1 GB 上限`);
      return;
    }
    setDrafts(supported.map((file) => ({
      clientFileId: crypto.randomUUID(),
      file,
      relativePath: file.webkitRelativePath || file.name,
      status: "pending",
    })));
  }

  function updateDraft(clientFileId: string, change: Partial<PackageFileDraft>) {
    setDrafts((current) => current.map((draft) => draft.clientFileId === clientFileId ? { ...draft, ...change } : draft));
  }

  async function uploadPackage() {
    if (!rightsAttested || !drafts.length || uploading || disabled) return;
    setUploading(true);
    onUploadingChange(true);
    setPackageError("");
    try {
      const canResume = activeBatch?.state === "uploading"
        && activeBatch.package?.upload_mode === mode
        && activeBatch.package_files?.length === drafts.length
        && drafts.every((draft) => activeBatch.package_files?.some((entry) => entry.relative_path === draft.relativePath && entry.declared_size === draft.file.size));
      const created = canResume ? activeBatch! : await api.createPackageImportBatch({
        rights_attested: true,
        upload_mode: mode,
        ...(effectiveCollectionId != null ? { target_collection_id: effectiveCollectionId } : {}),
        files: drafts.map((draft) => ({
          client_file_id: draft.clientFileId,
          relative_path: draft.relativePath,
          size_bytes: draft.file.size,
        })),
      });
      rememberPackageBatch(created.id);
      setActiveBatch(created);
      const serverFiles = new Map((created.package_files || []).map((entry) => [entry.relative_path, entry]));
      let cursor = 0;
      let failures = 0;
      async function worker() {
        while (cursor < drafts.length) {
          const draft = drafts[cursor++];
          const entry = serverFiles.get(draft.relativePath);
          if (!entry) {
            failures += 1;
            updateDraft(draft.clientFileId, { status: "failed" });
            continue;
          }
          if (entry.status === "uploaded" || entry.status === "analyzed") {
            updateDraft(draft.clientFileId, { status: "uploaded" });
            continue;
          }
          updateDraft(draft.clientFileId, { status: "uploading" });
          try {
            await api.uploadPackageImportFile(created.id, entry.id, draft.file);
            updateDraft(draft.clientFileId, { status: "uploaded" });
          } catch {
            failures += 1;
            updateDraft(draft.clientFileId, { status: "failed" });
          }
        }
      }
      await Promise.all([worker(), worker()]);
      if (failures) {
        setActiveBatch(await api.packageImportBatch(created.id));
        setPackageError(`${failures} 个文件上传失败；保留当前页面后可再次点击续传`);
        return;
      }
      const queued = await api.analyzePackageImportBatch(created.id);
      setActiveBatch(queued);
    } catch (value) {
      setPackageError(reason(value, "创建数据包导入失败"));
    } finally {
      setUploading(false);
      onUploadingChange(false);
    }
  }

  const uploaded = drafts.filter((draft) => draft.status === "uploaded").length;
  return (
    <section className="card package-import-card">
      <div className="section-head">
        <div><span className="kicker">外部工具数据</span><h2>导入外部视频数据包</h2></div>
        <small>支持文件夹或 ZIP · 最多 100 个视频 · 不访问抖音</small>
      </div>
      <p className="muted">可直接选择 F2 下载目录或 ZIP。后端会识别 `douyin_videos.db`、JSON、CSV 和文件名；识别不到元数据的视频会按本地视频导入。</p>
      <div className="package-pickers">
        <label className={mode === "folder" ? "selected" : ""}>
          <strong>选择文件夹</strong><span>视频与元数据一起选择</span>
          <input
            type="file"
            multiple
            disabled={disabled || uploading || analyzing}
            {...({ webkitdirectory: "", directory: "" } as Record<string, string>)}
            onChange={(event) => {
              selectFiles("folder", Array.from(event.target.files || []));
              event.target.value = "";
            }}
          />
        </label>
        <label className={mode === "zip" ? "selected" : ""}>
          <strong>选择 ZIP</strong><span>自动安全解压与识别</span>
          <input
            type="file"
            accept=".zip,application/zip"
            disabled={disabled || uploading || analyzing}
            onChange={(event) => {
              selectFiles("zip", Array.from(event.target.files || []));
              event.target.value = "";
            }}
          />
        </label>
        <label className="local-default-collection">
          <span>默认收藏夹</span>
          <select value={effectiveCollectionId ?? ""} disabled={uploading || !collections.length} onChange={(event) => setTargetCollectionId(event.target.value ? Number(event.target.value) : null)}>
            {!collections.length && <option value="">暂无收藏夹</option>}
            {collections.map((group) => <option key={group.id} value={group.id}>{group.title}</option>)}
          </select>
        </label>
      </div>
      {!!drafts.length && (
        <div className="package-summary">
          <strong>{mode === "zip" ? drafts[0].file.name : `已识别 ${drafts.filter((draft) => LOCAL_VIDEO_EXTENSIONS.has(draft.file.name.split(".").pop()?.toLowerCase() || "")).length} 个视频`}</strong>
          <span>{drafts.length} 个待上传文件 · {readableBytes(drafts.reduce((sum, draft) => sum + draft.file.size, 0))}</span>
          {uploading && <span>已上传 {uploaded}/{drafts.length}</span>}
        </div>
      )}
      {activeBatch && ["queued", "running"].includes(activeBatch.state) && (
        <div className="package-analysis-status"><span className="pill running">检测中</span><strong>{activeBatch.package?.analysis_state || activeBatch.state}</strong><small>切换页面或关闭浏览器不会中断后端检测</small></div>
      )}
      {activeBatch && ["succeeded", "partial"].includes(activeBatch.state) && activeBatch.items.length > 0 && (
        <div className="package-analysis-status"><span className={`pill ${activeBatch.state}`}>检测完成</span><strong>{activeBatch.items.filter((item) => item.status === "ready").length} 个视频可确认</strong><small>请在下方逐条核对并确认加入待入库</small></div>
      )}
      {activeBatch?.state === "failed" && (
        <p className="local-import-error">{activeBatch.error_message || "数据包检测失败，请检查文件后重新选择"}</p>
      )}
      {activeBatch?.state === "uploading" && !drafts.length && (
        <p className="muted">已恢复一个未完成上传的数据包。请重新选择同一个文件夹或 ZIP 后点击“续传并检测”。</p>
      )}
      {packageError && <p className="local-import-error">{packageError}</p>}
      <div className="local-import-foot">
        <label className="rights-attestation">
          <input type="checkbox" checked={rightsAttested} disabled={uploading} onChange={(event) => setRightsAttested(event.target.checked)} />
          <span>我确认有权处理这些视频文件，并授权 TokBrain 在本机检测、解析与后续 AI 入库。</span>
        </label>
        <button type="button" className="primary" disabled={!rightsAttested || !drafts.length || uploading || analyzing || disabled} onClick={uploadPackage}>
          {uploading ? `上传中 ${uploaded}/${drafts.length}` : activeBatch?.state === "uploading" ? "续传并检测" : "上传并自动检测"}
        </button>
      </div>
    </section>
  );
}

function ImportWorkspace({
  hidden,
  text,
  setText,
  rightsAttested,
  setRightsAttested,
  batch,
  retainedBatches,
  selected,
  setSelected,
  itemCollections,
  setItemCollections,
  busy,
  onSubmit,
  onCancel,
  onConfirm,
  onIngestConfirmed,
  onUpload,
  onLocalBatchReady,
  onEdit,
  onDelete,
  settings,
  usage,
  summary,
  collections,
  confirmedWorkIds,
}: {
  hidden: boolean;
  text: string;
  setText: (value: string) => void;
  rightsAttested: boolean;
  setRightsAttested: (value: boolean) => void;
  batch: ImportBatch | null;
  retainedBatches: ImportBatch[];
  selected: Set<number>;
  setSelected: (value: Set<number>) => void;
  itemCollections: Map<number, number>;
  setItemCollections: (value: Map<number, number>) => void;
  busy: string;
  onSubmit: (event: FormEvent) => void;
  onCancel: () => void;
  onConfirm: (itemIds: number[]) => void;
  onIngestConfirmed: () => void;
  onUpload: (item: ImportItem, files: File[]) => void;
  onLocalBatchReady: (batch: ImportBatch) => Promise<void>;
  onEdit: (item: ImportItem, changes: ImportItemUpdate) => void;
  onDelete: (item: ImportItem) => void;
  settings: RuntimeSettings | null;
  usage: Usage | null;
  summary: LibrarySummary;
  collections: Collection[];
  confirmedWorkIds: number[];
}) {
  const [importerBusy, setImporterBusy] = useState(false);
  const importerBusyRef = useRef(false);
  const effectiveBusy = busy || (importerBusy ? "local-importer" : "");
  function changeImporterBusy(value: boolean) {
    importerBusyRef.current = value;
    setImporterBusy(value);
  }
  const active = batch && !batchIsTerminal(batch);
  const completed = Number(batch?.progress.completed || 0);
  const percent = batch?.total_items ? Math.round((completed / batch.total_items) * 100) : 0;
  const dailyIngestRemaining = Math.max(
    0,
    (settings?.import_daily_limit || 150) - (usage?.daily_works_used || 0),
  );
  const rawDisplayItems = [
    ...retainedBatches.flatMap((entry) => entry.items),
    ...(batch?.items || []),
  ].filter(
    (item) =>
      !(item.status === "duplicate" && item.error_code === "duplicate_input"),
  );
  const displayItems = rawDisplayItems.filter(
    (item, index, items) =>
      items.findIndex((candidate) => samePreviewWork(candidate, item)) === index,
  );
  const selectable = displayItems.filter((item) =>
    item.status === "ready",
  );
  const selectedVisibleIds = selectable
    .filter((item) => selected.has(item.id))
    .map((item) => item.id);
  const manualCollectionId = collections.find((group) => group.key === "manual-import")?.id;
  function toggle(id: number, checked: boolean) {
    const next = new Set(selected);
    if (checked) next.add(id); else next.delete(id);
    setSelected(next);
  }
  return (
    <section className="import-workspace stack" hidden={hidden}>
      <div className="risk-notice" role="alert">
        <strong>导入风险提示</strong>
        <p>视频作品仅当平台明确返回作者允许下载时才处理完整视频；禁止下载或状态未知时，不下载视频、不抽帧。对您主动提交的公开图文作品，无论下载权限状态如何都会尝试取得并处理全部公开图片，但不会绕过登录、验证码或平台风控。本地素材只会在您确认有权处理后上传到本机服务。</p>
      </div>
      <section className="import-overview" aria-label="今日处理与知识库概况">
        <span><small>今日处理</small><strong>{usage?.daily_works_used || 0}<em> 件</em></strong></span>
        <span><small>今日 AI 用量</small><strong>{(usage?.daily_llm_tokens_used || 0).toLocaleString()}<em>/ {(usage?.daily_llm_tokens_limit || 0).toLocaleString()}</em></strong></span>
        <span><small>已入库</small><strong>{summary.local_item_count.toLocaleString()}</strong></span>
        <span><small>待入库</small><strong>{summary.candidate_count.toLocaleString()}</strong></span>
      </section>
      <LocalVideoImporter
        collections={collections}
        disabled={Boolean(active) || Boolean(effectiveBusy)}
        rightsAttested={rightsAttested}
        setRightsAttested={setRightsAttested}
        onBatchReady={onLocalBatchReady}
        onUploadingChange={changeImporterBusy}
      />
      <PackageVideoImporter
        collections={collections}
        disabled={Boolean(active) || Boolean(effectiveBusy)}
        rightsAttested={rightsAttested}
        setRightsAttested={setRightsAttested}
        onBatchReady={onLocalBatchReady}
        onUploadingChange={changeImporterBusy}
      />
      <form className="card import-form" onSubmit={(event) => {
        if (importerBusyRef.current) {
          event.preventDefault();
          return;
        }
        onSubmit(event);
      }}>
        <div className="section-head">
          <div><span className="kicker">公开作品链接</span><h2>添加抖音作品链接</h2></div>
          <small>每批最多 {settings?.import_batch_limit || 10} 条 · 预检后由您确认</small>
        </div>
        <textarea
          className="import-textarea"
          aria-label="抖音作品链接，每行一个"
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder={"每行粘贴一个抖音作品链接，例如：\nhttps://v.douyin.com/...\nhttps://www.douyin.com/video/..."}
        />
        <div className="import-form-foot">
          <span>{text.length.toLocaleString()} 字符 · 只进行预检，不会自动入库</span>
          <button
            className="primary"
            disabled={
              !text.trim()
              || Boolean(effectiveBusy)
              || Boolean(active)
            }
          >
            {busy === "import" ? "正在创建队列…" : "开始预检"}
          </button>
        </div>
      </form>
      {(batch || retainedBatches.length > 0 || confirmedWorkIds.length > 0) && (
        <section className="card batch-panel">
          {batch && <>
            <div className="section-head">
              <div><span className="kicker">解析进度</span><h2>{completed}/{batch.total_items} · {percent}%</h2></div>
              <div className="button-row">
                {active && <button className="danger" disabled={Boolean(effectiveBusy)} onClick={onCancel}>{batch.state === "cancelling" ? "正在安全停止" : "中断解析"}</button>}
                <span className={`pill ${batch.state}`}>{stateLabel(batch.state)}</span>
              </div>
            </div>
            <div className="batch-progress" role="progressbar" aria-label="导入预检进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(percent)}><i style={{ width: `${percent}%` }} /></div>
            <div className="batch-stats">
              <span>可确认 <strong>{batch.progress.ready || 0}</strong></span>
              <span>需补件 <strong>{batch.progress.needs_local_file || 0}</strong></span>
              <span>重复 <strong>{batch.progress.duplicates || 0}</strong></span>
              <span>失败 <strong>{batch.progress.failed || 0}</strong></span>
              <span>中断 <strong>{batch.progress.cancelled || 0}</strong></span>
              <span>今日入库剩余 <strong>{dailyIngestRemaining}</strong></span>
            </div>
          </>}
          {retainedBatches.length > 0 && (
            <p className="retained-results-note">
              已保留之前预检中的 {retainedBatches.reduce((total, entry) => total + entry.items.length, 0)} 个待确认作品
            </p>
          )}
          {!!batch?.workers.length && (
            <div className="worker-grid">
              {batch.workers.map((worker) => <div key={worker.worker_id}><span>Worker {worker.worker_id}</span><strong>{worker.title}</strong></div>)}
            </div>
          )}
          {batch?.circuit.active && <div className="circuit-warning"><strong>链接解析已暂停</strong><span>{batch.circuit.message} · {batch.circuit.expires_at ? new Date(batch.circuit.expires_at).toLocaleTimeString("zh-CN", { hour12: false }) : ""} 后再试</span></div>}
          <div className="import-results">
            {displayItems.map((item) => (
              <article className={`import-result status-${item.status}`} key={item.id}>
                <div className="result-select">
                  {item.status === "ready" ? <input type="checkbox" disabled={Boolean(effectiveBusy)} checked={selected.has(item.id)} onChange={(event) => toggle(item.id, event.target.checked)} aria-label={`选择 ${item.title || item.input_url}`} /> : <span>{item.ordinal}</span>}
                </div>
                <div className="result-cover">{localAssetUrl(item.cover_url) ? <img src={localAssetUrl(item.cover_url)!} alt="" /> : <span>{item.platform === "local" ? "本地" : item.kind === "image" ? "图文" : "链接"}</span>}</div>
                <div className="result-body">
                  <div><span className={`pill ${item.status}`}>{itemStatus(item)}</span>{item.error_code && <code>{visibleErrorCode(item.error_code)}</code>}</div>
                  {item.platform === "local" && item.status === "ready" ? (
                    <input
                      className="result-title-input"
                      defaultValue={item.title || ""}
                      aria-label="本地视频标题"
                      disabled={Boolean(effectiveBusy)}
                      onBlur={(event) => {
                        const title = event.target.value.trim();
                        if (title && title !== item.title) onEdit(item, { title });
                      }}
                    />
                  ) : (
                    <h3>{item.title || item.input_url}</h3>
                  )}
                  <p>{item.author_name || "作者待识别"}{item.duration_seconds > 0 ? ` · ${Math.round(item.duration_seconds)} 秒` : ""}</p>
                  <p className="muted">{mediaPolicyLabel(item)}</p>
                  {item.status === "ready" && (
                    <label className="result-collection">
                      <span>加入收藏夹</span>
                      <select
                        value={itemCollections.get(item.id) ?? manualCollectionId ?? ""}
                        disabled={Boolean(effectiveBusy)}
                        onChange={(event) => {
                          const next = new Map(itemCollections);
                          const targetCollectionId = event.target.value ? Number(event.target.value) : null;
                          if (targetCollectionId != null) next.set(item.id, targetCollectionId);
                          else next.delete(item.id);
                          setItemCollections(next);
                          if (item.platform === "local") {
                            onEdit(item, { target_collection_id: targetCollectionId });
                          }
                        }}
                      >
                        {collections.map((group) => (
                          <option key={group.id} value={group.id}>
                            {group.title}{group.summary_prompt ? " · 专属提示词" : ""}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  {item.error_message && <p className="result-error">{item.error_message}</p>}
                  <div className="work-actions">
                    {item.existing_work_id && <a href={`/works/${item.existing_work_id}`}>查看现有作品</a>}
                    {["needs_local_file", "failed"].includes(item.status) && (
                      <label className="upload-action">
                        {busy === `upload-${item.id}` ? "正在验证…" : "上传本地补件"}
                        <input
                          type="file"
                          multiple={item.platform !== "local"}
                          accept={item.platform === "local"
                            ? ".mp4,.mov,.mkv,.webm,video/mp4,video/quicktime,video/webm,video/x-matroska"
                            : ".mp4,.mov,.mkv,.webm,.jpg,.jpeg,.png,.webp,video/mp4,video/quicktime,video/webm,image/jpeg,image/png,image/webp"}
                          disabled={Boolean(effectiveBusy)}
                          onChange={(event) => {
                            const files = Array.from(event.currentTarget.files || []);
                            event.currentTarget.value = "";
                            onUpload(item, files);
                          }}
                        />
                      </label>
                    )}
                    {!["queued", "resolving"].includes(item.status) && (
                      <button
                        className="danger"
                        disabled={Boolean(effectiveBusy)}
                        onClick={() => onDelete(item)}
                      >
                        {busy === `remove-preview-${item.id}` ? "删除中…" : "删除预检结果"}
                      </button>
                    )}
                  </div>
                </div>
              </article>
            ))}
          </div>
          {(!!selectable.length || confirmedWorkIds.length > 0) && (
            <div className="confirm-bar">
              <div>
                <strong>
                  {selectedVisibleIds.length > 0 && confirmedWorkIds.length > 0
                    ? `已勾选待确认 ${selectedVisibleIds.length} 个 · 已确认待入库 ${confirmedWorkIds.length} 个`
                    : confirmedWorkIds.length > 0
                      ? `已确认待入库 ${confirmedWorkIds.length} 个作品`
                      : `已勾选待确认 ${selectedVisibleIds.length} 个作品`}
                </strong>
                <small>
                  {selectedVisibleIds.length > 0 && confirmedWorkIds.length > 0
                    ? "新勾选作品需先确认；已确认作品可直接入库，两组操作互不影响"
                    : confirmedWorkIds.length > 0
                      ? "点击“入库”即可处理已经确认的作品"
                      : "勾选项尚未加入待入库，请先确认并保留上方收藏夹设置"}
                </small>
              </div>
              <div className="button-row">
                {!!selectable.length && (
                  <button
                    className="link"
                    disabled={Boolean(effectiveBusy)}
                    onClick={() => setSelected(new Set(selectable.map((item) => item.id)))}
                  >
                    选择全部可确认项
                  </button>
                )}
                {!!selectable.length && (
                  <button
                    className={confirmedWorkIds.length > 0 ? "secondary" : "primary"}
                    disabled={!selectedVisibleIds.length || Boolean(effectiveBusy)}
                    onClick={() => onConfirm(selectedVisibleIds)}
                  >
                    {busy === "confirm"
                      ? "正在确认…"
                      : `确认加入待入库（${selectedVisibleIds.length}）`}
                  </button>
                )}
                {confirmedWorkIds.length > 0 && (
                  <button
                    className="primary"
                    disabled={!!busy}
                    onClick={onIngestConfirmed}
                  >
                    {busy === "ingest-confirmed" ? "正在创建入库任务…" : `入库（${confirmedWorkIds.length}）`}
                  </button>
                )}
              </div>
            </div>
          )}
        </section>
      )}
    </section>
  );
}
