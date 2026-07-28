"use client";

import { createContext, ReactNode, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useState } from "react";

import {
  DEFAULT_THEME_ID,
  getTheme,
  isThemeId,
  ThemeDefinition,
  ThemeId,
  THEMES,
  THEME_STORAGE_KEY,
} from "./registry";
import {
  BACKGROUND_INTENSITY_STORAGE_KEY,
  backgroundOverlayOpacity,
  DEFAULT_BACKGROUND_INTENSITY,
  normalizeBackgroundIntensity,
} from "./preferences";

type ThemeContextValue = {
  theme: ThemeDefinition;
  themes: readonly ThemeDefinition[];
  setTheme: (id: ThemeId) => void;
  backgroundIntensity: number;
  setBackgroundIntensity: (value: number) => void;
};

const ThemeContext = createContext<ThemeContextValue | null>(null);

function applyTheme(id: ThemeId) {
  const definition = getTheme(id);
  document.documentElement.dataset.theme = definition.id;
  document.documentElement.dataset.themeTone = definition.tone;
  document.documentElement.style.colorScheme = definition.tone;
}

function applyBackgroundIntensity(value: number) {
  document.documentElement.style.setProperty("--theme-overlay-opacity", String(backgroundOverlayOpacity(value)));
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [themeId, setThemeId] = useState<ThemeId>(DEFAULT_THEME_ID);
  const [backgroundIntensity, setBackgroundIntensityState] = useState(DEFAULT_BACKGROUND_INTENSITY);

  useLayoutEffect(() => {
    let next: ThemeId = DEFAULT_THEME_ID;
    let nextBackgroundIntensity = DEFAULT_BACKGROUND_INTENSITY;
    try {
      const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
      if (isThemeId(stored)) next = stored;
      nextBackgroundIntensity = normalizeBackgroundIntensity(window.localStorage.getItem(BACKGROUND_INTENSITY_STORAGE_KEY));
    } catch {
      // Browser privacy settings can make storage unavailable.
    }
    applyTheme(next);
    applyBackgroundIntensity(nextBackgroundIntensity);
    const frame = window.requestAnimationFrame(() => {
      setThemeId(next);
      setBackgroundIntensityState(nextBackgroundIntensity);
    });
    return () => window.cancelAnimationFrame(frame);
  }, []);

  useEffect(() => {
    const syncAcrossTabs = (event: StorageEvent) => {
      if (event.key === THEME_STORAGE_KEY) {
        const next = isThemeId(event.newValue) ? event.newValue : DEFAULT_THEME_ID;
        setThemeId(next);
        applyTheme(next);
      }
      if (event.key === BACKGROUND_INTENSITY_STORAGE_KEY) {
        const next = normalizeBackgroundIntensity(event.newValue);
        setBackgroundIntensityState(next);
        applyBackgroundIntensity(next);
      }
    };
    window.addEventListener("storage", syncAcrossTabs);
    return () => window.removeEventListener("storage", syncAcrossTabs);
  }, []);

  const setTheme = useCallback((id: ThemeId) => {
    const next = getTheme(id).id;
    setThemeId(next);
    applyTheme(next);
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // The visible theme still changes even when storage is unavailable.
    }
  }, []);

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

  const value = useMemo<ThemeContextValue>(() => ({
    theme: getTheme(themeId),
    themes: THEMES,
    setTheme,
    backgroundIntensity,
    setBackgroundIntensity,
  }), [backgroundIntensity, setBackgroundIntensity, setTheme, themeId]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  const value = useContext(ThemeContext);
  if (!value) throw new Error("useTheme must be used within ThemeProvider");
  return value;
}
