"use client";

import type { CSSProperties } from "react";
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../lib/api";
import type { CustomTheme, ThemePalette } from "../lib/api";
import { resolvedAssetUrl } from "../lib/assets";
import { reason } from "../lib/errors";
import { paletteContrastWarnings } from "../themes/palette";
import { useTheme } from "../themes/ThemeProvider";
import {
  DEFAULT_THEME_ID,
  getTheme,
  getThemePalette,
  isThemeId,
  PALETTE_FIELDS,
  ThemeDefinition,
  ThemeId,
} from "../themes/registry";

const PALETTE_LABELS: Record<keyof ThemePalette, string> = {
  page: "页面底色",
  sidebar: "侧栏",
  surface: "卡片 / 面板",
  surfaceAlt: "输入框 / 次级面板",
  border: "边框",
  text: "主文字",
  muted: "次要文字",
  primary: "主强调",
  secondary: "次强调",
  success: "成功",
  warning: "警告",
  danger: "危险",
};

function themePoster(id: ThemeId) {
  return id === "classic-night" ? null : `/themes/${id}.webp`;
}

function previewStyle(palette: ThemePalette, image?: string | null) {
  return {
    "--preview-bg": palette.page,
    "--preview-surface": palette.surface,
    "--preview-accent": palette.primary,
    "--preview-text": palette.text,
    "--preview-image": image ? `url("${image}")` : "none",
  } as CSSProperties;
}

function builtinPreviewStyle(theme: ThemeDefinition) {
  return previewStyle(getThemePalette(theme.id), themePoster(theme.id));
}

function customBaseId(theme: CustomTheme): ThemeId {
  return isThemeId(theme.source_theme_id) ? theme.source_theme_id : DEFAULT_THEME_ID;
}

