import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

import {
  applyChatStreamEvent,
  createChatStreamState,
  visibleChatStreamContent,
} from "../lib/chatStream.ts";
import { removeConfirmedImportItems } from "../lib/importBatches.ts";
import {
  IMPORT_BATCH_STORAGE_KEY,
  PACKAGE_IMPORT_STORAGE_KEY,
  rememberPackageBatch,
  storedPackageBatches,
} from "../lib/importBatchStorage.ts";
import {
  parseChatStreamEvent,
  parseImportBatch,
  parseJobs,
  parseProbe,
  parseRuntimeSettings,
} from "../lib/contractGuards.ts";
import { didAnyJobReachTerminal, operationIncludesJob } from "../lib/jobPolling.ts";
import { mergeWorksPage } from "../lib/libraryPagination.ts";

const pageShell = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
const libraryComponent = await readFile(new URL("../components/Library.tsx", import.meta.url), "utf8");
const settingsComponent = await readFile(new URL("../components/Settings.tsx", import.meta.url), "utf8");
const page = [pageShell, libraryComponent, settingsComponent].join("\n");
const chatComponent = await readFile(new URL("../components/Chat.tsx", import.meta.url), "utf8");
const api = await readFile(new URL("../lib/api.ts", import.meta.url), "utf8");
const contracts = await readFile(new URL("../lib/contracts.ts", import.meta.url), "utf8");
const libraryReturn = await readFile(new URL("../lib/libraryReturn.ts", import.meta.url), "utf8");
const batchStorage = await readFile(new URL("../lib/importBatchStorage.ts", import.meta.url), "utf8");
const assets = await readFile(new URL("../lib/assets.ts", import.meta.url), "utf8");
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

