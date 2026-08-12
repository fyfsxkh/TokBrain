"use client";

/* eslint-disable @next/next/no-img-element -- public covers and local assets are user-selected sources. */

import { useState } from "react";

import { api } from "../lib/api";
import type { Collection, LibrarySummary, Work, WorksPage } from "../lib/api";
import { localAssetUrl } from "../lib/assets";
import { isUserCancelled, reason } from "../lib/errors";
import { rememberLibraryReturnContext } from "../lib/libraryReturn";
import { chooseObsidianImageDirectory, exportToObsidian } from "../lib/obsidian";
import type { LibraryState, PerformOperation } from "../lib/uiTypes";

const SUPPLEMENT_REASON_LABELS: Record<string, string> = {
  full_video_unavailable: "未取得完整视频文件；已有总结可能来自字幕、音频或部分画面",
  missing_full_video: "未取得完整视频文件；已有总结可能来自字幕、音频或部分画面",
  video_missing: "未取得完整视频文件；已有总结可能来自字幕、音频或部分画面",
  missing_video_track: "缺少完整视频画面",
  visual_track_missing: "画面轨未完成",
  incomplete_images: "图片组未完整取得或解析；已有总结可能来自已取得的图片",
  image_set_incomplete: "图片组未完整取得或解析；已有总结可能来自已取得的图片",
  audio_unavailable: "音频或字幕内容不可用",
  evidence_insufficient: "没有足够的字幕、音频或画面材料",
  insufficient_evidence: "没有足够的字幕、音频或画面材料",
};

const TRACK_LABELS: Record<string, string> = {
  audio: "音频",
  subtitle: "字幕",
  transcript: "转写",
  video: "视频",
  visual: "画面",
  ocr: "OCR",
  images: "图片",
  image: "图片",
  expected_images: "预计图片",
  processed_images: "已处理图片",
  successful_images: "有效图片",
  missing_images: "缺失图片",
  missing_indices: "缺失位置",
  evidence: "材料判定",
  evidence_kinds: "可用材料",
};

const HIDDEN_TRACK_REPORT_KEYS = new Set(["migration", "kind"]);

function libraryStateLabel(value: string) {
  return ({
    pending: "待处理",
    in_library: "已入库",
    issues: "异常",
    archived: "已归档",
  } as Record<string, string>)[value] || value;
}

function supplementReasonLabel(value?: string | null) {
  if (!value) return "需要补充完整原始素材";
  return SUPPLEMENT_REASON_LABELS[value] || value;
}

function supplementStateLabel(value?: string, evidenceState?: string) {
  return ({
    required: evidenceState === "sufficient" ? "建议补充" : "需要补充",
    uploaded: "已上传",
    processing: "重新处理中",
    failed: "补件处理失败",
  } as Record<string, string>)[value || ""] || "待补件";
}