export function ThemePicker() {
  const {
    theme,
    themes,
    dynamicThemes,
    customThemes,
    customThemesError,
    selection,
    setTheme,
    setDynamicTheme,
    setCustomTheme,
    upsertCustomTheme,
    removeCustomTheme,
    previewPalette,
    restoreTheme,
    backgroundIntensity,
    setBackgroundIntensity,
    motionEnabled,
    setMotionEnabled,
  } = useTheme();
  const [editorOpen, setEditorOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [baseThemeId, setBaseThemeId] = useState<ThemeId>(theme.id);
  const [palette, setPalette] = useState<ThemePalette>(getThemePalette(theme.id));
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const selectedCustom = selection.kind === "custom"
    ? customThemes.find((item) => item.id === selection.id) || null
    : null;
  const selectedDynamic = selection.kind === "builtin-dynamic"
    ? dynamicThemes.find((item) => item.id === selection.id) || null
    : null;
  const currentName = selectedCustom?.name || selectedDynamic?.name || theme.name;
  const currentMark = selectedCustom ? "自" : selectedDynamic?.mark || theme.mark;
  const warnings = useMemo(() => paletteContrastWarnings(palette), [palette]);
  const restoreThemeRef = useRef(restoreTheme);

  useEffect(() => {
    restoreThemeRef.current = restoreTheme;
  }, [restoreTheme]);

  useEffect(() => {
    if (editorOpen) previewPalette(baseThemeId, palette);
  }, [baseThemeId, editorOpen, palette, previewPalette]);

  useEffect(() => () => restoreThemeRef.current(), []);

  function openCreate() {
    const source = selectedCustom ? customBaseId(selectedCustom) : theme.id;
    setEditingId(null);
    setBaseThemeId(source);
    setPalette(selectedCustom ? { ...selectedCustom.palette } : getThemePalette(source));
    setName(`${currentName}·自定义`);
    setFile(null);
    setMessage("");
    setError("");
    setEditorOpen(true);
  }

  function openEdit(item: CustomTheme) {
    setEditingId(item.id);
    setBaseThemeId(customBaseId(item));
    setPalette({ ...item.palette });
    setName(item.name);
    setFile(null);
    setMessage("");
    setError("");
    setEditorOpen(true);
  }

  function closeEditor() {
    setEditorOpen(false);
    setFile(null);
    restoreTheme();
  }

  async function saveTheme() {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const saved = editingId
        ? await api.updateTheme(editingId, { name: name.trim(), palette })
        : await api.createTheme(name.trim(), baseThemeId, palette, file);
      upsertCustomTheme(saved);
      setCustomTheme(saved.id);
      setEditorOpen(false);
      setFile(null);
      setMessage(editingId ? "自定义主题已更新。" : "自定义主题已保存并启用。");
    } catch (value) {
      setError(reason(value, "保存自定义主题失败"));
    } finally {
      setBusy(false);
    }
  }

  async function deleteTheme(item: CustomTheme) {
    if (!window.confirm(`确定删除自定义主题“${item.name}”吗？本地壁纸文件也会一并删除。`)) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await api.deleteTheme(item.id);
      removeCustomTheme(item.id);
      if (editingId === item.id) closeEditor();
      setMessage("自定义主题已删除。");
    } catch (value) {
      setError(reason(value, "删除自定义主题失败"));
    } finally {
      setBusy(false);
    }
  }

  function updateColor(field: keyof ThemePalette, value: string) {
    setPalette((current) => ({ ...current, [field]: value.toUpperCase() }));
  }

  return (
    <section className="card theme-picker">
      <div className="section-head theme-picker-head">
        <div><span className="kicker">界面主题</span><h2>壁纸与色板</h2></div>
        <span className="theme-current"><b>{currentMark}</b>{currentName}</span>
      </div>

      <div className="theme-group-head">
        <div><h3>内置静态</h3><small>原有 13 套主题保持不变</small></div>
      </div>
      <div className="theme-grid" role="radiogroup" aria-label="内置静态主题">
        {themes.map((item) => (
          <button
            type="button"
            role="radio"
            aria-checked={selection.kind === "builtin-static" && selection.id === item.id}
            className={`theme-card ${selection.kind === "builtin-static" && selection.id === item.id ? "selected" : ""}`}
            key={item.id}
            onClick={() => setTheme(item.id)}
            style={builtinPreviewStyle(item)}
          >
            <span className="theme-preview"><span className="theme-badge">静态</span></span>
            <span className="theme-card-copy"><strong>{item.name}</strong><small>{item.description}</small></span>
          </button>
        ))}
      </div>

      <div className="theme-group-head">
        <div><h3>内置动态</h3><small>本地无声循环视频，离线可用</small></div>
      </div>
      <div className="theme-grid" role="radiogroup" aria-label="内置动态主题">
        {dynamicThemes.map((item) => (
          <button
            type="button"
            role="radio"
            aria-checked={selection.kind === "builtin-dynamic" && selection.id === item.id}
            className={`theme-card ${selection.kind === "builtin-dynamic" && selection.id === item.id ? "selected" : ""}`}
            key={item.id}
            onClick={() => setDynamicTheme(item.id)}
            style={previewStyle(getThemePalette(item.baseThemeId), item.poster)}
          >
            <span className="theme-preview"><span className="theme-badge dynamic">动态</span></span>
            <span className="theme-card-copy"><strong>{item.name}</strong><small>{item.description}</small></span>
          </button>
        ))}
      </div>

      <div className="theme-group-head custom-theme-heading">
        <div><h3>我的主题</h3><small>上传壁纸或从现有主题复制后编辑 12 色色板</small></div>
        <button type="button" className="secondary" onClick={openCreate}>新建自定义主题</button>
      </div>
      {customThemes.length ? (
        <div className="theme-grid" role="radiogroup" aria-label="我的自定义主题">
          {customThemes.map((item) => {
            const base = getTheme(customBaseId(item));
            const image = item.poster_url ? resolvedAssetUrl(item.poster_url) : themePoster(base.id);
            const selected = selection.kind === "custom" && selection.id === item.id;
            return (
              <div className={`theme-card-wrap ${selected ? "selected" : ""}`} key={item.id}>
                <button
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  className={`theme-card ${selected ? "selected" : ""}`}
                  onClick={() => setCustomTheme(item.id)}
                  style={previewStyle(item.palette, image)}
                >
                  <span className="theme-preview"><span className={`theme-badge ${item.media_kind === "dynamic" ? "dynamic" : ""}`}>{item.media_kind === "dynamic" ? "动态" : item.media_kind === "static" ? "静态" : "配色"}</span></span>
                  <span className="theme-card-copy"><strong>{item.name}</strong><small>基于 {base.name} · {item.width && item.height ? `${item.width}×${item.height}` : "沿用内置壁纸"}</small></span>
                </button>
                <div className="theme-card-actions">
                  <button type="button" className="secondary" onClick={() => openEdit(item)}>编辑</button>
                  <button type="button" className="danger" onClick={() => void deleteTheme(item)} disabled={busy}>删除</button>
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="theme-empty">还没有自定义主题。可以只编辑色板，也可以上传静态或动态壁纸。</div>
      )}

      {customThemesError && <p className="theme-error" role="alert">{customThemesError}</p>}
      {error && <p className="theme-error" role="alert">{error}</p>}
      {message && <p className="theme-message" aria-live="polite">{message}</p>}

      {editorOpen && (
        <div className="theme-editor" aria-label="自定义主题编辑器">
          <div className="theme-editor-head">
            <div><span className="kicker">实时预览</span><h3>{editingId ? "编辑自定义主题" : "新建自定义主题"}</h3></div>
            <button type="button" className="secondary" onClick={closeEditor}>取消</button>
          </div>
          <div className="theme-editor-fields">
            <label><span>主题名称</span><input value={name} maxLength={80} onChange={(event) => setName(event.target.value)} /></label>
            <label>
              <span>配色基底</span>
              <select value={baseThemeId} disabled={Boolean(editingId)} onChange={(event) => {
                const next = event.target.value;
                if (isThemeId(next)) {
                  setBaseThemeId(next);
                  setPalette(getThemePalette(next));
                }
              }}>
                {themes.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}
              </select>
            </label>
            {!editingId && (
              <label className="theme-upload-field">
                <span>壁纸文件（可选）</span>
                <input
                  type="file"
                  accept="image/jpeg,image/png,image/webp,image/gif,video/mp4,video/webm"
                  onChange={(event) => setFile(event.target.files?.[0] || null)}
                />
                <small>{file ? `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB` : "支持 JPG、PNG、WebP、GIF、H.264 MP4、WebM"}</small>
              </label>
            )}
          </div>
          <div className="palette-grid">
            {PALETTE_FIELDS.map((field) => (
              <label key={field}>
                <span>{PALETTE_LABELS[field]}</span>
                <span className="palette-control">
                  <input type="color" value={palette[field]} onChange={(event) => updateColor(field, event.target.value)} />
                  <input aria-label={`${PALETTE_LABELS[field]}十六进制颜色`} value={palette[field]} pattern="#[0-9A-Fa-f]{6}" maxLength={7} onChange={(event) => updateColor(field, event.target.value)} />
                </span>
              </label>
            ))}
          </div>
          {warnings.length > 0 && (
            <div className="contrast-warning" role="status">
              <strong>可读性提醒</strong>
              {warnings.map((warning) => <span key={warning}>{warning}</span>)}
              <small>按你的选择仍可保存；如果操作困难，可随时恢复经典暗夜。</small>
            </div>
          )}
          <div className="theme-editor-actions">
            <button type="button" className="secondary" onClick={() => setPalette(getThemePalette(baseThemeId))}>恢复基底色板</button>
            <button type="button" className="secondary" onClick={() => { setTheme("classic-night"); setEditorOpen(false); }}>安全恢复经典暗夜</button>
            <button type="button" className="primary" disabled={busy || !name.trim()} onClick={() => void saveTheme()}>{busy ? "保存中…" : "保存并启用"}</button>
          </div>
        </div>
      )}

      <div className="theme-preferences">
        <label className="background-intensity">
          <span><strong>背景显现度</strong><small>统一调整静态图片、动画和视频壁纸的可见程度</small></span>
          <input aria-label="背景显现度" type="range" min="0" max="100" value={backgroundIntensity} onChange={(event) => setBackgroundIntensity(Number(event.target.value))} />
          <output>{backgroundIntensity}%</output>
        </label>
        <label className="motion-toggle">
          <span><strong>播放动态壁纸</strong><small>小屏、减少动态效果、强制色彩或后台标签会自动显示静态封面</small></span>
          <input type="checkbox" checked={motionEnabled} onChange={(event) => setMotionEnabled(event.target.checked)} />
        </label>
      </div>
    </section>
  );
}
