export const THEME_IDS = [
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
] as const;

export type ThemeId = (typeof THEME_IDS)[number];
export type ThemeTone = "light" | "dark";

export type ThemeCopy = {
  brandTagline: string;
  pages: {
    library: string;
    chat: string;
    settings: string;
  };
};

export type ThemeDefinition = {
  id: ThemeId;
  name: string;
  description: string;
  tone: ThemeTone;
  mark: string;
  preview: readonly [string, string, string, string];
  copy: ThemeCopy;
};

const sharedCopy: ThemeCopy = {
  brandTagline: "本地公开内容知识库",
  pages: {
    library: "我的知识库",
    chat: "和本地知识对话",
    settings: "安全与处理设置",
  },
};

function theme(
  definition: Omit<ThemeDefinition, "copy">,
): ThemeDefinition {
  return {
    ...definition,
    copy: sharedCopy,
  };
}

export const THEMES: readonly ThemeDefinition[] = [
  theme({
    id: "classic-night",
    name: "经典暗夜",
    description: "霓虹暗色工作台",
    tone: "dark",
    mark: "T",
    preview: ["#0d1013", "#15191d", "#36f0e0", "#f4f1ea"],
  }),
  theme({
    id: "dragonbone-biopunk",
    name: "龙骨生物朋克",
    description: "龙骨、神经纤维与琥珀胶囊",
    tone: "dark",
    mark: "脉",
    preview: ["#0c0d0f", "#181313", "#ff315a", "#d8cbb7"],
  }),
  theme({
    id: "aether-sky-city",
    name: "以太浮空城",
    description: "明亮云海、黄铜星盘与水晶航路",
    tone: "light",
    mark: "✧",
    preview: ["#f5f1e8", "#ffffff", "#41c7f4", "#12233c"],
  }),
  theme({
    id: "abyssal-runepunk",
    name: "深渊符文",
    description: "深海玻璃、潮汐符文与珍珠存储",
    tone: "dark",
    mark: "◈",
    preview: ["#031219", "#08242c", "#35f2e6", "#e8f3ef"],
  }),
  theme({
    id: "paper-organizer",
    name: "纸面清新收纳",
    description: "暖象牙纸与轻盈编辑排版",
    tone: "light",
    mark: "▱",
    preview: ["#f8f6f0", "#ffffff", "#9cb4a8", "#292929"],
  }),
  theme({
    id: "ocean-dawn",
    name: "海洋晨光",
    description: "海岸晨光与磨砂海玻璃",
    tone: "dark",
    mark: "≋",
    preview: ["#041b36", "#0d4261", "#28d8d1", "#f4fbff"],
  }),
  theme({
    id: "sunset-cloudsea",
    name: "晚霞云海",
    description: "金橙地平线与暮色玻璃",
    tone: "dark",
    mark: "●",
    preview: ["#171a3d", "#342044", "#ffb36b", "#fff0df"],
  }),
  theme({
    id: "aurora-snowfield",
    name: "极光雪原",
    description: "极夜冰湖与克制的双色极光",
    tone: "dark",
    mark: "✦",
    preview: ["#071426", "#0c2b48", "#38f29a", "#f3fbff"],
  }),
  theme({
    id: "eastern-mist",
    name: "东方雾山",
    description: "宣纸留白与现代中文编辑设计",
    tone: "light",
    mark: "山",
    preview: ["#f5f1e7", "#fbf9f2", "#426b5a", "#1f2925"],
  }),
  theme({
    id: "moss-forest",
    name: "苔藓森林",
    description: "晨光溪流与生长型知识隐喻",
    tone: "dark",
    mark: "叶",
    preview: ["#071d18", "#10372c", "#62d8c7", "#f3e9ce"],
  }),
  theme({
    id: "desert-observatory",
    name: "星夜沙漠",
    description: "沙金地平线与青铜观测刻线",
    tone: "dark",
    mark: "✷",
    preview: ["#10142c", "#1b1f3a", "#d8a657", "#efe2c4"],
  }),
  theme({
    id: "sakura-valley",
    name: "樱花溪谷",
    description: "樱粉晨雾与柔和溪流",
    tone: "light",
    mark: "花",
    preview: ["#f7eced", "#fff9f7", "#f3b6c6", "#313552"],
  }),
  theme({
    id: "volcanic-forge",
    name: "熔岩火山",
    description: "玄武岩、黑曜石与受控熔岩通道",
    tone: "dark",
    mark: "熔",
    preview: ["#0c0e12", "#171a1e", "#ff6a1a", "#f2f2ef"],
  }),
] as const;

