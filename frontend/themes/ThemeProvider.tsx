"use client";

import {
  createContext,
  ReactNode,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
} from "react";

import { api } from "../lib/api";
import type { CustomTheme } from "../lib/api";
import { resolvedAssetUrl } from "../lib/assets";
import {
  DEFAULT_THEME_ID,
  DYNAMIC_THEMES,
  DynamicThemeId,
  getDynamicTheme,
  getTheme,
  isDynamicThemeId,
  isThemeId,
  paletteTone,
  ThemeDefinition,
  ThemeId,
  ThemePalette,
  THEMES,
  THEME_STORAGE_KEY,
} from "./registry";
import {
  BACKGROUND_INTENSITY_STORAGE_KEY,
  backgroundOverlayOpacity,
  DEFAULT_BACKGROUND_INTENSITY,
  normalizeBackgroundIntensity,
} from "./preferences";

export const THEME_SELECTION_STORAGE_KEY = "tokbrain.theme-selection.v1";
export const THEME_MOTION_STORAGE_KEY = "tokbrain.theme-motion";

export type ThemeSelection =
  | { kind: "builtin-static"; id: ThemeId }
  | { kind: "builtin-dynamic"; id: DynamicThemeId }
  | { kind: "custom"; id: string };

export type ActiveWallpaper =
  | { kind: "image"; src: string; poster: string }
  | { kind: "animated-image"; src: string; poster: string }
  | {
      kind: "video";
      poster: string;
      sources: readonly { src: string; type: string }[];
    };

type ThemeContextValue = {
  theme: ThemeDefinition;
  themes: readonly ThemeDefinition[];
  dynamicThemes: typeof DYNAMIC_THEMES;
  customThemes: CustomTheme[];
  customThemesError: string;
  selection: ThemeSelection;
  wallpaper: ActiveWallpaper | null;
  setTheme: (id: ThemeId) => void;
  setDynamicTheme: (id: DynamicThemeId) => void;
  setCustomTheme: (id: string) => void;
  refreshCustomThemes: () => Promise<CustomTheme[]>;
  upsertCustomTheme: (theme: CustomTheme) => void;
  removeCustomTheme: (id: string) => void;
  previewPalette: (baseThemeId: ThemeId, palette: ThemePalette) => void;
  restoreTheme: () => void;
  backgroundIntensity: number;
  setBackgroundIntensity: (value: number) => void;
  motionEnabled: boolean;
  setMotionEnabled: (enabled: boolean) => void;
};

const ThemeContext = createContext<ThemeContextValue | null>(null);

const PALETTE_VARIABLES: Record<keyof ThemePalette, readonly string[]> = {
  page: ["--bg"],
  sidebar: ["--sidebar-bg"],
  surface: ["--panel", "--card-bg"],
  surfaceAlt: ["--panel-2", "--input-bg"],
  border: ["--line", "--panel-border"],
  text: ["--text"],
  muted: ["--muted"],
  primary: ["--cyan"],
  secondary: ["--pink"],
  success: ["--success"],
  warning: ["--amber"],
  danger: ["--danger", "--danger-text", "--danger-border"],
};

function clearPalette() {
  for (const variables of Object.values(PALETTE_VARIABLES)) {
    for (const variable of variables) document.documentElement.style.removeProperty(variable);
  }
  document.documentElement.style.removeProperty("--danger-surface");
}

function applyPalette(palette: ThemePalette | null) {
  clearPalette();
  if (!palette) return;
  for (const [field, variables] of Object.entries(PALETTE_VARIABLES) as [keyof ThemePalette, readonly string[]][]) {
    for (const variable of variables) document.documentElement.style.setProperty(variable, palette[field]);
  }
  document.documentElement.style.setProperty(
    "--danger-surface",
    `color-mix(in srgb, ${palette.danger} 16%, transparent)`,
  );
}

function applyTheme(id: ThemeId, palette: ThemePalette | null, source: ThemeSelection["kind"] | "custom-inherited") {
  const definition = getTheme(id);
  const tone = palette ? paletteTone(palette) : definition.tone;
  document.documentElement.dataset.theme = definition.id;
  document.documentElement.dataset.themeTone = tone;
  document.documentElement.dataset.wallpaperSource = source;
  document.documentElement.style.colorScheme = tone;
  applyPalette(palette);
}

