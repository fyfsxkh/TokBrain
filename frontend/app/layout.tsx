import type { Metadata } from "next";
import "katex/dist/katex.min.css";
import "./globals.css";
import "../themes/styles/base.css";
import "../themes/styles/classic-night.css";
import "../themes/styles/dragonbone-biopunk.css";
import "../themes/styles/aether-sky-city.css";
import "../themes/styles/abyssal-runepunk.css";
import "../themes/styles/paper-organizer.css";
import "../themes/styles/ocean-dawn.css";
import "../themes/styles/sunset-cloudsea.css";
import "../themes/styles/aurora-snowfield.css";
import "../themes/styles/eastern-mist.css";
import "../themes/styles/moss-forest.css";
import "../themes/styles/desert-observatory.css";
import "../themes/styles/sakura-valley.css";
import "../themes/styles/volcanic-forge.css";

import { ThemeProvider } from "../themes/ThemeProvider";
import {
  BACKGROUND_INTENSITY_STORAGE_KEY,
  backgroundOverlayOpacity,
  DEFAULT_BACKGROUND_INTENSITY,
} from "../themes/preferences";
import { DEFAULT_THEME_ID, THEMES, THEME_STORAGE_KEY } from "../themes/registry";

export const metadata: Metadata = {
  title: "TokBrain",
  description: "把用户主动提交的公开作品变成可检索、可追溯的本地知识库",
};

const themeToneMap = Object.fromEntries(THEMES.map((theme) => [theme.id, theme.tone]));
const themeBootScript = `(()=>{const d=document.documentElement;const fallback=${JSON.stringify(DEFAULT_THEME_ID)};const defaultIntensity=${DEFAULT_BACKGROUND_INTENSITY};try{const saved=localStorage.getItem(${JSON.stringify(THEME_STORAGE_KEY)});const tones=${JSON.stringify(themeToneMap)};const id=Object.prototype.hasOwnProperty.call(tones,saved)?saved:fallback;const storedIntensity=localStorage.getItem(${JSON.stringify(BACKGROUND_INTENSITY_STORAGE_KEY)});const raw=storedIntensity===null?defaultIntensity:Number(storedIntensity);const intensity=Number.isFinite(raw)?Math.min(100,Math.max(0,Math.round(raw))):defaultIntensity;d.dataset.theme=id;d.dataset.themeTone=tones[id];d.style.colorScheme=tones[id];d.style.setProperty("--theme-overlay-opacity",String(Math.max(.28,1-intensity*.0072)))}catch{d.dataset.theme=fallback;d.dataset.themeTone="dark";d.style.colorScheme="dark";d.style.setProperty("--theme-overlay-opacity",${JSON.stringify(String(backgroundOverlayOpacity(DEFAULT_BACKGROUND_INTENSITY)))})}})();`;

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN" data-theme={DEFAULT_THEME_ID} data-theme-tone="dark" suppressHydrationWarning>
      <head><script dangerouslySetInnerHTML={{ __html: themeBootScript }} /></head>
      <body><ThemeProvider>{children}</ThemeProvider></body>
    </html>
  );
}
