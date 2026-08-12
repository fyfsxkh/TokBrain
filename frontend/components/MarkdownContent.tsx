"use client";

import { memo, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";

import { markdownToPlainText, normalizeMathMarkdown } from "../lib/markdown";

export type AnswerFormat = "rich" | "markdown" | "plain";

export const MarkdownContent = memo(function MarkdownContent({ content }: { content: string }) {
  const normalized = useMemo(() => normalizeMathMarkdown(content), [content]);
  return (
    <div className="markdown-content">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex]}
        components={{
          a: ({ ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
        }}
      >
        {normalized}
      </ReactMarkdown>
    </div>
  );
});

export const AnswerBlock = memo(function AnswerBlock({ content, initialFormat }: { content: string; initialFormat: AnswerFormat }) {
  const [format, setFormat] = useState<AnswerFormat>(initialFormat);
  const [copied, setCopied] = useState(false);
  const richRef = useRef<HTMLDivElement>(null);
  const plain = useMemo(() => markdownToPlainText(content), [content]);

  async function copyCurrent() {
    if (format === "rich" && richRef.current && navigator.clipboard.write && typeof ClipboardItem !== "undefined") {
      const html = richRef.current.innerHTML;
      await navigator.clipboard.write([
        new ClipboardItem({
          "text/html": new Blob([html], { type: "text/html" }),
          "text/plain": new Blob([plain], { type: "text/plain" }),
        }),
      ]);
    } else {
      await navigator.clipboard.writeText(format === "markdown" ? content : plain);
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  }

  return (
    <div className="answer-block">
      <div className="answer-toolbar">
        <div className="format-switch" role="group" aria-label="回答显示格式">
          {(["rich", "markdown", "plain"] as AnswerFormat[]).map((value) => (
            <button type="button" key={value} aria-pressed={format === value} className={format === value ? "active" : ""} onClick={() => setFormat(value)}>
              {value === "rich" ? "阅读排版" : value === "markdown" ? "Markdown" : "纯文本"}
            </button>
          ))}
        </div>
        <button type="button" className="copy-answer" onClick={copyCurrent}>{copied ? "已复制" : "复制当前格式"}</button>
      </div>
      {format === "rich" ? <div ref={richRef}><MarkdownContent content={content} /></div> :
        format === "markdown" ? <pre className="markdown-source"><code>{content}</code></pre> :
          <div className="plain-answer">{plain}</div>}
    </div>
  );
});
