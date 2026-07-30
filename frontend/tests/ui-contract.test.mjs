import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

const page = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
const api = await readFile(new URL("../lib/api.ts", import.meta.url), "utf8");
const styles = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");
const themeRegistry = await readFile(new URL("../themes/registry.ts", import.meta.url), "utf8");
const themeProvider = await readFile(new URL("../themes/ThemeProvider.tsx", import.meta.url), "utf8");
const themeBaseStyles = await readFile(new URL("../themes/styles/base.css", import.meta.url), "utf8");
const packageManifest = JSON.parse(
  await readFile(new URL("../package.json", import.meta.url), "utf8"),
);

const expectedThemes = [
  "classic-night",
  "dragonbone-biopunk",
  "aether-sky-city",
  "abyssal-runepunk",
  "paper-organizer",
  "ocean-dawn",
  "sunset-cloudsea",
  "aurora-snowfield",
  "eastern-mist",
  "moss-forest",
  "desert-observatory",
  "sakura-valley",
  "volcanic-forge",
];

test("website branding is TokBrain and import is the primary workspace", async () => {
  const layout = await readFile(new URL("../app/layout.tsx", import.meta.url), "utf8");
  assert.match(layout, /title: "TokBrain"/);
  assert.match(page, /type Tab = "import" \| "library" \| "chat" \| "settings"/);
  assert.match(page, /<strong>TokBrain<\/strong>/);
  assert.match(page, /tab === "import"/);
});

test("frontend development and production servers bind only to localhost", () => {
  assert.match(packageManifest.scripts.dev, /--hostname 127\.0\.0\.1/);
  assert.match(packageManifest.scripts.start, /--hostname 127\.0\.0\.1/);
});

test("theme registry exposes thirteen unique locally persisted themes", () => {
  const ids = [...themeRegistry.matchAll(/^\s+id: "([^"]+)",$/gm)].map((match) => match[1]);
  assert.deepEqual(ids, expectedThemes);
  assert.equal(new Set(ids).size, 13);
  assert.match(themeRegistry, /DEFAULT_THEME_ID: ThemeId = "classic-night"/);
  assert.match(themeProvider, /window\.localStorage\.setItem\(THEME_STORAGE_KEY, next\)/);
  assert.match(themeProvider, /document\.documentElement\.dataset\.theme = definition\.id/);
});

test("each theme has isolated CSS and a local background asset", async () => {
  for (const id of expectedThemes) {
    const css = await readFile(new URL(`../themes/styles/${id}.css`, import.meta.url), "utf8");
    assert.match(css, new RegExp(`:root\\[data-theme="${id}"\\]`));
    if (id !== "classic-night") await access(new URL(`../public/themes/${id}.webp`, import.meta.url));
  }
});

test("layout imports all theme styles directly for Windows non-ASCII paths", async () => {
  const layout = await readFile(new URL("../app/layout.tsx", import.meta.url), "utf8");
  assert.match(layout, /themes\/styles\/base\.css/);
  for (const id of expectedThemes) assert.match(layout, new RegExp(`themes/styles/${id}\\.css`));
});

test("risk notice uses semantic danger tokens in light and dark themes", () => {
  assert.match(page, /className="risk-notice" role="alert"/);
  assert.match(page, /仅当平台明确返回作者允许下载时才处理完整视频/);
  assert.match(page, /作者未允许下载 · 仅字幕\/音频/);
  for (const token of ["--danger-text", "--danger-border", "--danger-surface"]) {
    assert.match(styles, new RegExp(token));
    assert.match(themeBaseStyles, new RegExp(token));
  }
  assert.match(themeBaseStyles, /html\[data-theme-tone="dark"\]/);
  assert.match(styles, /\.risk-notice\s*\{[^}]*var\(--danger-border\)[^}]*var\(--danger-surface\)/s);
  assert.match(styles, /\.app-shell>main\s*\{\s*min-width:0;\s*width:100%;/);
  assert.match(styles, /\.risk-notice\s*\{[^}]*overflow-wrap:anywhere/s);
});