export const DEFAULT_THEME_ID: ThemeId = "classic-night";
export const THEME_STORAGE_KEY = "tokbrain.theme";

const themeMap = new Map<ThemeId, ThemeDefinition>(
  THEMES.map((item) => [item.id, item]),
);

export function isThemeId(value: unknown): value is ThemeId {
  return typeof value === "string" && themeMap.has(value as ThemeId);
}

export function getTheme(value: unknown): ThemeDefinition {
  return isThemeId(value)
    ? themeMap.get(value)!
    : themeMap.get(DEFAULT_THEME_ID)!;
}

export const PALETTE_FIELDS = [
  "page",
  "sidebar",
  "surface",
  "surfaceAlt",
  "border",
  "text",
  "muted",
  "primary",
  "secondary",
  "success",
  "warning",
  "danger",
] as const;

export type PaletteField = (typeof PALETTE_FIELDS)[number];
export type ThemePalette = Record<PaletteField, string>;

const PALETTES: Record<ThemeId, ThemePalette> = {
  "classic-night": { page: "#0D1013", sidebar: "#0C0F12", surface: "#15191D", surfaceAlt: "#1B2025", border: "#2A3036", text: "#F4F1EA", muted: "#9BA3AA", primary: "#36F0E0", secondary: "#FF315F", success: "#36F0E0", warning: "#FFBD59", danger: "#FF665F" },
  "dragonbone-biopunk": { page: "#0C0D0F", sidebar: "#08090A", surface: "#151313", surfaceAlt: "#201818", border: "#4B3B32", text: "#E8DFCF", muted: "#A99B8B", primary: "#FF5B55", secondary: "#FF315A", success: "#FF5B55", warning: "#FFB347", danger: "#FF4A52" },
  "aether-sky-city": { page: "#EDF3F7", sidebar: "#FFFDF8", surface: "#FFFDF8", surfaceAlt: "#EDF8FD", border: "#D8C9AE", text: "#12233C", muted: "#64778B", primary: "#1DAEDB", secondary: "#EF7B67", success: "#1DAEDB", warning: "#B99146", danger: "#C74C42" },
  "abyssal-runepunk": { page: "#031219", sidebar: "#010E14", surface: "#071C23", surfaceAlt: "#0A2931", border: "#24505A", text: "#E8F3EF", muted: "#87AEB2", primary: "#35F2E6", secondary: "#8A5CFF", success: "#35F2E6", warning: "#D7CDB7", danger: "#FF765B" },
  "paper-organizer": { page: "#F8F6F0", sidebar: "#FAF8F2", surface: "#FFFEFA", surfaceAlt: "#F1EEE6", border: "#E0DBCF", text: "#292929", muted: "#716F6B", primary: "#648D7C", secondary: "#CC7F78", success: "#648D7C", warning: "#D4A373", danger: "#BA5952" },
  "ocean-dawn": { page: "#041B36", sidebar: "#031C35", surface: "#0A3852", surfaceAlt: "#0E4964", border: "#467C85", text: "#F4FBFF", muted: "#AFD0DB", primary: "#28D8D1", secondary: "#7CB8D8", success: "#28D8D1", warning: "#F0C67D", danger: "#FF7A6B" },
  "sunset-cloudsea": { page: "#171A3D", sidebar: "#161230", surface: "#2B203D", surfaceAlt: "#3A2848", border: "#735A61", text: "#FFF0DF", muted: "#D0B8C8", primary: "#FFB36B", secondary: "#FF6F9D", success: "#FFB36B", warning: "#FFD18F", danger: "#FF6F61" },
  "aurora-snowfield": { page: "#071426", sidebar: "#031226", surface: "#0A2340", surfaceAlt: "#0D2D4D", border: "#416477", text: "#F3FBFF", muted: "#A9C8D8", primary: "#61E9E2", secondary: "#8F62FF", success: "#38F29A", warning: "#38F29A", danger: "#FF767D" },
  "eastern-mist": { page: "#F5F1E7", sidebar: "#F3F4ED", surface: "#FBF9F2", surfaceAlt: "#EDF1EB", border: "#D8D4C8", text: "#1F2925", muted: "#68736D", primary: "#426B5A", secondary: "#A65B50", success: "#426B5A", warning: "#8BA887", danger: "#A74D45" },
  "moss-forest": { page: "#071D18", sidebar: "#021D17", surface: "#103127", surfaceAlt: "#153F31", border: "#45624A", text: "#F3E9CE", muted: "#B4C4A5", primary: "#62D8C7", secondary: "#9BBF63", success: "#62D8C7", warning: "#F0C87A", danger: "#E9796F" },
  "desert-observatory": { page: "#10142C", sidebar: "#080C1F", surface: "#181C34", surfaceAlt: "#22243D", border: "#67583E", text: "#EFE2C4", muted: "#B9AD9E", primary: "#D8A657", secondary: "#7658D8", success: "#D8A657", warning: "#EFC985", danger: "#E97161" },
  "sakura-valley": { page: "#EDE8EE", sidebar: "#232946", surface: "#FFF9F7", surfaceAlt: "#EBEAF3", border: "#B8B8C2", text: "#24324C", muted: "#66728A", primary: "#E985A4", secondary: "#F3B6C6", success: "#5B8C7B", warning: "#F2A985", danger: "#CE5D73" },
  "volcanic-forge": { page: "#0C0E12", sidebar: "#080A0D", surface: "#171A1E", surfaceAlt: "#20252B", border: "#3D434A", text: "#F2F2EF", muted: "#A8ADB3", primary: "#FF6A1A", secondary: "#E23D2D", success: "#FF6A1A", warning: "#FFC14D", danger: "#FF4938" },
};

