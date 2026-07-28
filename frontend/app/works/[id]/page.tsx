"use client";

/* eslint-disable @next/next/no-img-element -- processed assets are served by the local API. */

import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { MarkdownContent } from "../../../components/MarkdownContent";
import { API_BASE, api, WorkSummaryDetail } from "../../../lib/api";

function localAssetUrl(value?: string | null) {
  return value?.startsWith("/api/") ? `${API_BASE}${value}` : null;
}

export default function WorkSummaryPage() {
  const params = useParams<{ id: string }>();
  const workId = Number(params.id);
  const [detail, setDetail] = useState<WorkSummaryDetail | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!Number.isFinite(workId)) return;
    api.workSummary(workId)
      .then(setDetail)
      .catch((reason) =>
        setError(reason instanceof Error ? reason.message : "读取总结失败"),
      );
  }, [workId]);

  const assetMap = useMemo(
    () => new Map(detail?.assets.map((item) => [item.name, item.url]) || []),
    [detail],
  );
  const goBack = () =>
    window.location.assign("/?tab=library&state=in_library&view=works");

  if (error) {
    return (
      <main className="summary-page">
        <button className="back-link" onClick={goBack}>← 返回知识库</button>
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
      <button className="back-link" onClick={goBack}>← 返回知识库</button>
      <header className="summary-hero">
        {cover && <img src={cover} alt="" />}
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
            <span>
              总结时间：
              {detail.summary.generated_at
                ? new Date(detail.summary.generated_at).toLocaleString("zh-CN", {
                    hour12: false,
                  })
                : "—"}
            </span>
          </div>
        </div>
      </header>
      <div className="summary-tags">
        {detail.summary.tags.map((tag) => <span key={tag}>#{tag}</span>)}
      </div>
      <section className="summary-content">
        {detail.summary.sections.map((section, index) => {
          const asset = detail.assets[index];
          const assetUrl = asset && assetMap.get(asset.name);
          return (
            <article className="summary-section" key={`${section.kind}-${index}`}>
              <h2>{section.title}</h2>
              <MarkdownContent content={section.body} />
              {assetUrl && (
                <img
                  className="section-image"
                  src={`${API_BASE}${assetUrl}`}
                  alt={`${detail.work.title}相关图片`}
                />
              )}
            </article>
          );
        })}
        {detail.assets.length > detail.summary.sections.length && (
          <article className="summary-section">
            <h2>相关图片</h2>
            <div className="summary-gallery">
              {detail.assets.slice(detail.summary.sections.length).map((asset) => (
                <img
                  key={asset.name}
                  src={`${API_BASE}${asset.url}`}
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
