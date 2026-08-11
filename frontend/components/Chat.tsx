"use client";

import { FormEvent, memo } from "react";

import type { ChatSource, RuntimeSettings } from "../lib/api";
import { AnswerBlock } from "./MarkdownContent";

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  sources?: ChatSource[];
  status?: "pending" | "error";
};

export const Chat = memo(function Chat({
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
  messages: ChatMessage[];
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
        <div className="mode-switch" role="group" aria-label="回答模式">
          <button type="button" aria-pressed={mode === "fast"} className={mode === "fast" ? "active" : ""} onClick={() => setMode("fast")}>快速回答</button>
          <button type="button" aria-pressed={mode === "deep"} className={mode === "deep" ? "active" : ""} onClick={() => setMode("deep")}>深度回答</button>
        </div>
        <small className="chat-format-hint">回答生成后可切换阅读排版、Markdown 和纯文本。</small>
      </div>
      <div className="conversation">
        <div className="message-list" aria-live="polite" aria-busy={busy}>
          {!messages.length && <div className="empty compact"><strong>问一个具体问题</strong><p>例如：“知识库中关于颈椎拉伸有哪些步骤？”</p></div>}
          {messages.map((message) => (
            <article role={message.status === "error" ? "alert" : undefined} className={`chat-message ${message.role} ${message.status || ""}`} key={message.id}>
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
            aria-label="向本地知识库提问"
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
});
