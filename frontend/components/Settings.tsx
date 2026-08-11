"use client";

import type { CSSProperties, FormEvent } from "react";
import { useEffect, useRef, useState } from "react";

import { api } from "../lib/api";
import type {
  Health,
  IntegrationTokenStatus,
  RuntimeSettings,
  RuntimeSettingsUpdate,
  Usage,
} from "../lib/api";
import { reason } from "../lib/errors";
import type { PerformOperation } from "../lib/uiTypes";
import { useTheme } from "../themes/ThemeProvider";
import type { ThemeDefinition } from "../themes/registry";

export function ThemePicker() {
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

const HEALTH_PROBE_NAMES = ["database", "media_runtime", "coordinators", "security_cleanup"] as const;

function coordinatorProbeDetails(probe: Health["probes"][number] | undefined) {
  const coordinators = probe?.details.coordinators;
  if (!coordinators?.length) return "";
  const labels: Record<string, string> = {
    link_preview: "链接预检",
    package_import: "数据包",
    processing: "入库处理",
  };
  return coordinators.map((item) =>
    `${labels[item.name] || item.name} ${item.workers_alive}/${item.workers_expected}`,
  ).join(" · ");
}

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

export function Settings({
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
  perform: PerformOperation;
}) {
  const [checking, setChecking] = useState(false);
  const [checkProgress, setCheckProgress] = useState(health ? 100 : 0);
  const [liveProbes, setLiveProbes] = useState<Health["probes"]>(health?.probes || []);
  const [f2CookieDraft, setF2CookieDraft] = useState("");
  const [billingAccessKeyIdDraft, setBillingAccessKeyIdDraft] = useState("");
  const [billingAccessKeySecretDraft, setBillingAccessKeySecretDraft] = useState("");
  const [summaryPromptDraft, setSummaryPromptDraft] = useState(settings.summary_prompt);
  const [integrationToken, setIntegrationToken] = useState<IntegrationTokenStatus | null>(null);
  const [revealedIntegrationToken, setRevealedIntegrationToken] = useState("");
  const [integrationTokenBusy, setIntegrationTokenBusy] = useState(false);
  const [integrationTokenMessage, setIntegrationTokenMessage] = useState("");
  const [integrationTokenError, setIntegrationTokenError] = useState("");
  const dashscopeApiKeyRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let active = true;
    api.integrationToken()
      .then((status) => { if (active) setIntegrationToken(status); })
      .catch((value) => { if (active) setIntegrationTokenError(reason(value, "读取外部导入令牌状态失败")); });
    return () => { active = false; };
  }, []);

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const numeric = ["daily_media_minutes_limit", "daily_llm_token_limit", "monthly_warning_cny", "scene_threshold", "max_scene_candidates", "max_keyframes", "min_keyframe_gap_seconds"] as const;
    const body: RuntimeSettingsUpdate = {};
    numeric.forEach((name) => body[name] = Number(form.get(name)));
    (["dashscope_api_key", "bss_access_key_id", "bss_access_key_secret"] as const).forEach((name) => { const value = String(form.get(name) || "").trim(); if (value) body[name] = value; });
    (["processing_model", "chat_fast_model", "chat_deep_model"] as const).forEach((name) => {
      body[name] = String(form.get(name) || "");
    });
    const clearF2Cookie = form.get("clear_f2_cookie") === "on";
    body.clear_f2_cookie = clearF2Cookie;
    const answerFormat = String(form.get("default_answer_format") || "rich");
    body.default_answer_format = ["rich", "markdown", "plain"].includes(answerFormat)
      ? answerFormat as RuntimeSettings["default_answer_format"]
      : "rich";
    body.summary_prompt = summaryPromptDraft.trim() || settings.default_summary_prompt;
    const saved = await perform("settings", () => api.saveSettings(body), "设置已保存");
    if (saved) {
      if (dashscopeApiKeyRef.current) dashscopeApiKeyRef.current.value = "";
      if (clearF2Cookie) setF2CookieDraft("");
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
      if (dashscopeApiKeyRef.current) dashscopeApiKeyRef.current.value = "";
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

  async function createIntegrationToken() {
    if (
      integrationToken?.configured
      && !window.confirm("轮换令牌会立即使旧令牌失效。确认继续吗？")
    ) return;
    setIntegrationTokenBusy(true);
    setIntegrationTokenError("");
    setIntegrationTokenMessage("");
    try {
      const created = await api.createIntegrationToken();
      setIntegrationToken({
        configured: created.configured,
        prefix: created.prefix,
        created_at: created.created_at,
      });
      setRevealedIntegrationToken(created.token);
      setIntegrationTokenMessage("新令牌已生成。请立即复制，关闭或离开本页后无法再次查看明文。");
    } catch (value) {
      setIntegrationTokenError(reason(value, "生成外部导入令牌失败"));
    } finally {
      setIntegrationTokenBusy(false);
    }
  }

  async function copyIntegrationToken() {
    if (!revealedIntegrationToken) return;
    try {
      await navigator.clipboard.writeText(revealedIntegrationToken);
      setIntegrationTokenMessage("令牌已复制到剪贴板，请保存到调用工具的安全配置中。");
      setIntegrationTokenError("");
    } catch {
      setIntegrationTokenError("自动复制失败，请选中令牌后手动复制。");
    }
  }

  async function revokeIntegrationToken() {
    if (!window.confirm("撤销后，所有使用当前令牌的外部导入工具都会立即失效。确认撤销吗？")) return;
    setIntegrationTokenBusy(true);
    setIntegrationTokenError("");
    setIntegrationTokenMessage("");
    try {
      const status = await api.revokeIntegrationToken();
      setIntegrationToken(status);
      setRevealedIntegrationToken("");
      setIntegrationTokenMessage("外部导入令牌已撤销。");
    } catch (value) {
      setIntegrationTokenError(reason(value, "撤销外部导入令牌失败"));
    } finally {
      setIntegrationTokenBusy(false);
    }
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
                    <strong>{probeName === "database" ? "本地数据库" : probeName === "media_runtime" ? "音视频工具" : probeName === "coordinators" ? "后台协调器" : "敏感数据清理"}</strong>
                    <small>{probe?.message || (pendingState === "checking" ? "正在检测…" : "等待检测")}</small>
                    {probeName === "coordinators" && coordinatorProbeDetails(probe) && <small>{coordinatorProbeDetails(probe)}</small>}
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
                maxLength={12000}
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
            <label className="field wide"><span>百炼模型密钥 {settings.has_dashscope_key && "（已保存，留空不修改）"}</span><input ref={dashscopeApiKeyRef} name="dashscope_api_key" type="password" autoComplete="new-password" /></label>
            <div className="field wide cookie-field">
              <span>可选解析 Cookie {settings.has_f2_cookie && "（已保存并生效）"}</span>
              <textarea
                rows={3}
                maxLength={20000}
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
        <section className="card integration-token-card">
          <div className="section-head">
            <div>
              <span className="kicker">同机外部工具</span>
              <h2>外部批量导入令牌</h2>
            </div>
            <span className={`pill ${integrationToken?.configured ? "succeeded" : "cancelled"}`}>
              {integrationToken == null ? "读取中" : integrationToken.configured ? "已启用" : "未配置"}
            </span>
          </div>
          <p className="muted">
            令牌仅用于本机 127.0.0.1 上的版本化批量导入接口。后台只保存哈希，明文仅在生成或轮换后显示一次。
          </p>
          {integrationToken?.configured && (
            <div className="integration-token-status">
              <span><small>令牌前缀</small><strong>{integrationToken.prefix || "—"}…</strong></span>
              <span><small>生成时间</small><strong>{integrationToken.created_at ? new Date(integrationToken.created_at).toLocaleString("zh-CN") : "—"}</strong></span>
            </div>
          )}
          {revealedIntegrationToken && (
            <div className="integration-token-reveal">
              <strong>请立即复制这枚令牌</strong>
              <div>
                <input
                  readOnly
                  value={revealedIntegrationToken}
                  aria-label="新生成的外部导入令牌"
                  onFocus={(event) => event.target.select()}
                />
                <button type="button" className="secondary" onClick={copyIntegrationToken}>复制令牌</button>
                <button type="button" className="link" onClick={() => setRevealedIntegrationToken("")}>已保存，隐藏</button>
              </div>
            </div>
          )}
          {integrationTokenMessage && <p className="integration-token-message">{integrationTokenMessage}</p>}
          {integrationTokenError && <p className="integration-token-error">{integrationTokenError}</p>}
          <div className="button-row integration-token-actions">
            <button
              type="button"
              className="secondary"
              disabled={integrationTokenBusy || integrationToken == null}
              onClick={createIntegrationToken}
            >
              {integrationTokenBusy
                ? "处理中…"
                : integrationToken?.configured
                  ? "轮换令牌"
                  : "生成令牌"}
            </button>
            {integrationToken?.configured && (
              <button type="button" className="danger" disabled={integrationTokenBusy} onClick={revokeIntegrationToken}>
                撤销令牌
              </button>
            )}
          </div>
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