function trackValueLabel(value: unknown): string {
  if (value == null) return "未执行";
  if (typeof value === "boolean") return value ? "有效" : "无有效内容";
  if (typeof value === "number") return `${value}`;
  if (typeof value === "string") {
    return ({
      sufficient: "有效",
      insufficient: "无有效内容",
      unverified: "待验证",
      none: "无",
      complete: "完整",
      completed: "已完成",
      processed: "已处理",
      recorded: "已记录",
      empty: "无有效内容",
      not_applicable: "不适用",
      not_used: "未使用",
      no_stream: "无音轨",
      subtitle: "字幕",
      transcript: "音频转写",
      ocr: "图片文字",
      visual: "画面分析",
      video: "视频",
      image: "图片",
      missing: "缺失",
      unavailable: "不可用",
      failed: "失败",
      skipped: "未执行",
      no_track: "无此轨道",
    } as Record<string, string>)[value] || value;
  }
  if (Array.isArray(value)) return value.length ? value.map(trackValueLabel).join("、") : "无";
  if (typeof value === "object") {
    const report = value as Record<string, unknown>;
    const parts: string[] = [];
    if (typeof report.available === "boolean") {
      parts.push(report.available ? "可用" : "未取得");
    }
    const status = report.status ?? report.state;
    if (status != null) parts.push(trackValueLabel(status));
    const valid = report.valid ?? report.evidence_valid;
    if (typeof valid === "boolean") parts.push(valid ? "材料有效" : "无有效材料");
    const count = report.evidence_count ?? report.count ?? report.valid_count;
    if (typeof count === "number") parts.push(`${count} 条材料`);
    const expected = report.expected ?? report.expected_count;
    const processed = report.processed ?? report.processed_count ?? report.success_count;
    if (typeof expected === "number" || typeof processed === "number") {
      parts.push(`${typeof processed === "number" ? processed : 0}/${typeof expected === "number" ? expected : "?"} 已处理`);
    }
    const missing = report.missing_indices ?? report.missing;
    if (Array.isArray(missing) && missing.length) parts.push(`缺第 ${missing.join("、")} 张`);
    const failed = report.failed_positions;
    if (Array.isArray(failed) && failed.length) parts.push(`第 ${failed.join("、")} 项处理失败`);
    if (typeof report.text_char_count === "number" && typeof report.text_threshold === "number") {
      parts.push(`有效文本 ${report.text_char_count}/${report.text_threshold} 字`);
    }
    if (typeof report.visual_valid === "boolean") {
      parts.push(report.visual_valid ? "有有效画面材料" : "无有效画面材料");
    }
    return parts.join(" · ") || "已记录";
  }
  return String(value);
}

function audioSubtitleTrackValue(report: Record<string, unknown>) {
  const kinds = new Set(
    Array.isArray(report.evidence_kinds)
      ? report.evidence_kinds.filter((value): value is string => typeof value === "string")
      : [],
  );
  const audio = report.audio != null
    ? trackValueLabel(report.audio)
    : kinds.has("transcript")
      ? "有音频转写"
      : "未取得";
  const subtitle = report.subtitle != null
    ? trackValueLabel(report.subtitle)
    : kinds.has("subtitle")
      ? "可用"
      : "未取得";
  return `音频：${audio} · 字幕：${subtitle}`;
}

function trackReportRows(report?: Record<string, unknown> | null, kind?: string) {
  if (!report) return [];
  const rows: Array<{ key: string; label: string; value: string }> = [];
  const consumed = new Set(["video", "audio", "subtitle"]);
  if (kind === "video" || report.video != null) {
    rows.push({
      key: "video",
      label: "视频",
      value: trackValueLabel(report.video ?? { available: false }),
    });
    rows.push({
      key: "audio-subtitle",
      label: "音频/字幕",
      value: audioSubtitleTrackValue(report),
    });
  }
  rows.push(...Object.entries(report)
    .filter(([key]) => !consumed.has(key) && !HIDDEN_TRACK_REPORT_KEYS.has(key) && TRACK_LABELS[key])
    .map(([key, value]) => ({
      key,
      label: TRACK_LABELS[key],
      value: trackValueLabel(value),
    })));
  return rows;
}