function applyBackgroundIntensity(value: number) {
  document.documentElement.style.setProperty(
    "--theme-overlay-opacity",
    String(backgroundOverlayOpacity(value)),
  );
}

function defaultSelection(id: ThemeId = DEFAULT_THEME_ID): ThemeSelection {
  return { kind: "builtin-static", id };
}

export function parseStoredSelection(value: string | null, fallback: ThemeId): ThemeSelection {
  if (!value) return defaultSelection(fallback);
  try {
    const parsed = JSON.parse(value) as { kind?: unknown; id?: unknown };
    if (parsed.kind === "builtin-static" && isThemeId(parsed.id)) {
      return { kind: parsed.kind, id: parsed.id };
    }
    if (parsed.kind === "builtin-dynamic" && isDynamicThemeId(parsed.id)) {
      return { kind: parsed.kind, id: parsed.id };
    }
    if (parsed.kind === "custom" && typeof parsed.id === "string" && parsed.id.length <= 100) {
      return { kind: parsed.kind, id: parsed.id };
    }
  } catch {
    // Corrupt browser state falls back to the valid legacy theme.
  }
  return defaultSelection(fallback);
}

function baseThemeForSelection(selection: ThemeSelection, customThemes: CustomTheme[]): ThemeId {
  if (selection.kind === "builtin-static") return selection.id;
  if (selection.kind === "builtin-dynamic") {
    return getDynamicTheme(selection.id)?.baseThemeId || DEFAULT_THEME_ID;
  }
  const custom = customThemes.find((item) => item.id === selection.id);
  return isThemeId(custom?.source_theme_id) ? custom.source_theme_id : DEFAULT_THEME_ID;
}

function customPaletteForSelection(selection: ThemeSelection, customThemes: CustomTheme[]) {
  if (selection.kind !== "custom") return null;
  return customThemes.find((item) => item.id === selection.id)?.palette || null;
}

function wallpaperSourceForSelection(selection: ThemeSelection, customThemes: CustomTheme[]) {
  if (selection.kind !== "custom") return selection.kind;
  const custom = customThemes.find((item) => item.id === selection.id);
  return custom?.media_kind === "inherited" ? "custom-inherited" : "custom";
}