test("batch input, progress, three workers, cancellation, and confirmation are visible", () => {
  assert.match(page, /className="import-textarea"/);
  assert.match(page, /可直接粘贴整段分享文案或每行一个链接/);
  assert.match(page, /className="batch-progress"/);
  assert.match(page, /className="batch-stats"/);
  assert.match(page, /className="worker-grid"/);
  assert.match(page, /中断解析/);
  assert.match(page, /正在安全停止/);
  assert.match(page, /确认加入待入库/);
  assert.match(page, /confirmedWorkIds/);
  assert.match(page, /confirmedIds\.push\(\.\.\.result\.work_ids\)/);
  assert.match(page, /api\.ingest\(workIds\)/);
  assert.match(page, /入库（\$\{confirmedWorkIds\.length\}）/);
  assert.match(page, /已勾选待确认/);
  assert.match(page, /已确认待入库/);
  assert.match(page, /新勾选作品需先确认；已确认作品可直接入库，两组操作互不影响/);
  assert.match(page, /samePreviewWork/);
  assert.match(page, /selectedVisibleIds/);
  assert.match(page, /确认加入待入库（\$\{selectedVisibleIds\.length\}）/);
  assert.match(page, /加入收藏夹/);
  assert.match(page, /manual-import/);
});

test("batch polling is bounded and stops at terminal states", () => {
  assert.match(page, /TERMINAL_BATCH_STATES/);
  assert.match(page, /window\.setTimeout\(async \(\) =>/);
  assert.match(page, /api\.importBatch\(batch\.id\)/);
  assert.doesNotMatch(page, /setInterval\(/);
});

test("starting a new preview retains prior confirmable results and clears only successful input", () => {
  assert.match(page, /const \[retainedBatches, setRetainedBatches\]/);
  assert.match(page, /RETAINED_IMPORT_STATUSES\.has\(item\.status\)/);
  assert.match(page, /setRetainedBatches\(\(current\) =>/);
  assert.match(page, /submittedImportTexts/);
  assert.match(page, /fullySuccessful/);
  assert.match(page, /setImportText\(\(current\) => current\.trim\(\) === submitted\.trim\(\) \? "" : current\)/);
  assert.doesNotMatch(
    page,
    /if \(tab === "import"\) return;\s*setRetainedBatches\(\[\]\)/,
  );
});

test("duplicate links are removed automatically while unique works continue previewing", () => {
  assert.match(api, /queued_count: number/);
  assert.match(api, /duplicate_count: number/);
  assert.match(page, /const duplicateOnly =/);
  assert.match(page, /已去掉 \$\{created\.duplicate_count\} 条重复链接/);
  assert.match(page, /没有新的作品需要预检/);
  assert.match(page, /\$\{created\.queued_count\} 个不同作品已进入预检/);
  assert.match(page, /Number\(next\.progress\.duplicates \|\| 0\)/);
  assert.match(
    page,
    /!\(item\.status === "duplicate" && item\.error_code === "duplicate_input"\)/,
  );
});

test("result rows expose stable errors, upload fallback, and long-text wrapping", () => {
  assert.match(page, /item\.error_code && <code>/);
  assert.match(page, /item\.error_message/);
  assert.match(page, /item\.error_code === "duplicate_input" \? "预检重复" : "已在知识库"/);
  assert.match(page, /上传本地补件/);
  assert.match(page, /删除预检结果/);
  assert.match(page, /removePreviewItem/);
  assert.match(api, /removeImportItem/);
  assert.match(page, /\.mp4,.mov,.mkv,.webm/);
  assert.match(styles, /\.result-body h3\s*\{[^}]*overflow-wrap:anywhere/s);
  assert.match(styles, /\.result-body p\s*\{[^}]*overflow-wrap:anywhere/s);
});

test("small screens collapse worker, result, and safety layouts", () => {
  assert.match(styles, /@media\(max-width:760px\)[\s\S]*\.batch-stats,.worker-grid,.safety-grid/);
  assert.match(styles, /@media\(max-width:460px\)[\s\S]*\.import-result/);
});

test("v4 API client exposes import, local assets, and unified jobs", () => {
  assert.match(api, /createImportBatch/);
  assert.match(api, /\/api\/import-batches/);
  assert.match(api, /cancelImportBatch/);
  assert.match(api, /confirmImportBatch/);
  assert.match(api, /\/api\/import-items\/\$\{itemId\}\/assets/);
  assert.match(
    api,
    /jobs\(\): Promise<Contract\.Job\[\]> \{[\s\S]*?this\.request\("\/api\/jobs"\)/,
  );
  assert.match(api, /cancelJob/);
  assert.match(api, /createCollection/);
  assert.match(api, /addWorksToCollection/);
});

test("removed collection automation and account entry points are absent", () => {
  assert.doesNotMatch(page, /refreshCollections/);
  assert.doesNotMatch(page, /api\.sync/);
  assert.doesNotMatch(page, /继续初始化/);
  assert.doesNotMatch(page, /完整核对/);
  assert.doesNotMatch(api, /\/api\/auth\//);
  assert.doesNotMatch(api, /adapter-health/);
});

test("settings show fixed read-only safety values and local health", () => {
  assert.match(page, /固定安全策略/);
  assert.match(page, /单作品访问护栏/);
  assert.match(page, /可选解析 Cookie/);
  assert.doesNotMatch(page, /settings\.import_worker_count/);
  assert.doesNotMatch(page, /settings\.import_network_concurrency/);
  assert.doesNotMatch(page, /settings\.import_cooldown_min_seconds/);
  assert.match(page, /saveF2Cookie/);
  assert.match(page, /保存 Cookie/);
  assert.match(page, /已保存并生效/);
  assert.match(page, /className="card principles-card"/);
  assert.match(page, /className="health-progress"/);
  assert.match(page, /api\.healthProbe/);
  assert.match(page, /敏感残留尚未清理/);
  assert.doesNotMatch(page, /name="import_worker_count"/);
  assert.doesNotMatch(page, /name="import_daily_limit"/);
});

test("settings expose the editable summary prompt and default reset", () => {
  assert.match(page, /视频 AI 总结提示词/);
  assert.match(page, /settings\.summary_prompt/);
  assert.match(page, /settings\.default_summary_prompt/);
  assert.match(page, /一键恢复默认提示词/);
  assert.match(page, /saveSummaryPrompt/);
  assert.match(page, /视频\/图文总结模型/);
  assert.match(page, /快速回答模型/);
  assert.match(page, /深度回答模型/);
  assert.match(page, /settings\.processing_model_options\.map/);
  assert.match(page, /settings\.chat_model_options\.map/);
  assert.match(page, /qwen-math-turbo/);
  assert.match(page, /固定专用模型/);
  assert.match(page, /刷新官方账单/);
  assert.match(page, /账单查询 AccessKey ID/);
  assert.match(page, /saveBillingCredentials/);
  assert.match(page, /账单查询凭据已在本机后台加密保存/);
  assert.match(page, /本月账单/);
  assert.match(page, /usage\?\.official_billed_cny/);
  assert.match(page, /clearAllKeys/);
  assert.match(page, /删除全部 API Key 与 AccessKey/);
  assert.match(api, /\/api\/settings\/secrets/);
});

test("import overview and processing queue keep all active tasks visible", () => {
  assert.match(page, /className="import-overview"/);
  for (const label of ["今日处理", "今日 AI 用量", "已入库", "待入库"]) {
    assert.match(page, new RegExp(label));
  }
  assert.match(page, /const activeJobs = jobs/);
  assert.match(page, /activeJobs\.map/);
  assert.match(page, /队列第/);
});

test("pending and in-library works can share local collections", () => {
  assert.match(page, /新建收藏夹/);
  assert.match(page, /选择目标收藏夹/);
  assert.match(page, /加入收藏夹/);
  assert.match(page, /state === "pending" \|\| state === "in_library"/);
  assert.match(page, /收藏夹关系会在作品完成 AI 处理后继续保留/);
  assert.match(page, /批量开始入库/);
  assert.match(page, /设置总结提示词/);
  assert.match(page, /收藏夹专属 AI 规则/);
  assert.match(page, /activeCollection\.summary_prompt \|\| globalSummaryPrompt/);
  assert.match(page, /已作为可编辑正文载入/);
  assert.doesNotMatch(page, /placeholder=\{globalSummaryPrompt/);
  assert.match(api, /updateCollectionSummaryPrompt/);
  assert.match(api, /\/api\/library\/ingest\/jobs/);
});

test("sidebar ambient scenes exist for non-classic themes", () => {
  assert.match(page, /className="theme-ambient"/);
  assert.match(themeBaseStyles, /data-theme="classic-night".*theme-ambient\s*\{\s*display:none/s);
  for (const theme of ["sakura-valley", "abyssal-runepunk", "sunset-cloudsea", "moss-forest", "volcanic-forge"]) {
    assert.match(themeBaseStyles, new RegExp(`data-theme="${theme}"`));
  }
  assert.match(themeBaseStyles, /ambient-petal/);
  assert.match(themeBaseStyles, /sunset-cloudsea[^}]*--ambient-symbol:"☁"/s);
  assert.match(themeBaseStyles, /sunset-cloudsea[^}]*theme-ambient i[^}]*#5cb8ff/s);
  assert.match(themeBaseStyles, /volcanic-forge[^}]*--ambient-symbol:"火"/s);
});

test("theme picker and background preferences remain local", () => {
  assert.match(page, /role="radiogroup" aria-label="界面主题"/);
  assert.match(page, /className={`theme-card/);
  assert.match(page, /className="theme-preview"/);
  assert.match(page, /className="background-intensity"/);
  assert.match(page, /aria-label="背景显现度"/);
  assert.match(themeProvider, /BACKGROUND_INTENSITY_STORAGE_KEY/);
  assert.match(themeProvider, /setBackgroundIntensity/);
});

test("chat appends user input before awaiting streamed response", () => {
  const clearIndex = page.indexOf('setQuestion("")');
  const appendIndex = page.indexOf("setMessages((current) => [");
  const requestIndex = page.indexOf("await api.askStream(text, history, chatMode");
  assert.ok(clearIndex > 0 && appendIndex > 0 && requestIndex > 0);
  assert.ok(clearIndex < requestIndex && appendIndex < requestIndex);
});

test("chat retains markdown, math, answer formats, and fast/deep modes", async () => {
  const markdown = await readFile(new URL("../components/MarkdownContent.tsx", import.meta.url), "utf8");
  assert.match(page, /阅读排版/);
  assert.match(page, /快速回答/);
  assert.match(page, /深度回答/);
  assert.match(markdown, /ReactMarkdown/);
  assert.match(markdown, /remarkMath/);
  assert.match(markdown, /rehypeKatex/);
  assert.match(markdown, /format === "markdown" \? <div className="markdown-preview"><MarkdownContent content=\{content\}/);
  assert.match(page, /<AnswerBlock/);
  assert.match(api, /askStream/);
});

test("import helper copy omits internal worker and cooldown details", () => {
  assert.doesNotMatch(page, /3 个 worker · 平台网络严格串行/);
  assert.doesNotMatch(page, /每条链接完成后随机冷却 4–8 秒/);
});

test("toast colors use semantic tokens across light and dark themes", () => {
  for (const token of ["--toast-bg", "--toast-text", "--toast-success-border", "--toast-error-border"]) {
    assert.match(styles, new RegExp(token));
    assert.match(themeBaseStyles, new RegExp(token));
  }
  assert.match(styles, /\.toast\s*\{[^}]*background:var\(--toast-bg\)[^}]*color:var\(--toast-text\)/s);
  assert.match(themeBaseStyles, /html\[data-theme-tone="dark"\][^{]*\{[^}]*--toast-bg:/s);
});

test("LaTeX delimiters normalize without changing code fences", async () => {
  const { normalizeMathMarkdown, markdownToPlainText } = await import("../lib/markdown.ts");
  const source = "公式：\\\\[ \\int_a^b f(x) \\mathrm{d}x \\\\]，其中 \\\\(a=0\\\\)。";
  const normalized = normalizeMathMarkdown(source);
  assert.match(normalized, /\$\$\n\\int_a\^b f\(x\) \\mathrm\{d\}x\n\$\$/);
  assert.match(normalized, /\$a=0\$/);
  assert.equal(normalizeMathMarkdown("```latex\n\\\\[x\\\\]\n```"), "```latex\n\\\\[x\\\\]\n```");
  assert.match(markdownToPlainText(source), /\\int_a\^b/);
});

test("library keeps four states, summaries, lazy covers, and Obsidian export", async () => {
  const obsidian = await readFile(new URL("../lib/obsidian.ts", import.meta.url), "utf8");
  for (const state of ["pending", "in_library", "issues", "archived"]) {
    assert.match(page, new RegExp(`state === "${state}"`));
  }
  assert.match(page, /补齐\/更新总结/);
  assert.match(page, /导出到 Obsidian/);
  assert.match(page, /设置图片统一存放位置/);
  assert.match(page, /loading="lazy" decoding="async"/);
  assert.match(page, /value\?\.startsWith\("\/api\/"\) \? `\$\{API_BASE\}\$\{value\}` : null/);
  assert.match(page, /className="technical-details" aria-label="技术详情"/);
  assert.match(page, /确认永久删除这个作品、总结、索引与本地资产/);
  assert.match(obsidian, /showDirectoryPicker/);
  assert.match(obsidian, /text\/markdown;charset=utf-8/);
  assert.match(obsidian, /chooseObsidianImageDirectory/);
});

test("success notices auto-dismiss while errors stay until closed", () => {
  assert.match(page, /window\.setTimeout\(\(\) => setNotice\(""\), 5000\)/);
  assert.doesNotMatch(page, /setTimeout\(\(\) => setError\(""\)/);
});

test("cancelling a native folder picker is not reported as an application error", () => {
  assert.match(page, /import \{ isUserCancelled \} from "\.\.\/lib\/errors"/);
  assert.match(page, /if \(isUserCancelled\(value\)\) return false/);
  assert.match(page, /if \(isUserCancelled\(value\)\) return;/);
});

test("native folder picker abort errors are detected by name and browser message", async () => {
  const { isUserCancelled } = await import("../lib/errors.ts");
  const namedAbort = new Error("用户取消了目录选择");
  namedAbort.name = "AbortError";
  assert.equal(isUserCancelled(namedAbort), true);
  assert.equal(
    isUserCancelled(
      new Error(
        "Failed to execute 'showDirectoryPicker' on 'Window': The user aborted a request.",
      ),
    ),
    true,
  );
  assert.equal(isUserCancelled(new Error("目录没有写入权限")), false);
});

test("production page and summary route compile around current navigation", async () => {
  const summaryPage = await readFile(new URL("../app/works/[id]/page.tsx", import.meta.url), "utf8");
  assert.match(summaryPage, /\?tab=library&state=in_library&view=works/);
  assert.match(summaryPage, /summary-content/);
  assert.match(summaryPage, /永久删除/);
  assert.match(summaryPage, /api\.remove\(workId\)/);
});