export function Library({
  state,
  setState,
  collectionId,
  setCollectionId,
  collections,
  summary,
  page,
  reload,
  loadMore,
  loading,
  perform,
  globalSummaryPrompt,
  rightsAttested,
  setRightsAttested,
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
  loading: boolean;
  perform: PerformOperation;
  globalSummaryPrompt: string;
  rightsAttested: boolean;
  setRightsAttested: (value: boolean) => void;
}) {
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [creatingCollection, setCreatingCollection] = useState(false);
  const [newCollectionTitle, setNewCollectionTitle] = useState("");
  const [targetCollectionId, setTargetCollectionId] = useState<number | null>(null);
  const [editingCollectionPrompt, setEditingCollectionPrompt] = useState(false);
  const [supplementingWorkId, setSupplementingWorkId] = useState<number | null>(null);
  const activeCollection = collections.find((group) => group.id === collectionId);
  const [collectionPromptDraft, setCollectionPromptDraft] = useState(
    activeCollection?.summary_prompt || globalSummaryPrompt,
  );
  const selectedWorks = page.items.filter((work) => selected.has(work.id));
  const canOrganize = state === "pending" || state === "in_library";
  function rememberSummaryReturn(workId: number) {
    const params = new URLSearchParams({
      tab: "library",
      state,
      view: "works",
    });
    if (collectionId != null) params.set("collection_id", String(collectionId));
    const url = `/?${params.toString()}`;
    window.history.replaceState(window.history.state, "", url);
    rememberLibraryReturnContext({
      workId,
      state,
      collectionId,
      scrollY: window.scrollY,
      url,
    });
  }
  async function uploadSupplement(work: Work, files: File[]) {
    if (!rightsAttested || !files.length || supplementingWorkId != null) return;
    if (work.kind === "image" && files.length > 12) {
      window.alert("图文补件最多选择 12 张图片，请选择完整图片组后重试。");
      return;
    }
    const selectedFiles = work.kind === "image" ? files : files.slice(0, 1);
    setSupplementingWorkId(work.id);
    try {
      await perform(
        `supplement-${work.id}`,
        () => api.uploadWorkSupplement(work.id, selectedFiles),
        work.kind === "image" ? "完整图片组已上传，正在重新处理" : "完整视频已上传，正在重新处理",
      );
    } finally {
      setSupplementingWorkId(null);
    }
  }
  async function ingestSelected() {
    if (!selected.size) return;
    const count = selected.size;
    if (await perform("ingest-selected", () => api.ingest([...selected]), `已创建 ${count} 个作品的入库任务`)) {
      setSelected(new Set());
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
        <div className="segmented" role="group" aria-label="知识库状态">
          <button type="button" aria-pressed={state === "pending"} className={state === "pending" ? "active" : ""} onClick={() => setState("pending")}>待处理 <em>{summary.candidate_count}</em></button>
          <button type="button" aria-pressed={state === "in_library"} className={state === "in_library" ? "active" : ""} onClick={() => setState("in_library")}>在库 <em>{summary.local_item_count}</em></button>
          <button type="button" aria-pressed={state === "supplement"} className={state === "supplement" ? "active" : ""} onClick={() => setState("supplement")}>待补件 <em>{summary.supplement_count ?? 0}</em></button>
          <button type="button" aria-pressed={state === "issues"} className={state === "issues" ? "active" : ""} onClick={() => setState("issues")}>异常 <em>{summary.issue_count}</em></button>
          <button type="button" aria-pressed={state === "archived"} className={state === "archived" ? "active" : ""} onClick={() => setState("archived")}>已归档 <em>{summary.archived_count}</em></button>
        </div>
        <label className="collection-filter"><span>本地分组</span><select value={collectionId ?? ""} onChange={(event) => setCollectionId(event.target.value ? Number(event.target.value) : null)}><option value="">全部分组</option>{collections.map((group) => <option key={group.id} value={group.id}>{group.title}</option>)}</select></label>
      </div>
      {state === "supplement" && (
        <section className="card supplement-consent">
          <div>
            <span className="kicker">本地素材补件</span>
            <strong>补齐画面后会自动重新处理并刷新知识</strong>
            <p>“需要补充”表示材料不足；“建议补充”表示现有总结可用，但还没有完整视频或图片组。</p>
          </div>
          <label>
            <input type="checkbox" checked={rightsAttested} onChange={(event) => setRightsAttested(event.target.checked)} />
            <span>我确认有权处理并上传这些本地素材</span>
          </label>
        </section>
      )}
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
              <div className="work-card-status">
                <span className="kicker">{work.kind === "image" ? "图文" : "视频"} · {libraryStateLabel(work.library_state)}</span>
                {state !== "supplement" && ["required", "failed"].includes(work.supplement_state || "") && (
                  <span className="work-supplement-indicator">需要补件</span>
                )}
              </div>
              <h3>{work.library_state === "in_library" && work.summary_state === "ready" ? <a className="work-title-link" href={`/works/${work.id}`} onClick={() => rememberSummaryReturn(work.id)}>{work.title}</a> : work.title}</h3>
              <p>{work.author_name || "未知作者"}{work.duration_seconds ? ` · ${Math.round(work.duration_seconds)} 秒` : ""}</p>
              {state === "supplement" && (
                <section className="supplement-details" aria-label="补件与材料状态">
                  <div className="supplement-status-row">
                    <span className={`supplement-state state-${work.supplement_state || "required"}`}>{supplementStateLabel(work.supplement_state, work.evidence_state)}</span>
                    <span className={`evidence-state ${work.evidence_state === "sufficient" ? "searchable" : "not-searchable"}`}>
                      {work.evidence_state === "sufficient" ? "现有材料可检索" : work.evidence_state === "unverified" ? "材料待验证 · 暂不检索" : "材料不足 · 不可检索"}
                    </span>
                  </div>
                  <p className="supplement-reason"><strong>材料缺口：</strong>{supplementReasonLabel(work.supplement_reason)}</p>
                  {trackReportRows(work.track_report, work.kind).length > 0 ? (
                    <dl className="track-report">
                      {trackReportRows(work.track_report, work.kind).map((track) => (
                        <div key={track.key}><dt>{track.label}</dt><dd>{track.value}</dd></div>
                      ))}
                    </dl>
                  ) : (
                    <p className="track-report-empty">材料处理报告暂未生成</p>
                  )}
                  {!rightsAttested && (
                    <label className="supplement-rights-attestation">
                      <input type="checkbox" checked={rightsAttested} onChange={(event) => setRightsAttested(event.target.checked)} />
                      <span>我确认有权处理并上传该本地素材</span>
                    </label>
                  )}
                  <label className={`supplement-upload ${!rightsAttested || supplementingWorkId != null || ["uploaded", "processing"].includes(work.supplement_state || "") ? "disabled" : ""}`}>
                    <span>
                      {supplementingWorkId === work.id
                        ? "正在上传…"
                        : work.supplement_state === "processing"
                          ? "正在重新处理"
                          : work.kind === "image"
                            ? "上传完整图片组"
                            : "上传完整视频"}
                    </span>
                    <input
                      type="file"
                      accept={work.kind === "image" ? "image/jpeg,image/png,image/webp,image/*" : "video/mp4,video/quicktime,video/x-matroska,video/webm,video/*"}
                      multiple={work.kind === "image"}
                      disabled={!rightsAttested || supplementingWorkId != null || ["uploaded", "processing"].includes(work.supplement_state || "")}
                      onChange={(event) => {
                        const files = Array.from(event.currentTarget.files || []);
                        event.currentTarget.value = "";
                        uploadSupplement(work, files);
                      }}
                    />
                  </label>
                </section>
              )}
              {work.summary_excerpt && <p className="summary-excerpt">{work.summary_excerpt}</p>}
              <p className="collection-tags">{work.collections.join(" · ")}</p>
              <div className="work-actions">
                {work.source_url && <a href={work.source_url} target="_blank" rel="noreferrer">查看公开原作品</a>}
                {state === "pending" && <button onClick={() => perform("ingest-one", () => api.retry(work.id), "已创建入库任务")}>开始入库</button>}
                {state === "issues" && <button onClick={() => perform("retry", () => api.retry(work.id), "已创建重试任务")}>重新处理</button>}
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
      {page.next_offset != null && (
        <button className="secondary load-more" disabled={loading} onClick={loadMore}>
          {loading ? "正在加载…" : "加载更多"}
        </button>
      )}
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