export function getThemePalette(id: ThemeId): ThemePalette {
  return { ...PALETTES[id] };
}

export function paletteTone(palette: ThemePalette): ThemeTone {
  const value = palette.page.slice(1);
  const channels = [0, 2, 4].map((offset) => Number.parseInt(value.slice(offset, offset + 2), 16) / 255);
  const linear = channels.map((channel) => channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4);
  const luminance = linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
  return luminance > 0.42 ? "light" : "dark";
}

export const DYNAMIC_THEME_IDS = [
  "sakura-valley-motion",
  "ocean-dawn-motion",
  "aurora-snowfield-motion",
] as const;

export type DynamicThemeId = (typeof DYNAMIC_THEME_IDS)[number];

export type DynamicThemeDefinition = {
  id: DynamicThemeId;
  name: string;
  description: string;
  baseThemeId: ThemeId;
  mark: string;
  poster: string;
  sources: readonly { src: string; type: "video/mp4" | "video/webm" }[];
};

export const DYNAMIC_THEMES: readonly DynamicThemeDefinition[] = [
  {
    id: "sakura-valley-motion",
    name: "樱花溪谷·流风",
    description: "晨雾、水光与落樱的无声循环",
    baseThemeId: "sakura-valley",
    mark: "花",
    poster: "/themes/dynamic/sakura-valley-poster.webp",
    sources: [
      { src: "/themes/dynamic/sakura-valley.webm", type: "video/webm" },
      { src: "/themes/dynamic/sakura-valley.mp4", type: "video/mp4" },
    ],
  },
  {
    id: "ocean-dawn-motion",
    name: "海洋晨光·潮汐",
    description: "海面呼吸、晨光游移的安静循环",
    baseThemeId: "ocean-dawn",
    mark: "潮",
    poster: "/themes/ocean-dawn.webp",
    sources: [
      { src: "/themes/dynamic/ocean-dawn.webm", type: "video/webm" },
      { src: "/themes/dynamic/ocean-dawn.mp4", type: "video/mp4" },
    ],
  },
  {
    id: "aurora-snowfield-motion",
    name: "极光雪原·流光",
    description: "极光舒展、雪粒轻落的夜空循环",
    baseThemeId: "aurora-snowfield",
    mark: "光",
    poster: "/themes/aurora-snowfield.webp",
    sources: [
      { src: "/themes/dynamic/aurora-snowfield.webm", type: "video/webm" },
      { src: "/themes/dynamic/aurora-snowfield.mp4", type: "video/mp4" },
    ],
  },
] as const;

export function isDynamicThemeId(value: unknown): value is DynamicThemeId {
  return typeof value === "string" && DYNAMIC_THEME_IDS.includes(value as DynamicThemeId);
}

export function getDynamicTheme(value: unknown): DynamicThemeDefinition | null {
  return DYNAMIC_THEMES.find((item) => item.id === value) || null;
}