test("page delegates library and settings UI to dependency-safe components", () => {
  assert.match(pageShell, /import \{ Library \} from "\.\.\/components\/Library"/);
  assert.match(pageShell, /import \{ Settings, ThemePicker \} from "\.\.\/components\/Settings"/);
  assert.doesNotMatch(pageShell, /function Library\(/);
  assert.doesNotMatch(pageShell, /function Settings\(/);
  assert.doesNotMatch(pageShell, /function ThemePicker\(/);
  assert.match(libraryComponent, /export function Library\(/);
  assert.match(settingsComponent, /export function Settings\(/);
  assert.match(settingsComponent, /export function ThemePicker\(/);
  assert.doesNotMatch(libraryComponent, /from "\.\.\/app\/page"/);
  assert.doesNotMatch(settingsComponent, /from "\.\.\/app\/page"/);
});

test("frontend development and production servers bind only to localhost", () => {
  assert.match(packageManifest.scripts.dev, /--hostname 127\.0\.0\.1/);
  assert.match(packageManifest.scripts.start, /--hostname 127\.0\.0\.1/);
  assert.equal(
    packageManifest.scripts.typecheck,
    "tsc --noEmit --incremental false --noUnusedLocals --noUnusedParameters",
  );
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
  assert.match(page, /每行粘贴一个抖音作品链接/);
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

test("Douyin links are restored to manual preview and confirmation", () => {
  assert.match(page, /添加抖音作品链接/);
  assert.match(page, /预检后由您确认/);
  assert.match(page, /只进行预检，不会自动入库/);
  assert.match(page, /开始预检/);
  assert.match(page, /createImportBatch\(\{ text: submittedText \}\)/);
  assert.doesNotMatch(page, /setLinkDefaultCollectionId/);
  assert.doesNotMatch(page, /auto_confirm: true/);
  assert.doesNotMatch(page, /start_processing: linkStartProcessing/);
  assert.doesNotMatch(contracts, /automation\?: \{/);
  assert.match(contracts, /export type ImportBatchCreateRequest = \{\s*text: string;\s*\}/);
});

test("folder and ZIP packages upload, recover, and enter the normal confirm flow", () => {
  assert.match(page, /导入外部视频数据包/);
  assert.match(page, /选择文件夹/);
  assert.match(page, /选择 ZIP/);
  assert.match(page, /douyin_videos\.db/);
  assert.match(page, /Promise\.all\(\[worker\(\), worker\(\)\]\)/);
  assert.match(page, /api\.createPackageImportBatch/);
  assert.match(page, /api\.uploadPackageImportFile/);
  assert.match(page, /api\.analyzePackageImportBatch/);
  assert.match(page, /storedPackageBatches/);
  assert.match(page, /切换页面或关闭浏览器不会中断后端检测/);
  assert.match(api, /\/api\/package-import-batches/);
  assert.match(contracts, /"package_upload"/);
  assert.match(page, /const \[rightsAttested, setRightsAttested\] = useState\(false\)/);
  assert.match(page, /setRightsAttested=\{changeRightsAttestation\}/);
  assert.match(page, /rightsAttested=\{rightsAttested\}/);
  assert.match(page, /rights_attested: true/);
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
  assert.match(contracts, /queued_count: number/);
  assert.match(contracts, /duplicate_count: number/);
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

test("API client exposes import, local assets, and unified jobs", () => {
  assert.match(api, /createImportBatch/);
  assert.match(api, /\/api\/import-batches/);
  assert.match(api, /cancelImportBatch/);
  assert.match(api, /confirmImportBatch/);
  assert.match(api, /\/api\/import-items\/\$\{itemId\}\/assets/);
  assert.match(
    api,
    /jobs\(\): Promise<Contract\.Job\[\]> \{[\s\S]*?this\.request<unknown>\("\/api\/jobs"\)/,
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
  assert.match(page, /今日处理[\s\S]{0,120}daily_works_used/);
  assert.doesNotMatch(page, /今日处理[\s\S]{0,120}daily_links_used/);
  assert.match(page, /今日入库剩余[\s\S]{0,120}dailyIngestRemaining/);
  assert.match(page, /dailyIngestRemaining[\s\S]{0,180}daily_works_used/);
  assert.doesNotMatch(page, /今日剩余[\s\S]{0,120}batch\.remaining_daily/);
  assert.match(page, /const activeJobs = jobs/);
  assert.match(page, /activeJobs\.map/);
  assert.match(page, /队列第/);
});

test("local video import supports bounded two-worker uploads and refresh recovery", () => {
  assert.match(page, /导入已经下载好的视频/);
  assert.match(page, /const LOCAL_VIDEO_LIMIT = 10/);
  assert.match(page, /multiple\s+accept="\.mp4,\.mov,\.mkv,\.webm/);
  assert.match(page, /await Promise\.all\(\[worker\(\), worker\(\)\]\)/);
  assert.match(page, /rights_attested: true/);
  assert.match(page, /rememberLocalImportBatch\(created\.id\)/);
  assert.match(batchStorage, /LOCAL_IMPORT_BATCH_STORAGE_KEY/);
  assert.match(page, /已恢复刷新前的导入批次/);
  assert.match(page, /owner\.source_type === "local_upload"/);
  assert.match(api, /\/api\/local-import-batches/);
  assert.match(api, /\/api\/import-items\/\$\{itemId\}/);
});

test("local video consent and preflight survive navigation within the browser tab", () => {
  assert.match(batchStorage, /LOCAL_IMPORT_RIGHTS_SESSION_KEY/);
  assert.match(batchStorage, /window\.sessionStorage\.getItem\(LOCAL_IMPORT_RIGHTS_SESSION_KEY\)/);
  assert.match(batchStorage, /window\.sessionStorage\.setItem\(LOCAL_IMPORT_RIGHTS_SESSION_KEY, "true"\)/);
  assert.match(page, /<ImportWorkspace\s+hidden=\{tab !== "import"\}/);
  assert.match(page, /<section className="import-workspace stack" hidden=\{hidden\}>/);
});

test("settings manage one-time external import tokens", () => {
  assert.match(page, /外部导入令牌/);
  assert.match(page, /明文仅在生成或轮换后显示一次/);
  assert.match(page, /createIntegrationToken/);
  assert.match(page, /revokeIntegrationToken/);
  assert.match(api, /\/api\/settings\/integration-token/);
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

test("chat stream keeps stages separate, accumulates deltas, and requires done", () => {
  let state = createChatStreamState();
  state = applyChatStreamEvent(state, { type: "stage", stage: "search", message: "正在检索" });
  assert.equal(visibleChatStreamContent(state), "正在检索");
  state = applyChatStreamEvent(state, { type: "sources", sources: [{ work_id: 7, title: "来源", collection: null, external_url: null }] });
  state = applyChatStreamEvent(state, { type: "delta", text: "第一段" });
  state = applyChatStreamEvent(state, { type: "delta", text: "第二段" });
  state = applyChatStreamEvent(state, { type: "done", timing_ms: {} });
  assert.equal(state.content, "第一段第二段");
  assert.equal(state.sources[0].work_id, 7);
  assert.equal(state.completed, true);
  assert.throws(
    () => applyChatStreamEvent(state, { type: "error", message: "模型失败" }),
    /模型失败/,
  );
});

test("library pagination de-duplicates overlapping load-more responses", () => {
  const current = { items: [{ id: 1 }, { id: 2 }], total: 4, selected_count: 0, account_selected_count: 0, next_offset: 2 };
  const incoming = { items: [{ id: 2 }, { id: 3 }], total: 4, selected_count: 0, account_selected_count: 0, next_offset: 3 };
  assert.deepEqual(mergeWorksPage(current, incoming, true).items.map((work) => work.id), [1, 2, 3]);
  assert.deepEqual(mergeWorksPage(current, incoming, false).items.map((work) => work.id), [2, 3]);
});

test("partial batch confirmation removes only server-confirmed preview items", () => {
  const batch = { id: "batch-a", items: [{ id: 11 }, { id: 12 }, { id: 13 }] };
  const reconciled = removeConfirmedImportItems(batch, new Set([11, 13]));
  assert.deepEqual(reconciled.items.map((item) => item.id), [12]);
  assert.deepEqual(batch.items.map((item) => item.id), [11, 12, 13]);
});

test("processing job polling invalidates dependent data only on a terminal transition", () => {
  const job = (id, state, jobType = "ingest") => ({ id, state, job_type: jobType });
  assert.equal(didAnyJobReachTerminal([job("a", "running")], [job("a", "running")]), false);
  assert.equal(didAnyJobReachTerminal([job("a", "running")], [job("a", "succeeded")]), true);
  assert.equal(didAnyJobReachTerminal([job("preview", "running", "link_preview")], []), false);
  assert.equal(operationIncludesJob({ job: job("nested", "queued") }), true);
  assert.match(page, /const next = await api\.jobs\(\)/);
  assert.match(page, /if \(reachedTerminal\) \{[\s\S]*?loadUsage\(\)[\s\S]*?loadCollections\(\)/);
  assert.doesNotMatch(page, /loadCommon/);
});

test("batch storage keeps generic and package recovery indexes together", () => {
  const createStorage = () => {
    const values = new Map();
    return {
      getItem: (key) => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, String(value)),
      removeItem: (key) => values.delete(key),
    };
  };
  const previousWindow = globalThis.window;
  const localStorage = createStorage();
  globalThis.window = { localStorage, sessionStorage: createStorage() };
  try {
    rememberPackageBatch("package-1");
    assert.deepEqual(JSON.parse(localStorage.getItem(IMPORT_BATCH_STORAGE_KEY)), ["package-1"]);
    assert.deepEqual(JSON.parse(localStorage.getItem(PACKAGE_IMPORT_STORAGE_KEY)), ["package-1"]);
    localStorage.setItem(PACKAGE_IMPORT_STORAGE_KEY, "not-json");
    assert.deepEqual(storedPackageBatches(), []);
  } finally {
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});

test("critical API contract guards reject malformed local responses", () => {
  assert.deepEqual(parseJobs([{ id: "j1", job_type: "ingest", state: "running", message: "处理中", progress: {} }])[0].id, "j1");
  assert.throws(() => parseJobs([{ id: 1, job_type: "ingest", state: "running", message: "处理中", progress: {} }]), /任务列表格式无效/);
  const runtimeSettings = {
    daily_media_minutes_limit: 60,
    daily_llm_token_limit: 100_000,
    monthly_warning_cny: 20,
    scene_threshold: 0.35,
    max_scene_candidates: 120,
    max_keyframes: 12,
    min_keyframe_gap_seconds: 2,
    dpapi_warning: "本地估算",
    has_dashscope_key: true,
    has_bss_credentials: false,
    has_f2_cookie: false,
    security_cleanup_required: false,
    security_cleanup_message: "",
    summary_prompt: "总结",
    default_summary_prompt: "默认",
    default_answer_format: "rich",
    processing_model: "processing",
    chat_fast_model: "fast",
    chat_deep_model: "deep",
    processing_model_options: [],
    chat_model_options: [],
    ocr_model: "ocr",
    asr_model: "asr",
    embedding_model: "embedding",
    import_batch_limit: 10,
    import_daily_limit: 150,
    import_worker_count: 3,
    import_network_concurrency: 1,
    import_cooldown_min_seconds: 4,
    import_cooldown_max_seconds: 8,
  };
  assert.equal(parseRuntimeSettings(runtimeSettings).summary_prompt, "总结");
  assert.throws(
    () => parseRuntimeSettings({ ...runtimeSettings, daily_media_minutes_limit: undefined }),
    /设置格式无效/,
  );
  assert.throws(
    () => parseRuntimeSettings({ ...runtimeSettings, import_daily_limit: Number.NaN }),
    /设置格式无效/,
  );
  assert.throws(
    () => parseRuntimeSettings({ ...runtimeSettings, has_f2_cookie: "false" }),
    /设置格式无效/,
  );
  assert.throws(() => parseImportBatch({ id: "b1", state: "running", items: [] }), /导入批次格式无效/);
  assert.throws(() => parseChatStreamEvent({ type: "delta", text: 1 }), /回答流事件格式无效/);
  assert.equal(parseProbe({
    probe: "coordinators",
    status: "healthy",
    message: "正常",
    details: { coordinators: [{ name: "processing", alive: true, workers_alive: 1, workers_expected: 1, last_error: null }] },
  }).details.coordinators[0].alive, true);
});

test("chat markdown is split from the import shell and stream paints are frame-batched", () => {
  assert.match(page, /const Chat = dynamic\(/);
  assert.match(page, /import\("\.\.\/components\/Chat"\)/);
  assert.doesNotMatch(page, /components\/MarkdownContent/);
  assert.match(chatComponent, /import \{ AnswerBlock \} from "\.\/MarkdownContent"/);
  assert.match(page, /requestAnimationFrame\(renderStreamState\)/);
});

test("health checks include coordinator liveness details", () => {
  assert.match(page, /HEALTH_PROBE_NAMES = \["database", "media_runtime", "coordinators", "security_cleanup"\]/);
  assert.match(contracts, /coordinators\?: Array/);
  assert.match(page, /后台协调器/);
  assert.match(page, /workers_alive/);
});

test("chat retains markdown, math, answer formats, and fast/deep modes", async () => {
  const markdown = await readFile(new URL("../components/MarkdownContent.tsx", import.meta.url), "utf8");
  assert.match(chatComponent, /阅读排版/);
  assert.match(chatComponent, /快速回答/);
  assert.match(chatComponent, /深度回答/);
  assert.match(markdown, /ReactMarkdown/);
  assert.match(markdown, /remarkMath/);
  assert.match(markdown, /rehypeKatex/);
  assert.match(markdown, /format === "markdown" \? <pre className="markdown-source"><code>\{content\}<\/code><\/pre>/);
  assert.match(chatComponent, /<AnswerBlock/);
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

test("library keeps primary states plus supplement view, summaries, lazy covers, and Obsidian export", async () => {
  const obsidian = await readFile(new URL("../lib/obsidian.ts", import.meta.url), "utf8");
  for (const state of ["pending", "in_library", "supplement", "issues", "archived"]) {
    assert.match(page, new RegExp(`state === "${state}"`));
  }
  assert.match(page, /补齐\/更新总结/);
  assert.match(page, /导出到 Obsidian/);
  assert.match(page, /设置图片统一存放位置/);
  assert.match(page, /loading="lazy" decoding="async"/);
  assert.match(assets, /value\?\.startsWith\("\/api\/"\) \? `\$\{API_BASE\}\$\{value\}` : null/);
  assert.match(page, /className="technical-details" aria-label="技术详情"/);
  assert.match(page, /确认永久删除这个作品、总结、索引与本地资产/);
  assert.match(page, /\["required", "failed"\]\.includes\(work\.supplement_state \|\| ""\)/);
  assert.match(page, /className="work-supplement-indicator">需要补件/);
  assert.match(styles, /\.work-supplement-indicator/);
  assert.match(obsidian, /showDirectoryPicker/);
  assert.match(obsidian, /text\/markdown;charset=utf-8/);
  assert.match(obsidian, /chooseObsidianImageDirectory/);
});

test("supplement view explains material gaps and uploads complete local media", () => {
  assert.match(page, /待补件 <em>\{summary\.supplement_count \?\? 0\}<\/em>/);
  assert.match(page, /<strong>材料缺口：<\/strong>\{supplementReasonLabel\(work\.supplement_reason\)\}/);
  assert.match(page, /work\.evidence_state === "sufficient" \? "现有材料可检索"/);
  assert.match(page, /full_video_unavailable: "未取得完整视频文件；已有总结可能来自字幕、音频或部分画面"/);
  assert.match(page, /HIDDEN_TRACK_REPORT_KEYS = new Set\(\["migration", "kind"\]\)/);
  assert.match(page, /required: evidenceState === "sufficient" \? "建议补充" : "需要补充"/);
  assert.match(page, /typeof report\.available === "boolean"/);
  assert.match(page, /report\.available \? "可用" : "未取得"/);
  assert.match(page, /label: "音频\/字幕"/);
  assert.match(page, /return `音频：\$\{audio\} · 字幕：\$\{subtitle\}`/);
  assert.match(page, /trackReportRows\(work\.track_report, work\.kind\)/);
  assert.doesNotMatch(page, /证据/);
  assert.match(page, /我确认有权处理并上传该本地素材/);
  assert.match(page, /work\.kind === "image"\s*\? "上传完整图片组"\s*:\s*"上传完整视频"/s);
  assert.match(page, /api\.uploadWorkSupplement\(work\.id, selectedFiles\)/);
  assert.match(page, /我确认有权处理并上传这些本地素材/);
  assert.match(page, /无论下载权限状态如何都会尝试取得并处理全部公开图片/);
  assert.match(api, /state: "pending" \| "in_library" \| "supplement" \| "issues" \| "archived"/);
  assert.match(api, /body\.append\("rights_attested", "true"\)/);
  assert.match(api, /body\.append\("files", file, file\.name\)/);
  assert.match(api, /\/api\/library\/works\/\$\{id\}\/supplement/);
  assert.match(contracts, /supplement_state\?: SupplementState/);
  assert.match(contracts, /evidence_state\?: EvidenceState/);
  assert.match(contracts, /track_report\?: TrackReport \| null/);
  assert.match(styles, /\.supplement-details/);
  assert.match(styles, /\.evidence-state\.searchable/);
});

test("success notices auto-dismiss while errors stay until closed", () => {
  assert.match(page, /window\.setTimeout\(\(\) => setNotice\(""\), 5000\)/);
  assert.doesNotMatch(page, /setTimeout\(\(\) => setError\(""\)/);
});

test("cancelling a native folder picker is not reported as an application error", () => {
  assert.match(page, /import \{ isUserCancelled, reason \} from "\.\.\/lib\/errors"/);
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
  assert.match(summaryPage, /window\.history\.back\(\)/);
  assert.match(summaryPage, /readLibraryReturnContext\(\)/);
  assert.match(page, /rememberLibraryReturnContext\(\{/);
  assert.match(page, /window\.scrollTo\(\{ top: context\.scrollY, behavior: "auto" \}\)/);
  assert.match(libraryReturn, /tokbrain\.library-return\.v1/);
  assert.match(summaryPage, /summary-content/);
  assert.match(summaryPage, /永久删除/);
  assert.match(summaryPage, /api\.remove\(workId\)/);
});
