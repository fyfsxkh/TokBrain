"use client";

/* eslint-disable @next/next/no-img-element -- public covers and local assets are user-selected sources. */

import { CSSProperties, FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { AnswerBlock } from "../components/MarkdownContent";
import {
  API_BASE,
  api,
  ChatSource,
  Collection,
  Health,
  ImportBatch,
  ImportItem,
  Job,
  LibrarySummary,
  RuntimeSettings,
  Usage,
  WorksPage,
} from "../lib/api";
import { isUserCancelled } from "../lib/errors";
import { chooseObsidianImageDirectory, exportToObsidian } from "../lib/obsidian";
import { useTheme } from "../themes/ThemeProvider";
import { ThemeDefinition } from "../themes/registry";

type Tab = "import" | "library" | "chat" | "settings";
type LibraryState = "pending" | "in_library" | "issues" | "archived";
type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  sources?: ChatSource[];
  status?: "pending" | "error";
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

function reason(value: unknown, fallback = "操作失败") {
  return value instanceof Error ? value.message : fallback;
}

function localAssetUrl(value?: string | null) {
  return value?.startsWith("/api/") ? `${API_BASE}${value}` : null;
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
  return left.normalized_url === right.normalized_url;
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
  const [libraryState, setLibraryState] = useState<LibraryState>("in_library");
  const [collectionId, setCollectionId] = useState<number | null>(null);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [importText, setImportText] = useState("");
  const [batch, setBatch] = useState<ImportBatch | null>(null);
  const [retainedBatches, setRetainedBatches] = useState<ImportBatch[]>([]);
  const [selectedItems, setSelectedItems] = useState<Set<number>>(new Set());
  const [itemCollections, setItemCollections] = useState<Map<number, number>>(new Map());
  const [confirmedWorkIds, setConfirmedWorkIds] = useState<number[]>([]);
  const [question, setQuestion] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [chatMode, setChatMode] = useState<"fast" | "deep">("fast");
  const submittedImportTexts = useRef(new Map<string, string>());

  const loadCommon = useCallback(async () => {
    const results = await Promise.allSettled([
      api.health(),
      api.settings(),
      api.usage(),
      api.jobs(),
      api.collections(),
    ]);
    if (results[0].status === "fulfilled") setHealth(results[0].value);
    if (results[1].status === "fulfilled") setSettings(results[1].value);
    if (results[2].status === "fulfilled") setUsage(results[2].value);
    if (results[3].status === "fulfilled") setJobs(results[3].value);
    if (results[4].status === "fulfilled") {
      setCollections(results[4].value.items);
      setSummary(results[4].value.summary);
    }
  }, []);

  const loadLibrary = useCallback(
    async (append = false) => {
      const offset = append ? worksPage.items.length : 0;
      const [groups, page] = await Promise.all([
        api.collections(),
        api.works(libraryState, collectionId ?? undefined, offset),
      ]);
      setCollections(groups.items);
      setSummary(groups.summary);
      setWorksPage((current) => ({
        ...page,
        items: append ? [...current.items, ...page.items] : page.items,
      }));
    },
    [collectionId, libraryState, worksPage.items.length],
  );

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("tab") === "library") {
      setTab("library");
      const state = params.get("state");
      if (state && ["pending", "in_library", "issues", "archived"].includes(state)) {
        setLibraryState(state as LibraryState);
      }
    }
  }, []);

  useEffect(() => {
    loadCommon().catch(() => undefined);
  }, [loadCommon]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 5000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    if (tab === "library") loadLibrary(false).catch((value) => setError(reason(value)));
  }, [tab, libraryState, collectionId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!batch || TERMINAL_BATCH_STATES.has(batch.state)) return;
    const timer = window.setTimeout(async () => {
      try {
        const next = await api.importBatch(batch.id);
        setBatch(next);
        if (TERMINAL_BATCH_STATES.has(next.state)) {
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
          await loadCommon();
        }
      } catch (value) {
        setError(reason(value, "读取解析进度失败"));
      }
    }, 1000);
    return () => window.clearTimeout(timer);
  }, [batch, loadCommon]);

  useEffect(() => {
    const active = jobs.find(
      (job) => ["queued", "running", "cancelling"].includes(job.state) && job.job_type !== "link_preview",
    );
    if (!active) return;
    const timer = window.setTimeout(async () => {
      try {
        await loadCommon();
        if (batch) setBatch(await api.importBatch(batch.id));
        if (tab === "library") await loadLibrary(false);
      } catch {
        // The next polling cycle or a manual refresh will retry local status reads.
      }
    }, 2000);
    return () => window.clearTimeout(timer);
  }, [batch, jobs, loadCommon, loadLibrary, tab]);

  async function perform(name: string, operation: () => Promise<unknown>, success: string) {
    setBusy(name);
    setError("");
    try {
      await operation();
      setNotice(success);
      await loadCommon();
      return true;
    } catch (value) {
      if (isUserCancelled(value)) return false;
      setError(reason(value));
      return false;
    } finally {
      setBusy("");
    }
  }

  async function submitImport(event: FormEvent) {
    event.preventDefault();
    if (!importText.trim()) return;
    setBusy("import");
    setError("");
    const submittedText = importText;
    try {
      const created = await api.createImportBatch(submittedText);
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
        await loadCommon();
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
      setBusy("");
    }
  }

  async function cancelBatch() {
    if (!batch) return;
    setBusy("cancel-batch");
    try {
      setBatch(await api.cancelImportBatch(batch.id));
      setNotice("正在安全停止；已成功解析的结果会继续保留");
    } catch (value) {
      setError(reason(value));
    } finally {
      setBusy("");
    }
  }

  async function upload(item: ImportItem, files: File[]) {
    if (!files.length) return;
    const owner = [batch, ...retainedBatches].find((candidate) =>
      candidate?.items.some((entry) => entry.id === item.id),
    );
    if (!owner) return;
    setBusy(`upload-${item.id}`);
    setError("");
    try {
      await api.uploadImportAssets(item.id, files);
      if (item.existing_work_id) {
        await api.retry(item.existing_work_id);
      }
      const next = await api.importBatch(owner.id);
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
      setBusy("");
    }
  }

  async function removePreviewItem(item: ImportItem) {
    if (!window.confirm("确认删除这条预检结果？已加入知识库的作品不会在这里被删除。")) return;
    const owner = [batch, ...retainedBatches].find((candidate) =>
      candidate?.items.some((entry) => entry.id === item.id),
    );
    if (!owner) return;
    setBusy(`remove-preview-${item.id}`);
    setError("");
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
      setBusy("");
    }
  }

  async function confirmBatch(itemIds: number[]) {
    if (!itemIds.length) return;
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
    const accepted = await perform(
      "confirm",
      async () => {
        for (const group of groups) {
          const result = await api.confirmImportBatch(group.batch.id, group.items);
          confirmedIds.push(...result.work_ids);
        }
      },
      "所选作品已加入待入库，可直接点击“入库”开始处理",
    );
    if (accepted) {
      const uniqueConfirmedIds = [...new Set(confirmedIds)];
      setNotice(
        `已将 ${uniqueConfirmedIds.length} 个不同作品加入待入库，可直接点击“入库”开始处理`,
      );
      setConfirmedWorkIds((current) => [
        ...new Set([...current, ...uniqueConfirmedIds]),
      ]);
      setSelectedItems(new Set());
      setItemCollections(new Map());
      const refreshed = await Promise.all(
        candidates.map((entry) => api.importBatch(entry.id)),
      );
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
    if (!text || busy === "chat") return;
    const history = messages
      .filter((message) => !message.status)
      .map((message) => ({ role: message.role, content: message.content }));
    const user: Message = { id: crypto.randomUUID(), role: "user", content: text };
    const assistantId = crypto.randomUUID();
    setQuestion("");
    setMessages((current) => [
      ...current,
      user,
      { id: assistantId, role: "assistant", content: "正在查找相关作品…", status: "pending" },
    ]);
    setBusy("chat");
    try {
      let content = "";
      let sources: ChatSource[] = [];
      await api.askStream(text, history, chatMode, (streamEvent) => {
        if (streamEvent.type === "delta") content += streamEvent.text;
        if (streamEvent.type === "sources") sources = streamEvent.sources;
        if (streamEvent.type === "stage" && !content) content = streamEvent.message;
        setMessages((current) =>
          current.map((message) =>
            message.id === assistantId
              ? { ...message, content: content || "正在处理…", sources, status: undefined }
              : message,
          ),
        );
      });
    } catch (value) {
      setMessages((current) =>
        current.map((message) =>
          message.id === assistantId
            ? { ...message, content: reason(value, "回答失败"), status: "error" }
            : message,
        ),
      );
    } finally {
      setBusy("");
    }
  }

  const activeJobs = jobs
    .filter(
      (job) => ["queued", "running", "cancelling"].includes(job.state) && job.job_type !== "link_preview",
    )
    .sort((left, right) => {
      const priority = (job: Job) => job.state === "running" || job.state === "cancelling" ? 0 : 1;
      return priority(left) - priority(right)
        || new Date(left.created_at).getTime() - new Date(right.created_at).getTime();
    });
  const titles: Record<Tab, string> = {
    import: "视频链接导入",
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
        <nav>
          <button className={tab === "import" ? "active" : ""} onClick={() => setTab("import")}>导入<i /></button>
          <button className={tab === "library" ? "active" : ""} onClick={() => setTab("library")}>知识库<i /></button>
          <button className={tab === "chat" ? "active" : ""} onClick={() => setTab("chat")}>对话<i /></button>
          <button className={tab === "settings" ? "active" : ""} onClick={() => setTab("settings")}>设置<i /></button>
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
        {notice && <div className="toast ok">{notice}<button onClick={() => setNotice("")}>×</button></div>}
        {error && <div className="toast error">{error}<button onClick={() => setError("")}>×</button></div>}
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
        {tab === "import" && (
          <ImportWorkspace
            text={importText}
            setText={setImportText}
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
            onDelete={removePreviewItem}
            settings={settings}
            usage={usage}
            summary={summary}
            collections={collections}
            confirmedWorkIds={confirmedWorkIds}
          />
        )}
        {tab === "library" && (
          <Library
            key={`${libraryState}:${collectionId ?? "all"}`}
            state={libraryState}
            setState={setLibraryState}
            collectionId={collectionId}
            setCollectionId={setCollectionId}
            collections={collections}
            summary={summary}
            page={worksPage}
            reload={() => loadLibrary(false)}
            loadMore={() => loadLibrary(true)}
            perform={perform}
            globalSummaryPrompt={settings?.summary_prompt || ""}
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
      <div className="progress-track"><i style={{ width: `${percent}%` }} /></div>
      <span className={`pill ${job.state}`}>{stateLabel(job.state)}</span>
      <button className="danger" disabled={job.state === "cancelling"} onClick={onCancel}>安全停止</button>
    </section>
  );
}

function ImportWorkspace({
  text,
  setText,
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
  onDelete,
  settings,
  usage,
  summary,
  collections,
  confirmedWorkIds,
}: {
  text: string;
  setText: (value: string) => void;
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
  onDelete: (item: ImportItem) => void;
  settings: RuntimeSettings | null;
  usage: Usage | null;
  summary: LibrarySummary;
  collections: Collection[];
  confirmedWorkIds: number[];
}) {
  const active = batch && !TERMINAL_BATCH_STATES.has(batch.state);
  const completed = Number(batch?.progress.completed || 0);
  const percent = batch?.total_items ? Math.round((completed / batch.total_items) * 100) : 0;
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
  const selectable = displayItems.filter((item) => item.status === "ready");
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
    <section className="import-workspace stack">
      <div className="risk-notice" role="alert">
        <strong>导入风险提示</strong>
        <p>仅当平台明确返回作者允许下载时才处理完整视频；禁止下载或状态未知时，不下载视频、不抽帧，只尝试字幕、独立音频或基础信息。单作品解析仍可能触发平台风控。</p>
      </div>
      <section className="import-overview" aria-label="今日处理与知识库概况">
        <span><small>今日处理</small><strong>{usage?.daily_links_used || 0}<em>/ {usage?.daily_links_limit || settings?.import_daily_limit || 150}</em></strong></span>
        <span><small>今日 AI 用量</small><strong>{(usage?.daily_llm_tokens_used || 0).toLocaleString()}<em>/ {(usage?.daily_llm_tokens_limit || 0).toLocaleString()}</em></strong></span>
        <span><small>已入库</small><strong>{summary.local_item_count.toLocaleString()}</strong></span>
        <span><small>待入库</small><strong>{summary.candidate_count.toLocaleString()}</strong></span>
      </section>
      <form className="card import-form" onSubmit={onSubmit}>
        <div className="section-head">
          <div><span className="kicker">批量粘贴</span><h2>添加公开作品链接</h2></div>
          <small>每批最多 {settings?.import_batch_limit || 10} 条 · 每日最多 {settings?.import_daily_limit || 150} 条</small>
        </div>
        <textarea
          className="import-textarea"
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder={"可直接粘贴整段分享文案或每行一个链接，例如：\nhttps://v.douyin.com/...\nhttps://www.douyin.com/video/..."}
        />
        <div className="import-form-foot">
          <span>{text.length.toLocaleString()} 字符</span>
          <button className="primary" disabled={!text.trim() || busy === "import" || !!active}>
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
                {active && <button className="danger" disabled={busy === "cancel-batch"} onClick={onCancel}>{batch.state === "cancelling" ? "正在安全停止" : "中断解析"}</button>}
                <span className={`pill ${batch.state}`}>{stateLabel(batch.state)}</span>
              </div>
            </div>
            <div className="batch-progress"><i style={{ width: `${percent}%` }} /></div>
            <div className="batch-stats">
              <span>可确认 <strong>{batch.progress.ready || 0}</strong></span>
              <span>需补件 <strong>{batch.progress.needs_local_file || 0}</strong></span>
              <span>重复 <strong>{batch.progress.duplicates || 0}</strong></span>
              <span>失败 <strong>{batch.progress.failed || 0}</strong></span>
              <span>中断 <strong>{batch.progress.cancelled || 0}</strong></span>
              <span>今日剩余 <strong>{batch.remaining_daily}</strong></span>
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
                  {item.status === "ready" ? <input type="checkbox" checked={selected.has(item.id)} onChange={(event) => toggle(item.id, event.target.checked)} aria-label={`选择 ${item.title || item.input_url}`} /> : <span>{item.ordinal}</span>}
                </div>
                <div className="result-cover">{localAssetUrl(item.cover_url) ? <img src={localAssetUrl(item.cover_url)!} alt="" /> : <span>{item.kind === "image" ? "图文" : "链接"}</span>}</div>
                <div className="result-body">
                  <div><span className={`pill ${item.status}`}>{itemStatus(item)}</span>{item.error_code && <code>{visibleErrorCode(item.error_code)}</code>}</div>
                  <h3>{item.title || item.input_url}</h3>
                  <p>{item.author_name || "作者待识别"}{item.duration_seconds > 0 ? ` · ${Math.round(item.duration_seconds)} 秒` : ""}</p>
                  <p className="muted">{mediaPolicyLabel(item)}</p>
                  {item.status === "ready" && (
                    <label className="result-collection">
                      <span>加入收藏夹</span>
                      <select
                        value={itemCollections.get(item.id) ?? manualCollectionId ?? ""}
                        onChange={(event) => {
                          const next = new Map(itemCollections);
                          if (event.target.value) next.set(item.id, Number(event.target.value));
                          else next.delete(item.id);
                          setItemCollections(next);
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
                        <input type="file" multiple accept=".mp4,.mov,.mkv,.webm,.jpg,.jpeg,.png,.webp,video/mp4,video/quicktime,video/webm,image/jpeg,image/png,image/webp" disabled={busy === `upload-${item.id}`} onChange={(event) => onUpload(item, Array.from(event.target.files || []))} />
                      </label>
                    )}
                    {!["queued", "resolving"].includes(item.status) && (
                      <button
                        className="danger"
                        disabled={busy === `remove-preview-${item.id}`}
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
                    disabled={!!busy}
                    onClick={() => setSelected(new Set(selectable.map((item) => item.id)))}
                  >
                    选择全部可确认项
                  </button>
                )}
                {!!selectable.length && (
                  <button
                    className={confirmedWorkIds.length > 0 ? "secondary" : "primary"}
                    disabled={!selectedVisibleIds.length || !!busy}
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

function Library({
  state,
  setState,
  collectionId,
  setCollectionId,
  collections,
  summary,
  page,
  reload,
  loadMore,
  perform,
  globalSummaryPrompt,
}: {
  state: LibraryState;
  setState: (value: LibraryState) => void;
  collectionId: number | null;
  setCollectionId: (value: number | null) => void;
  collections: Collection[];
  summary: LibrarySummary;
  page: WorksPage;
  reload: () => Promise<void>;
  loadMore: () => Promise<void>;
  perform: (name: string, operation: () => Promise<unknown>, success: string) => Promise<boolean>;
  globalSummaryPrompt: string;
}) {
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [creatingCollection, setCreatingCollection] = useState(false);
  const [newCollectionTitle, setNewCollectionTitle] = useState("");
  const [targetCollectionId, setTargetCollectionId] = useState<number | null>(null);
  const [editingCollectionPrompt, setEditingCollectionPrompt] = useState(false);
  const activeCollection = collections.find((group) => group.id === collectionId);
  const [collectionPromptDraft, setCollectionPromptDraft] = useState(
    activeCollection?.summary_prompt || globalSummaryPrompt,
  );
  const selectedWorks = page.items.filter((work) => selected.has(work.id));
  const canOrganize = state === "pending" || state === "in_library";
  async function ingestSelected() {
    if (!selected.size) return;
    const count = selected.size;
    if (await perform("ingest-selected", () => api.ingest([...selected]), `已创建 ${count} 个作品的入库任务`)) {
      setSelected(new Set());
      await reload();
    }
  }
  async function summarize() {
    if (!selected.size) return;
    if (await perform("summarize", () => api.summarize([...selected]), `已创建 ${selected.size} 个作品的总结任务`)) setSelected(new Set());
  }
  async function exportNotes() {
    if (!selected.size) return;
    try {
      const manifest = await api.obsidianManifest([...selected]);
      const result = await exportToObsidian(manifest);
      if (result.failed) throw new Error(result.messages.join("；"));
      setSelected(new Set());
    } catch (value) {
      if (isUserCancelled(value)) return;
      window.alert(reason(value, "导出失败"));
    }
  }
  async function chooseImageFolder() {
    await perform(
      "obsidian-image-folder",
      () => chooseObsidianImageDirectory(),
      "Obsidian 图片统一存放位置已保存",
    );
  }
  async function createCollection() {
    const title = newCollectionTitle.trim();
    if (!title) return;
    let created: Collection | null = null;
    const ok = await perform(
      "create-collection",
      async () => {
        created = await api.createCollection(title);
      },
      `收藏夹“${title}”已创建`,
    );
    if (!ok) return;
    setNewCollectionTitle("");
    setCreatingCollection(false);
    if (created) setTargetCollectionId((created as Collection).id);
    await reload();
  }
  async function addToCollection() {
    if (!targetCollectionId || !selected.size) return;
    const target = collections.find((group) => group.id === targetCollectionId);
    const ok = await perform(
      "assign-collection",
      () => api.addWorksToCollection(targetCollectionId, [...selected]),
      `已加入收藏夹“${target?.title || "所选收藏夹"}”`,
    );
    if (!ok) return;
    setSelected(new Set());
    await reload();
  }
  async function saveCollectionPrompt(value: string | null = collectionPromptDraft) {
    if (!activeCollection) return;
    const prompt = value?.trim() || null;
    const ok = await perform(
      "collection-prompt",
      () => api.updateCollectionSummaryPrompt(activeCollection.id, prompt),
      prompt
        ? `收藏夹“${activeCollection.title}”的专属总结提示词已保存`
        : `收藏夹“${activeCollection.title}”已改用全局总结提示词`,
    );
    if (!ok) return;
    setCollectionPromptDraft(prompt || "");
    setEditingCollectionPrompt(false);
    await reload();
  }
  return (
    <section className="library-layout stack">
      <div className="library-toolbar">
        <div className="segmented">
          <button className={state === "pending" ? "active" : ""} onClick={() => setState("pending")}>待处理 <em>{summary.candidate_count}</em></button>
          <button className={state === "in_library" ? "active" : ""} onClick={() => setState("in_library")}>在库 <em>{summary.local_item_count}</em></button>
          <button className={state === "issues" ? "active" : ""} onClick={() => setState("issues")}>异常 <em>{summary.issue_count}</em></button>
          <button className={state === "archived" ? "active" : ""} onClick={() => setState("archived")}>已归档 <em>{summary.archived_count}</em></button>
        </div>
        <label className="collection-filter"><span>本地分组</span><select value={collectionId ?? ""} onChange={(event) => setCollectionId(event.target.value ? Number(event.target.value) : null)}><option value="">全部分组</option>{collections.map((group) => <option key={group.id} value={group.id}>{group.title}</option>)}</select></label>
      </div>
      <div className="collection-head">
        <div>
          <span className="kicker">本地知识空间</span>
          <h2>{collections.find((group) => group.id === collectionId)?.title || "全部作品"}</h2>
          <p>{page.total} 个结果</p>
        </div>
        <div className="collection-head-actions">
          {creatingCollection && (
            <div className="collection-create">
              <input
                value={newCollectionTitle}
                maxLength={100}
                autoFocus
                onChange={(event) => setNewCollectionTitle(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    createCollection();
                  }
                  if (event.key === "Escape") setCreatingCollection(false);
                }}
                placeholder="输入新收藏夹名称"
              />
              <button className="primary" disabled={!newCollectionTitle.trim()} onClick={createCollection}>创建</button>
              <button className="link" onClick={() => setCreatingCollection(false)}>取消</button>
            </div>
          )}
          <div className="button-row">
            {state === "in_library" && (
              <button className="secondary" onClick={chooseImageFolder}>设置图片统一存放位置</button>
            )}
            {canOrganize && <button className="secondary" onClick={() => setCreatingCollection((value) => !value)}>新建收藏夹</button>}
            {activeCollection && (
              <button
                className="secondary"
                onClick={() => {
                  setCollectionPromptDraft(
                    activeCollection.summary_prompt || globalSummaryPrompt,
                  );
                  setEditingCollectionPrompt((value) => !value);
                }}
              >
                设置总结提示词
              </button>
            )}
            {canOrganize && <button className="secondary" onClick={() => setSelected(new Set(page.items.map((work) => work.id)))}>选择当前页</button>}
            {canOrganize && <button className="link" onClick={() => setSelected(new Set())}>取消选择</button>}
          </div>
        </div>
      </div>
      {activeCollection && editingCollectionPrompt && (
        <section className="card collection-prompt-editor">
          <div className="section-head">
            <div>
              <span className="kicker">收藏夹专属 AI 规则</span>
              <h3>{activeCollection.title}</h3>
            </div>
            <small>{collectionPromptDraft.length.toLocaleString()} / 12,000</small>
          </div>
          <textarea
            maxLength={12000}
            value={collectionPromptDraft}
            onChange={(event) => setCollectionPromptDraft(event.target.value)}
            placeholder="请输入这个收藏夹专用的总结提示词"
          />
          <div className="collection-prompt-foot">
            <small>
              {activeCollection.summary_prompt
                ? "当前使用收藏夹专属提示词。修改只影响之后新入库或重新生成的总结。"
                : "当前继承全局提示词，已作为可编辑正文载入；保存后会成为这个收藏夹的专属提示词。"}
            </small>
            <div className="button-row">
              <button className="link" onClick={() => saveCollectionPrompt(null)}>使用全局提示词</button>
              <button className="primary" disabled={!collectionPromptDraft.trim()} onClick={() => saveCollectionPrompt()}>保存专属提示词</button>
            </div>
          </div>
        </section>
      )}
      <div className="work-grid">
        {page.items.map((work) => (
          <article className={`work-card ${selected.has(work.id) ? "selected" : ""}`} key={work.id}>
            {canOrganize && <label className="work-check"><input type="checkbox" checked={selected.has(work.id)} onChange={(event) => { const next = new Set(selected); if (event.target.checked) next.add(work.id); else next.delete(work.id); setSelected(next); }} /><span>选择</span></label>}
            {localAssetUrl(work.cover_url) ? <img src={localAssetUrl(work.cover_url)!} alt="" loading="lazy" decoding="async" /> : <div className="cover-placeholder">{work.kind === "image" ? "图文" : "视频"}</div>}
            <div className="work-body">
              <span className="kicker">{work.kind === "image" ? "图文" : "视频"} · {work.library_state}</span>
              <h3>{state === "in_library" ? <a className="work-title-link" href={`/works/${work.id}`}>{work.title}</a> : work.title}</h3>
              <p>{work.author_name || "未知作者"}{work.duration_seconds ? ` · ${Math.round(work.duration_seconds)} 秒` : ""}</p>
              {work.summary_excerpt && <p className="summary-excerpt">{work.summary_excerpt}</p>}
              <p className="collection-tags">{work.collections.join(" · ")}</p>
              <div className="work-actions">
                {work.source_url && <a href={work.source_url} target="_blank" rel="noreferrer">查看公开原作品</a>}
                {state === "pending" && <button onClick={() => perform("ingest-one", () => api.retry(work.id), "已创建入库任务").then(reload)}>开始入库</button>}
                {state === "issues" && <button onClick={() => perform("retry", () => api.retry(work.id), "已创建重试任务").then(reload)}>重新处理</button>}
                {state === "archived" && <button onClick={() => perform("restore", () => api.restore(work.id), "作品已恢复").then(reload)}>恢复</button>}
                <button
                  className="danger"
                  onClick={() =>
                    window.confirm("确认永久删除这个作品、总结、索引与本地资产？此操作不可恢复。")
                    && perform(`remove-${work.id}`, () => api.remove(work.id), "作品已永久删除").then(reload)
                  }
                >
                  永久删除
                </button>
              </div>
              {(work.error_code || work.process_error) && <div className="technical-details" aria-label="技术详情"><span className="technical-details-title">技术详情</span>{work.error_code && <small className="error-code">{work.error_code}</small>}{work.process_error && <small className="work-error">{work.process_error}</small>}</div>}
            </div>
          </article>
        ))}
        {!page.items.length && <div className="empty"><strong>这个范围还没有作品</strong><p>前往“导入”粘贴您有权处理的公开作品链接。</p></div>}
      </div>
      {page.next_offset != null && <button className="secondary load-more" onClick={loadMore}>加载更多</button>}
      {canOrganize && selectedWorks.length > 0 && (
        <div className="selection-bar">
          <div>
            <strong>已选择 {selectedWorks.length} 个{state === "pending" ? "待处理" : "在库"}作品</strong>
            <small>收藏夹关系会在作品完成 AI 处理后继续保留</small>
          </div>
          <div className="selection-actions">
            <select value={targetCollectionId ?? ""} onChange={(event) => setTargetCollectionId(event.target.value ? Number(event.target.value) : null)}>
              <option value="">选择目标收藏夹</option>
              {collections.map((group) => <option key={group.id} value={group.id}>{group.title}</option>)}
            </select>
            <button className="secondary" disabled={!targetCollectionId} onClick={addToCollection}>加入收藏夹</button>
            {state === "pending" && <button className="primary" onClick={ingestSelected}>批量开始入库</button>}
            {state === "in_library" && <button className="secondary" onClick={summarize}>补齐/更新总结</button>}
            {state === "in_library" && <button className="primary" onClick={exportNotes}>导出到 Obsidian</button>}
          </div>
        </div>
      )}
    </section>
  );
}

function Chat({
  question,
  setQuestion,
  messages,
  busy,
  mode,
  setMode,
  initialFormat,
  onSubmit,
}: {
  question: string;
  setQuestion: (value: string) => void;
  messages: Message[];
  busy: boolean;
  mode: "fast" | "deep";
  setMode: (value: "fast" | "deep") => void;
  initialFormat: RuntimeSettings["default_answer_format"];
  onSubmit: (event: FormEvent) => void;
}) {
  return (
    <section className="chat-layout">
      <div className="chat-intro">
        <span className="kicker">有来源的回答</span>
        <h2>和本地知识库对话</h2>
        <p>回答仅检索已确认并处理完成的作品，来源链接可追溯。</p>
        <div className="mode-switch">
          <button type="button" className={mode === "fast" ? "active" : ""} onClick={() => setMode("fast")}>快速回答</button>
          <button type="button" className={mode === "deep" ? "active" : ""} onClick={() => setMode("deep")}>深度回答</button>
        </div>
        <small className="chat-format-hint">回答生成后可切换阅读排版、Markdown 和纯文本。</small>
      </div>
      <div className="conversation">
        <div className="message-list">
          {!messages.length && <div className="empty compact"><strong>问一个具体问题</strong><p>例如：“知识库中关于颈椎拉伸有哪些步骤？”</p></div>}
          {messages.map((message) => (
            <article className={`chat-message ${message.role} ${message.status || ""}`} key={message.id}>
              {message.role === "assistant" && <span>TokBrain</span>}
              {message.role === "assistant" && !message.status
                ? <AnswerBlock content={message.content} initialFormat={initialFormat} />
                : <p>{message.content}</p>}
              {message.sources?.length ? (
                <div className="sources">
                  {message.sources.map((source) => (
                    <article key={source.work_id}>
                      <a href={`/works/${source.work_id}`}><strong>{source.title}</strong><small>{source.collection || "本地知识库"}</small></a>
                      {source.external_url && <a href={source.external_url} target="_blank" rel="noreferrer">查看公开原作品</a>}
                    </article>
                  ))}
                </div>
              ) : null}
            </article>
          ))}
        </div>
        <form className="chat-form" onSubmit={onSubmit}>
          <textarea
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
              event.preventDefault();
              if (!busy && question.trim()) event.currentTarget.form?.requestSubmit();
            }}
            placeholder="向本地知识库提问…"
          />
          <button className="primary" disabled={busy || !question.trim()}>{busy ? "正在回答" : "发送"}</button>
        </form>
      </div>
    </section>
  );
}

function ThemePicker() {
  const { theme, themes, setTheme, backgroundIntensity, setBackgroundIntensity } = useTheme();
  return (
    <section className="card theme-picker">
      <div className="section-head">
        <div><span className="kicker">界面皮肤</span><h2>选择主题</h2></div>
        <span className="theme-current"><b>{theme.mark}</b>{theme.name}</span>
      </div>
      <div className="theme-grid" role="radiogroup" aria-label="界面主题">
        {themes.map((item) => (
          <button
            type="button"
            role="radio"
            aria-checked={item.id === theme.id}
            className={`theme-card ${item.id === theme.id ? "selected" : ""}`}
            key={item.id}
            onClick={() => setTheme(item.id)}
            style={themePreviewStyle(item)}
          >
            <span className="theme-preview"><span className="theme-badge">{item.mark}</span></span>
            <span className="theme-card-copy"><strong>{item.name}</strong><small>{item.description}</small></span>
          </button>
        ))}
      </div>
      <label className="background-intensity">
        <span><strong>背景显现度</strong><small>调整主题背景图的可见程度</small></span>
        <input aria-label="背景显现度" type="range" min="0" max="100" value={backgroundIntensity} onChange={(event) => setBackgroundIntensity(Number(event.target.value))} />
        <output>{backgroundIntensity}%</output>
      </label>
    </section>
  );
}

function themePreviewStyle(theme: ThemeDefinition) {
  return {
    "--preview-bg": theme.preview[0],
    "--preview-surface": theme.preview[1],
    "--preview-accent": theme.preview[2],
    "--preview-text": theme.preview[3],
  } as CSSProperties;
}

const HEALTH_PROBE_NAMES = ["database", "media_runtime", "security_cleanup"] as const;

function healthFromProbes(probes: Health["probes"]): Health {
  const overall = probes.some((probe) => probe.status === "down")
    ? "down"
    : probes.some((probe) => probe.status === "degraded")
      ? "degraded"
      : "healthy";
  return {
    overall,
    summary: overall === "healthy" ? "本地运行环境正常" : "部分本地处理能力需要处理",
    checked_at: new Date().toISOString(),
    probes,
  };
}

function modelOptionLabel(model: string) {
  const notes: Record<string, string> = {
    "qwen3.6-flash": "低成本默认",
    "qwen3.7-flash": "新一代轻量",
    "qwen3.7-plus": "能力与成本均衡",
    "qwen3.7-max": "高能力",
    "qwen-math-turbo": "数学专项，仅建议用于对话",
    "deepseek-r1-distill-qwen-7b": "推理蒸馏",
    "deepseek-v4-flash": "第三方轻量",
    "deepseek-v4-pro": "第三方高能力",
    "glm-5": "第三方推理",
    "glm-5.1": "第三方推理",
    "glm-5.2": "第三方推理",
  };
  return notes[model] ? `${model}（${notes[model]}）` : model;
}

function Settings({
  settings,
  usage,
  health,
  onHealthChange,
  busy,
  perform,
}: {
  settings: RuntimeSettings;
  usage: Usage | null;
  health: Health | null;
  onHealthChange: (health: Health) => void;
  busy: string;
  perform: (name: string, operation: () => Promise<unknown>, success: string) => Promise<boolean>;
}) {
  const [checking, setChecking] = useState(false);
  const [checkProgress, setCheckProgress] = useState(health ? 100 : 0);
  const [liveProbes, setLiveProbes] = useState<Health["probes"]>(health?.probes || []);
  const [f2CookieDraft, setF2CookieDraft] = useState("");
  const [billingAccessKeyIdDraft, setBillingAccessKeyIdDraft] = useState("");
  const [billingAccessKeySecretDraft, setBillingAccessKeySecretDraft] = useState("");
  const [summaryPromptDraft, setSummaryPromptDraft] = useState(settings.summary_prompt);

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const numeric = ["daily_media_minutes_limit", "daily_llm_token_limit", "monthly_warning_cny", "scene_threshold", "max_scene_candidates", "max_keyframes", "min_keyframe_gap_seconds"];
    const body: Record<string, unknown> = {};
    numeric.forEach((name) => body[name] = Number(form.get(name)));
    ["dashscope_api_key", "bss_access_key_id", "bss_access_key_secret", "f2_cookie"].forEach((name) => { const value = String(form.get(name) || "").trim(); if (value) body[name] = value; });
    ["processing_model", "chat_fast_model", "chat_deep_model"].forEach((name) => {
      body[name] = String(form.get(name) || "");
    });
    body.clear_f2_cookie = form.get("clear_f2_cookie") === "on";
    body.default_answer_format = String(form.get("default_answer_format") || "rich");
    body.summary_prompt = summaryPromptDraft.trim() || settings.default_summary_prompt;
    const saved = await perform("settings", () => api.saveSettings(body), "设置已保存");
    if (saved) {
      setBillingAccessKeyIdDraft("");
      setBillingAccessKeySecretDraft("");
    }
  }

  async function runHealthDetection() {
    setChecking(true);
    setCheckProgress(0);
    setLiveProbes([]);
    await perform(
      "health-check",
      async () => {
        const nextProbes: Health["probes"] = [];
        for (const [index, probeName] of HEALTH_PROBE_NAMES.entries()) {
          const probe = await api.healthProbe(probeName);
          nextProbes.push(probe);
          setLiveProbes([...nextProbes]);
          setCheckProgress(Math.round(((index + 1) / HEALTH_PROBE_NAMES.length) * 100));
          // Keep each genuine probe result visible long enough to be perceived
          // instead of flashing directly from 0% to 100% on fast local machines.
          await new Promise((resolve) => window.setTimeout(resolve, 180));
        }
        onHealthChange(healthFromProbes(nextProbes));
      },
      "本地检测已完成",
    );
    setChecking(false);
  }

  async function saveF2Cookie() {
    const value = f2CookieDraft.trim();
    if (!value) return;
    const saved = await perform(
      "f2-cookie",
      () => api.saveSettings({ f2_cookie: value, clear_f2_cookie: false }),
      "解析 Cookie 已加密保存，可以返回导入页重新预检",
    );
    if (saved) setF2CookieDraft("");
  }

  async function saveBillingCredentials() {
    const accessKeyId = billingAccessKeyIdDraft.trim();
    const accessKeySecret = billingAccessKeySecretDraft.trim();
    if (!accessKeyId && !accessKeySecret) return;
    const saved = await perform(
      "billing-credentials",
      () => api.saveSettings({
        ...(accessKeyId ? { bss_access_key_id: accessKeyId } : {}),
        ...(accessKeySecret ? { bss_access_key_secret: accessKeySecret } : {}),
      }),
      "账单查询凭据已在本机后台加密保存",
    );
    if (saved) {
      setBillingAccessKeyIdDraft("");
      setBillingAccessKeySecretDraft("");
    }
  }

  async function clearAllKeys() {
    const confirmed = window.confirm(
      "确定删除本机保存的全部百炼 API Key 和账单 AccessKey 吗？删除后 AI 处理、对话和官方账单查询将不可用，直至重新填写。",
    );
    if (!confirmed) return;
    const cleared = await perform(
      "clear-all-keys",
      () => api.clearAllKeys(),
      "全部模型 API Key 与账单 AccessKey 已删除",
    );
    if (cleared) {
      setBillingAccessKeyIdDraft("");
      setBillingAccessKeySecretDraft("");
    }
  }

  async function saveSummaryPrompt(value = summaryPromptDraft) {
    const prompt = value.trim();
    if (!prompt) return;
    await perform(
      "summary-prompt",
      () => api.saveSettings({ summary_prompt: prompt }),
      "AI 总结提示词已保存，之后创建的总结任务将使用此内容",
    );
  }

  async function resetSummaryPrompt() {
    setSummaryPromptDraft(settings.default_summary_prompt);
    await saveSummaryPrompt(settings.default_summary_prompt);
  }

  const activeProbeIndex = checking
    ? Math.min(HEALTH_PROBE_NAMES.length - 1, liveProbes.length)
    : -1;
  const visibleProbes = checking ? liveProbes : liveProbes.length ? liveProbes : health?.probes || [];

  return (
    <div className="settings-layout">
      <form className="stack" onSubmit={save}>
        <ThemePicker />
        {settings.security_cleanup_required && (
          <section className="risk-notice"><strong>敏感残留尚未清理</strong><p>{settings.security_cleanup_message}</p></section>
        )}
        <section className="card principles-card">
          <div className="principles-title"><span className="kicker">本地解析原则</span><h2>主动、低频、可中断</h2></div>
          <div className="principles-inline" aria-label="本地解析原则详情">
            <span>有权公开内容</span>
            <span>预检不下载、不调用 AI</span>
            <span>解析规则变化即失败</span>
            <span>媒体缺失可补件</span>
            <span>不绕过平台限制</span>
          </div>
        </section>
        <section className="card">
          <div className="section-head">
            <div><span className="kicker">固定安全策略</span><h2>单作品访问护栏</h2></div>
            <span className="pill succeeded">不可提高</span>
          </div>
          <div className="safety-grid">
            <span>每批<strong>{settings.import_batch_limit}</strong></span>
            <span>每日<strong>{settings.import_daily_limit}</strong></span>
          </div>
          <p className="muted">TokBrain 仅在您主动提交链接或确认入库后进行单作品解析；不会扫描账号或收藏夹，应用启动和本地检查不会访问抖音。</p>
        </section>
        <section className="card health-card">
          <div className="section-head">
            <div><span className="kicker">本地检查</span><h2>{health?.summary || "本地运行环境"}</h2></div>
            <div className="health-detection">
              <button type="button" className="secondary" disabled={checking || busy === "health-check"} onClick={runHealthDetection}>
                {checking ? "检测中…" : "重新检测"}
              </button>
              <span>{checking ? `${checkProgress}%` : health ? "检测完成" : "尚未检测"}</span>
            </div>
          </div>
          <div className="health-progress" aria-label="本地检测进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={checkProgress} role="progressbar">
            <i style={{ width: `${checkProgress}%` }} />
          </div>
          <div className="probe-list">
            {HEALTH_PROBE_NAMES.map((probeName, index) => {
              const probe = visibleProbes.find((item) => item.probe === probeName);
              const pendingState = checking && index === activeProbeIndex ? "checking" : "unknown";
              return (
                <div className="probe" key={probeName}>
                  <span className={`signal ${probe?.status || pendingState}`} />
                  <div>
                    <strong>{probeName === "database" ? "本地数据库" : probeName === "media_runtime" ? "音视频工具" : "敏感数据清理"}</strong>
                    <small>{probe?.message || (pendingState === "checking" ? "正在检测…" : "等待检测")}</small>
                  </div>
                </div>
              );
            })}
          </div>
        </section>
        <section className="card">
          <div className="section-head"><div><span className="kicker">处理额度</span><h2>媒体、AI 与费用</h2></div></div>
          <div className="form-grid">
            <Field name="daily_media_minutes_limit" label="每日媒体分钟" value={settings.daily_media_minutes_limit} min={1} />
            <Field name="daily_llm_token_limit" label="每日 AI Token" value={settings.daily_llm_token_limit} min={1000} />
            <Field name="monthly_warning_cny" label="月度费用预警" value={settings.monthly_warning_cny} min={0} step=".01" />
            <Field name="scene_threshold" label="画面变化灵敏度" value={settings.scene_threshold} min={0.05} max={0.95} step=".05" />
            <Field name="max_scene_candidates" label="初选画面上限" value={settings.max_scene_candidates} min={12} max={1000} />
            <Field name="max_keyframes" label="最终保留画面" value={settings.max_keyframes} min={1} max={48} />
            <Field name="min_keyframe_gap_seconds" label="画面最小间隔" value={settings.min_keyframe_gap_seconds} min={0.2} max={60} step=".1" />
            <label className="field">
              <span>回答默认格式</span>
              <select name="default_answer_format" defaultValue={settings.default_answer_format}>
                <option value="rich">阅读排版</option><option value="markdown">Markdown</option><option value="plain">纯文本</option>
              </select>
            </label>
            <label className="field">
              <span>视频/图文总结模型</span>
              <select name="processing_model" defaultValue={settings.processing_model}>
                {settings.processing_model_options.map((model) => (
                  <option key={model} value={model}>{modelOptionLabel(model)}</option>
                ))}
              </select>
              <small>仅列出支持 JSON 结构化总结的文本生成模型；向量、重排、语音模型不能用于这里。</small>
            </label>
            <label className="field">
              <span>快速回答模型</span>
              <select name="chat_fast_model" defaultValue={settings.chat_fast_model}>
                {settings.chat_model_options.map((model) => (
                  <option key={model} value={model}>{modelOptionLabel(model)}</option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>深度回答模型</span>
              <select name="chat_deep_model" defaultValue={settings.chat_deep_model}>
                {settings.chat_model_options.map((model) => (
                  <option key={model} value={model}>{modelOptionLabel(model)}</option>
                ))}
              </select>
              <small>第三方或专项模型须先在百炼控制台开通；本地估算未覆盖的模型以官方账单为准。</small>
            </label>
            <div className="field model-readonly">
              <span>固定专用模型</span>
              <p>画面识别：{settings.ocr_model}<br />语音转写：{settings.asr_model}<br />向量检索：{settings.embedding_model}</p>
            </div>
            <div className="field wide summary-prompt-field">
              <span>视频 AI 总结提示词</span>
              <textarea
                name="summary_prompt"
                rows={18}
                value={summaryPromptDraft}
                onChange={(event) => setSummaryPromptDraft(event.target.value)}
                placeholder="用于控制视频和图文入库后的 AI 总结方式"
              />
              <div className="prompt-field-actions">
                <small>{summaryPromptDraft.length.toLocaleString()} / 12,000 字符；修改只影响之后新建或重新生成的总结</small>
                <div className="button-row">
                  <button type="button" className="link" disabled={busy === "summary-prompt"} onClick={resetSummaryPrompt}>一键恢复默认提示词</button>
                  <button type="button" className="secondary" disabled={!summaryPromptDraft.trim() || busy === "summary-prompt"} onClick={() => saveSummaryPrompt()}>
                    {busy === "summary-prompt" ? "保存中…" : "保存提示词"}
                  </button>
                </div>
              </div>
            </div>
            <label className="field wide"><span>百炼模型密钥 {settings.has_dashscope_key && "（已保存，留空不修改）"}</span><input name="dashscope_api_key" type="password" autoComplete="off" /></label>
            <div className="field wide cookie-field">
              <span>可选解析 Cookie {settings.has_f2_cookie && "（已保存并生效）"}</span>
              <textarea
                name="f2_cookie"
                rows={3}
                autoComplete="off"
                value={f2CookieDraft}
                onChange={(event) => setF2CookieDraft(event.target.value)}
                placeholder="粘贴完整 Cookie 后，必须点击下方“保存 Cookie”才会生效"
              />
              <div className="cookie-field-actions">
                <small>{settings.has_f2_cookie ? "已保存；重新粘贴可覆盖旧 Cookie" : "尚未保存 Cookie"}</small>
                <button
                  type="button"
                  className="secondary"
                  disabled={!f2CookieDraft.trim() || busy === "f2-cookie"}
                  onClick={saveF2Cookie}
                >
                  {busy === "f2-cookie" ? "保存中…" : "保存 Cookie"}
                </button>
              </div>
            </div>
            {settings.has_f2_cookie && <label className="field checkbox-field"><span>清除已保存的解析 Cookie</span><input name="clear_f2_cookie" type="checkbox" /></label>}
            <label className="field">
              <span>账单查询 AccessKey ID {settings.has_bss_credentials && "（后台已保存）"}</span>
              <input
                name="bss_access_key_id"
                type="password"
                autoComplete="off"
                value={billingAccessKeyIdDraft}
                onChange={(event) => setBillingAccessKeyIdDraft(event.target.value)}
                placeholder={settings.has_bss_credentials ? "已加密保存；无需重新输入" : "请输入只读账单 AccessKey ID"}
              />
            </label>
            <label className="field">
              <span>账单查询 AccessKey Secret {settings.has_bss_credentials && "（后台已保存）"}</span>
              <input
                name="bss_access_key_secret"
                type="password"
                autoComplete="off"
                value={billingAccessKeySecretDraft}
                onChange={(event) => setBillingAccessKeySecretDraft(event.target.value)}
                placeholder={settings.has_bss_credentials ? "已加密保存；不会回显完整密钥" : "请输入只读账单 AccessKey Secret"}
              />
            </label>
            <div className="field wide billing-credentials-status">
              <span>账单凭据保存状态</span>
              <div>
                <small>
                  {settings.has_bss_credentials
                    ? "AccessKey ID 与 Secret 已保存在本机后台，退出页面或重启应用后仍然有效。为避免泄露，密码框不会显示原文。"
                    : "尚未保存账单凭据。请同时填写 ID 与 Secret 后点击右侧按钮。"}
                </small>
                <button
                  type="button"
                  className="secondary"
                  disabled={
                    (!billingAccessKeyIdDraft.trim() && !billingAccessKeySecretDraft.trim())
                    || (!settings.has_bss_credentials && (!billingAccessKeyIdDraft.trim() || !billingAccessKeySecretDraft.trim()))
                    || busy === "billing-credentials"
                  }
                  onClick={saveBillingCredentials}
                >
                  {busy === "billing-credentials" ? "保存中…" : settings.has_bss_credentials ? "更新账单凭据" : "保存账单凭据"}
                </button>
              </div>
            </div>
            <div className="field wide billing-summary">
              <span>本月账单</span>
              <div className="billing-summary-grid">
                <span>
                  <small>TokBrain 本地估算</small>
                  <strong>¥ {(usage?.month_estimated_cny || 0).toFixed(4)}</strong>
                </span>
                <span>
                  <small>阿里云官方账单</small>
                  <strong>{usage?.official_billed_cny == null ? "尚未查询" : `¥ ${usage.official_billed_cny.toFixed(4)}`}</strong>
                </span>
                <span>
                  <small>官方账单状态</small>
                  <strong>{
                    usage?.official_status === "available_delayed"
                      ? "已获取（存在结算延迟）"
                      : usage?.official_status === "error"
                        ? "查询失败"
                        : "尚未查询"
                  }</strong>
                </span>
                <span>
                  <small>官方数据更新时间</small>
                  <strong>{usage?.official_data_as_of ? new Date(usage.official_data_as_of).toLocaleString("zh-CN") : "—"}</strong>
                </span>
              </div>
            </div>
            <div className="field wide billing-refresh">
              <span>官方账单核对</span>
              <div>
                <small>凭据保存后可直接刷新；官方数据通常有约 24 小时结算延迟，可能与本地实时估算不同。</small>
                <button
                  type="button"
                  className="secondary"
                  disabled={!settings.has_bss_credentials || busy === "official-bill"}
                  onClick={() => perform("official-bill", () => api.refreshOfficialBill(), "官方账单已刷新")}
                >
                  {busy === "official-bill" ? "查询中…" : "刷新官方账单"}
                </button>
              </div>
            </div>
          </div>
          <p className="muted">{settings.dpapi_warning}</p>
          <p className="muted">今日公开链接 {usage?.daily_links_used || 0}/{usage?.daily_links_limit || 150} · 今日 AI {(usage?.daily_llm_tokens_used || 0).toLocaleString()}/{(usage?.daily_llm_tokens_limit || 0).toLocaleString()}</p>
        </section>
        <section className="card credential-cleanup">
          <div>
            <span className="kicker">敏感凭据清理</span>
            <h2>删除全部 API Key 与 AccessKey</h2>
            <p className="muted">删除百炼模型 API Key、账单 AccessKey ID/Secret 及已缓存的官方账单；不会删除知识库、作品、总结或解析 Cookie。</p>
          </div>
          <button type="button" className="danger" disabled={busy === "clear-all-keys"} onClick={clearAllKeys}>
            {busy === "clear-all-keys" ? "删除中…" : "删除全部密钥"}
          </button>
        </section>
        <button className="primary save" disabled={busy === "settings"}>{busy === "settings" ? "保存中…" : "保存设置"}</button>
      </form>
    </div>
  );
}

function Field({ name, label, value, min, max, step = "1" }: { name: string; label: string; value: number; min: number; max?: number; step?: string }) {
  return <label className="field"><span>{label}</span><div className="field-control"><input name={name} type="number" defaultValue={value} min={min} max={max} step={step} /></div></label>;
}
