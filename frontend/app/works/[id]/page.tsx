"use client";

/* eslint-disable @next/next/no-img-element -- processed assets are served by the local API. */

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { MarkdownContent } from "../../../components/MarkdownContent";
import { api, WorkSummaryDetail } from "../../../lib/api";
import { localAssetUrl, resolvedAssetUrl } from "../../../lib/assets";
import { readLibraryReturnContext } from "../../../lib/libraryReturn";

function localSummaryTime(value: string) {
  const timestamp = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`;
  return new Date(timestamp).toLocaleString("zh-CN", { hour12: false });
}

export default function WorkSummaryPage() {
  const params = useParams<{ id: string }>();
  const workId = Number(params.id);
  const [loadedDetail, setLoadedDetail] = useState<WorkSummaryDetail | null>(null);
  const [loadError, setLoadError] = useState<{ workId: number; message: string } | null>(null);
  const [deleting, setDeleting] = useState(false);
  const invalidWorkId = !Number.isInteger(workId) || workId <= 0;
  const detail = loadedDetail?.work.id === workId ? loadedDetail : null;
  const error = invalidWorkId
    ? "作品编号无效"
    : loadError?.workId === workId ? loadError.message : "";

  useEffect(() => {
    if (!Number.isInteger(workId) || workId <= 0) return;
    let active = true;
    api.workSummary(workId)
      .then((value) => { if (active) setLoadedDetail(value); })
      .catch((reason) => {
        if (active) {
          setLoadError({
            workId,
            message: reason instanceof Error ? reason.message : "读取总结失败",
          });
        }
      });
    return () => { active = false; };
  }, [workId]);
  const goBack = (forceReload = false) => {
    const context = readLibraryReturnContext();
    if (!forceReload && context?.workId === workId && window.history.length > 1) {
      window.history.back();
      return;
    }
    window.location.assign(
      context?.workId === workId
        ? context.url
        : "/?tab=library&state=in_library&view=works",
    );
  };
  const removeWork = async () => {
    if (!window.confirm("确认永久删除这个作品、总结、索引与本地资产？此操作不可恢复。")) {
      return;
    }
    setDeleting(true);
    setLoadError(null);
    try {
      await api.remove(workId);
      goBack(true);
    } catch (reason) {
      setLoadError({
        workId,
        message: reason instanceof Error ? reason.message : "删除作品失败",
      });
      setDeleting(false);
    }
  };

  if (error) {
    return (
      <main className="summary-page">
        <button className="back-link" onClick={() => goBack()}>← 返回知识库</button>
        <div className="empty">
          <strong>暂时无法查看总结</strong>
          <p>{error}</p>
        </div>
      </main>
    );
  }
  if (!detail) {
    return <main className="summary-page"><p className="muted">正在读取作品内容…</p></main>;
  }

  const cover = localAssetUrl(detail.work.cover_url);
  return (
    <main className="summary-page">
      <button className="back-link" onClick={() => goBack()}>← 返回知识库</button>
      <header className="summary-hero">
        {cover && <img src={cover} alt={`${detail.work.title}封面`} />}
        <div>
          <h1>{detail.work.title}</h1>
          <p>
            {detail.work.author_name || "未知作者"}
            {detail.work.collections.length > 0 && ` · ${detail.work.collections.join(" · ")}`}
          </p>
          <blockquote>{detail.summary.one_sentence}</blockquote>
          <div className="summary-actions">
            {detail.work.source_url && (
              <a href={detail.work.source_url} target="_blank" rel="noreferrer">
                查看公开原作品
              </a>
            )}
            <button className="danger" disabled={deleting} onClick={removeWork}>
              {deleting ? "删除中…" : "永久删除"}
            </button>
            <span>
              总结时间：
              {detail.summary.generated_at
                ? localSummaryTime(detail.summary.generated_at)
                : "—"}
            </span>
          </div>
        </div>
      </header>
      <div className="summary-tags">
        {detail.summary.tags.map((tag) => <span key={tag}>#{tag}</span>)}
      </div>
      <section className="summary-content">
        {detail.summary.sections.map((section, index) => (
            <article className="summary-section" key={`${section.kind}-${index}`}>
              <h2>{section.title}</h2>
              <MarkdownContent content={section.body} />
            </article>
        ))}
        {detail.assets.length > 0 && (
          <article className="summary-section">
            <h2>相关图片</h2>
            <div className="summary-gallery">
              {detail.assets.map((asset) => (
                <img
                  key={asset.name}
                  src={resolvedAssetUrl(asset.url)}
                  alt={`${detail.work.title}相关图片`}
                />
              ))}
            </div>
          </article>
        )}
      </section>
    </main>
  );
}