function wallpaperForSelection(selection: ThemeSelection, customThemes: CustomTheme[]): ActiveWallpaper | null {
  if (selection.kind === "builtin-static") return null;
  if (selection.kind === "builtin-dynamic") {
    const dynamicTheme = getDynamicTheme(selection.id);
    return dynamicTheme
      ? { kind: "video", poster: dynamicTheme.poster, sources: dynamicTheme.sources }
      : null;
  }
  const custom = customThemes.find((item) => item.id === selection.id);
  if (!custom?.content_url || !custom.poster_url || custom.media_kind === "inherited") return null;
  const src = resolvedAssetUrl(custom.content_url);
  const poster = resolvedAssetUrl(custom.poster_url);
  if (custom.media_kind === "static") return { kind: "image", src, poster };
  if (custom.mime_type?.startsWith("image/")) return { kind: "animated-image", src, poster };
  return { kind: "video", poster, sources: [{ src, type: custom.mime_type || "video/mp4" }] };
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [themeId, setThemeId] = useState<ThemeId>(DEFAULT_THEME_ID);
  const [selection, setSelection] = useState<ThemeSelection>(defaultSelection());
  const [customThemes, setCustomThemes] = useState<CustomTheme[]>([]);
  const [customThemesError, setCustomThemesError] = useState("");
  const [customThemesLoaded, setCustomThemesLoaded] = useState(false);
  const [backgroundIntensity, setBackgroundIntensityState] = useState(DEFAULT_BACKGROUND_INTENSITY);
  const [motionEnabled, setMotionEnabledState] = useState(true);

  const persistSelection = useCallback((next: ThemeSelection, themes: CustomTheme[]) => {
    const baseThemeId = baseThemeForSelection(next, themes);
    const palette = customPaletteForSelection(next, themes);
    setSelection(next);
    setThemeId(baseThemeId);
    applyTheme(baseThemeId, palette, wallpaperSourceForSelection(next, themes));
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, baseThemeId);
      window.localStorage.setItem(THEME_SELECTION_STORAGE_KEY, JSON.stringify(next));
    } catch {
      // The visible theme still changes when browser storage is unavailable.
    }
  }, []);

  const refreshCustomThemes = useCallback(async () => {
    try {
      const items = await api.themes();
      setCustomThemes(items);
      setCustomThemesError("");
      setCustomThemesLoaded(true);
      return items;
    } catch {
      setCustomThemesError("自定义主题暂时不可用；内置主题仍可正常使用。");
      setCustomThemesLoaded(true);
      return [];
    }
  }, []);

  useLayoutEffect(() => {
    let nextThemeId: ThemeId = DEFAULT_THEME_ID;
    let nextSelection = defaultSelection();
    let nextBackgroundIntensity = DEFAULT_BACKGROUND_INTENSITY;
    let nextMotionEnabled = true;
    try {
      const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
      if (isThemeId(stored)) nextThemeId = stored;
      nextSelection = parseStoredSelection(
        window.localStorage.getItem(THEME_SELECTION_STORAGE_KEY),
        nextThemeId,
      );
      nextBackgroundIntensity = normalizeBackgroundIntensity(
        window.localStorage.getItem(BACKGROUND_INTENSITY_STORAGE_KEY),
      );
      nextMotionEnabled = window.localStorage.getItem(THEME_MOTION_STORAGE_KEY) !== "false";
    } catch {
      // Browser privacy settings can make storage unavailable.
    }
    const baseThemeId = baseThemeForSelection(nextSelection, []);
    applyTheme(
      baseThemeId === DEFAULT_THEME_ID && nextSelection.kind === "custom" ? nextThemeId : baseThemeId,
      null,
      nextSelection.kind === "custom" ? "custom-inherited" : nextSelection.kind,
    );
    applyBackgroundIntensity(nextBackgroundIntensity);
    const frame = window.requestAnimationFrame(() => {
      setThemeId(baseThemeId === DEFAULT_THEME_ID && nextSelection.kind === "custom" ? nextThemeId : baseThemeId);
      setSelection(nextSelection);
      setBackgroundIntensityState(nextBackgroundIntensity);
      setMotionEnabledState(nextMotionEnabled);
    });
    return () => window.cancelAnimationFrame(frame);
  }, []);

  useEffect(() => {
    let cancelled = false;
    void api.themes().then((items) => {
      if (cancelled) return;
      setCustomThemes(items);
      setCustomThemesError("");
      setCustomThemesLoaded(true);
    }).catch(() => {
      if (cancelled) return;
      setCustomThemesError("自定义主题暂时不可用；内置主题仍可正常使用。");
      setCustomThemesLoaded(true);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (selection.kind !== "custom") return;
    if (!customThemesLoaded) return;
    const frame = window.requestAnimationFrame(() => {
      const selected = customThemes.find((item) => item.id === selection.id);
      if (!selected) {
        if (customThemesError) return;
        persistSelection(defaultSelection(themeId), customThemes);
        return;
      }
      const base = isThemeId(selected.source_theme_id) ? selected.source_theme_id : DEFAULT_THEME_ID;
      setThemeId(base);
      applyTheme(base, selected.palette, wallpaperSourceForSelection(selection, customThemes));
      try {
        window.localStorage.setItem(THEME_STORAGE_KEY, base);
      } catch {
        // The active custom theme remains visible when storage is unavailable.
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [customThemes, customThemesError, customThemesLoaded, persistSelection, selection, themeId]);

  useEffect(() => {
    const syncAcrossTabs = (event: StorageEvent) => {
      if (event.key === THEME_SELECTION_STORAGE_KEY) {
        const fallback = isThemeId(window.localStorage.getItem(THEME_STORAGE_KEY))
          ? window.localStorage.getItem(THEME_STORAGE_KEY) as ThemeId
          : DEFAULT_THEME_ID;
        persistSelection(parseStoredSelection(event.newValue, fallback), customThemes);
      }
      if (event.key === BACKGROUND_INTENSITY_STORAGE_KEY) {
        const next = normalizeBackgroundIntensity(event.newValue);
        setBackgroundIntensityState(next);
        applyBackgroundIntensity(next);
      }
      if (event.key === THEME_MOTION_STORAGE_KEY) {
        setMotionEnabledState(event.newValue !== "false");
      }
    };
    window.addEventListener("storage", syncAcrossTabs);
    return () => window.removeEventListener("storage", syncAcrossTabs);
  }, [customThemes, persistSelection]);

  const setTheme = useCallback((id: ThemeId) => {
    persistSelection({ kind: "builtin-static", id: getTheme(id).id }, customThemes);
  }, [customThemes, persistSelection]);

  const setDynamicTheme = useCallback((id: DynamicThemeId) => {
    if (getDynamicTheme(id)) persistSelection({ kind: "builtin-dynamic", id }, customThemes);
  }, [customThemes, persistSelection]);

  const setCustomTheme = useCallback((id: string) => {
    persistSelection({ kind: "custom", id }, customThemes);
  }, [customThemes, persistSelection]);

  const upsertCustomTheme = useCallback((nextTheme: CustomTheme) => {
    setCustomThemes((current) => {
      const next = [nextTheme, ...current.filter((item) => item.id !== nextTheme.id)];
      if (selection.kind === "custom" && selection.id === nextTheme.id) {
        window.requestAnimationFrame(() => persistSelection(selection, next));
      }
      return next;
    });
  }, [persistSelection, selection]);

  const removeCustomTheme = useCallback((id: string) => {
    const removed = customThemes.find((item) => item.id === id);
    const nextThemes = customThemes.filter((item) => item.id !== id);
    setCustomThemes(nextThemes);
    if (selection.kind === "custom" && selection.id === id) {
      const fallback = isThemeId(removed?.source_theme_id) ? removed.source_theme_id : DEFAULT_THEME_ID;
      persistSelection(defaultSelection(fallback), nextThemes);
    }
  }, [customThemes, persistSelection, selection]);

  const setBackgroundIntensity = useCallback((value: number) => {
    const next = normalizeBackgroundIntensity(value);
    setBackgroundIntensityState(next);
    applyBackgroundIntensity(next);
    try {
      window.localStorage.setItem(BACKGROUND_INTENSITY_STORAGE_KEY, String(next));
    } catch {
      // The visible background still changes even when storage is unavailable.
    }
  }, []);

  const setMotionEnabled = useCallback((enabled: boolean) => {
    setMotionEnabledState(enabled);
    try {
      window.localStorage.setItem(THEME_MOTION_STORAGE_KEY, String(enabled));
    } catch {
      // The current session preference still applies.
    }
  }, []);

  const previewPalette = useCallback((baseThemeId: ThemeId, palette: ThemePalette) => {
    applyTheme(baseThemeId, palette, "custom-inherited");
  }, []);

  const restoreTheme = useCallback(() => {
    const baseThemeId = baseThemeForSelection(selection, customThemes);
    applyTheme(
      baseThemeId,
      customPaletteForSelection(selection, customThemes),
      wallpaperSourceForSelection(selection, customThemes),
    );
  }, [customThemes, selection]);

  const value = useMemo<ThemeContextValue>(() => ({
    theme: getTheme(themeId),
    themes: THEMES,
    dynamicThemes: DYNAMIC_THEMES,
    customThemes,
    customThemesError,
    selection,
    wallpaper: wallpaperForSelection(selection, customThemes),
    setTheme,
    setDynamicTheme,
    setCustomTheme,
    refreshCustomThemes,
    upsertCustomTheme,
    removeCustomTheme,
    previewPalette,
    restoreTheme,
    backgroundIntensity,
    setBackgroundIntensity,
    motionEnabled,
    setMotionEnabled,
  }), [
    backgroundIntensity,
    customThemes,
    customThemesError,
    motionEnabled,
    refreshCustomThemes,
    removeCustomTheme,
    previewPalette,
    restoreTheme,
    selection,
    setBackgroundIntensity,
    setCustomTheme,
    setDynamicTheme,
    setMotionEnabled,
    setTheme,
    themeId,
    upsertCustomTheme,
  ]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  const value = useContext(ThemeContext);
  if (!value) throw new Error("useTheme must be used within ThemeProvider");
  return value;
}
