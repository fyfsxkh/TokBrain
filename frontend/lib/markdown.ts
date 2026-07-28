import { toString } from "mdast-util-to-string";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import remarkParse from "remark-parse";
import { unified } from "unified";

type MdNode = { type?: string; value?: string; children?: MdNode[] };

const CODE_FENCE = /(```[\s\S]*?```|~~~[\s\S]*?~~~)/g;

/**
 * remark-math understands dollar delimiters, while many chat models emit the
 * equivalent LaTeX delimiters `\\(...\\)` and `\\[...\\]`. Normalize those
 * variants before Markdown parsing, but leave fenced code examples untouched.
 */
export function normalizeMathMarkdown(markdown: string): string {
  return markdown
    .split(CODE_FENCE)
    .map((part, index) => {
      if (index % 2 === 1) return part;
      return part
        .replace(/\\{1,2}\[\s*([\s\S]*?)\s*\\{1,2}\]/g, (_match, body: string) =>
          `\n\n$$\n${body.trim()}\n$$\n\n`,
        )
        .replace(/\\{1,2}\(\s*([\s\S]*?)\s*\\{1,2}\)/g, (_match, body: string) =>
          `$${body.trim()}$`,
        );
    })
    .join("");
}

function blockText(node: MdNode, depth = 0): string {
  if (node.type === "html") return "";
  if (node.type === "code" || node.type === "math") return node.value || "";
  if (node.type === "table") {
    return (node.children || [])
      .map((row) => (row.children || []).map((cell) => toString(cell)).join("\t"))
      .join("\n");
  }
  if (node.type === "list") {
    return (node.children || [])
      .map((item, index) => `${"  ".repeat(depth)}${index + 1}. ${toString(item)}`)
      .join("\n");
  }
  if (node.type === "blockquote") {
    return (node.children || []).map((child) => blockText(child, depth + 1)).filter(Boolean).join("\n");
  }
  if (["heading", "paragraph", "listItem"].includes(node.type || "")) return toString(node);
  if (node.type === "thematicBreak") return "——";
  return (node.children || []).map((child) => blockText(child, depth)).filter(Boolean).join("\n\n");
}

export function markdownToPlainText(markdown: string): string {
  const tree = unified()
    .use(remarkParse)
    .use(remarkGfm)
    .use(remarkMath)
    .parse(normalizeMathMarkdown(markdown)) as MdNode;
  return blockText(tree).replace(/\n{3,}/g, "\n\n").trim();
}

export const GENERATED_START = "<!-- shiguang:generated:start -->";
export const GENERATED_END = "<!-- shiguang:generated:end -->";

export function mergeGeneratedMarkdown(existing: string, generated: string): string | null {
  const oldStart = existing.indexOf(GENERATED_START);
  const oldEnd = existing.indexOf(GENERATED_END);
  const newStart = generated.indexOf(GENERATED_START);
  const newEnd = generated.indexOf(GENERATED_END);
  if (oldStart < 0 || oldEnd < oldStart || newStart < 0 || newEnd < newStart) return null;
  const replacement = generated.slice(newStart, newEnd + GENERATED_END.length);
  return `${existing.slice(0, oldStart)}${replacement}${existing.slice(oldEnd + GENERATED_END.length)}`;
}
