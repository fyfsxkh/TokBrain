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
export type ThemeMotion = "precise" | "organic" | "fluid" | "celestial" | "paper";

export type ThemeCopy = {
  brandTagline: string;
  nav: {
    dashboard: string;
    library: string;
    chat: string;
    settings: string;
  };
  pages: {
    dashboard: string;
    library: string;
    chat: string;
    settings: string;
  };
  cta: string;
  flow: {
    eyebrow: string;
    title: string;
    description: string;
    stages: readonly [
      { title: string; description: string },
      { title: string; description: string },
      { title: string; description: string },
    ];
  };
  metrics: readonly [string, string, string, string];
  systemCheck: string;
  recentTasks: string;
};

export type ThemeDefinition = {
  id: ThemeId;
  name: string;
  description: string;
  tone: ThemeTone;
  mark: string;
  motion: ThemeMotion;
  preview: readonly [string, string, string, string];
  assets: {
    background: string | null;
    position: string;
  };
  copy: ThemeCopy;
};

const sharedCopy: ThemeCopy = {
  brandTagline: "本地公开内容知识库",
  nav: {
    dashboard: "链接导入",
    library: "知识库",
    chat: "对话",
    settings: "设置",
  },
  pages: {
    dashboard: "公开链接导入",
    library: "我的知识库",
    chat: "和本地知识对话",
    settings: "安全与处理设置",
  },
  cta: "开始预检",
  flow: {
    eyebrow: "用户主动处理路径",
    title: "从公开链接到可追溯知识",
    description: "逐条预检、明确确认，并保留每条知识的公开来源。",
    stages: [
      { title: "链接预检", description: "低频进行单作品解析" },
      { title: "用户确认", description: "选择作品或补充本地文件" },
      { title: "知识入库", description: "处理并建立可检索来源" },
    ],
  },
  metrics: ["待确认", "已入库", "处理异常", "任务状态"],
  systemCheck: "本地检查",
  recentTasks: "最近任务",
};

function theme(
  definition: Omit<ThemeDefinition, "assets" | "copy"> & {
    assets?: Partial<ThemeDefinition["assets"]>;
  },
): ThemeDefinition {
  return {
    ...definition,
    assets: {
      background:
        definition.assets?.background ??
        (definition.id === "classic-night" ? null : `/themes/${definition.id}.webp`),
      position: definition.assets?.position ?? "center",
    },
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
    motion: "precise",
    preview: ["#0d1013", "#15191d", "#36f0e0", "#f4f1ea"],
  }),
  theme({
    id: "dragonbone-biopunk",
    name: "龙骨生物朋克",
    description: "龙骨、神经纤维与琥珀胶囊",
    tone: "dark",
    mark: "脉",
    motion: "organic",
    preview: ["#0c0d0f", "#181313", "#ff315a", "#d8cbb7"],
  }),
  theme({
    id: "aether-sky-city",
    name: "以太浮空城",
    description: "明亮云海、黄铜星盘与水晶航路",
    tone: "light",
    mark: "✧",
    motion: "celestial",
    preview: ["#f5f1e8", "#ffffff", "#41c7f4", "#12233c"],
  }),
  theme({
    id: "abyssal-runepunk",
    name: "深渊符文",
    description: "深海玻璃、潮汐符文与珍珠存储",
    tone: "dark",
    mark: "◈",
    motion: "fluid",
    preview: ["#031219", "#08242c", "#35f2e6", "#e8f3ef"],
  }),
  theme({
    id: "paper-organizer",
    name: "纸面清新收纳",
    description: "暖象牙纸与轻盈编辑排版",
    tone: "light",
    mark: "▱",
    motion: "paper",
    preview: ["#f8f6f0", "#ffffff", "#9cb4a8", "#292929"],
  }),
  theme({
    id: "ocean-dawn",
    name: "海洋晨光",
    description: "海岸晨光与磨砂海玻璃",
    tone: "dark",
    mark: "≋",
    motion: "fluid",
    preview: ["#041b36", "#0d4261", "#28d8d1", "#f4fbff"],
  }),
  theme({
    id: "sunset-cloudsea",
    name: "晚霞云海",
    description: "金橙地平线与暮色玻璃",
    tone: "dark",
    mark: "●",
    motion: "celestial",
    preview: ["#171a3d", "#342044", "#ffb36b", "#fff0df"],
  }),
  theme({
    id: "aurora-snowfield",
    name: "极光雪原",
    description: "极夜冰湖与克制的双色极光",
    tone: "dark",
    mark: "✦",
    motion: "celestial",
    preview: ["#071426", "#0c2b48", "#38f29a", "#f3fbff"],
  }),
  theme({
    id: "eastern-mist",
    name: "东方雾山",
    description: "宣纸留白与现代中文编辑设计",
    tone: "light",
    mark: "山",
    motion: "paper",
    preview: ["#f5f1e7", "#fbf9f2", "#426b5a", "#1f2925"],
  }),
  theme({
    id: "moss-forest",
    name: "苔藓森林",
    description: "晨光溪流与生长型知识隐喻",
    tone: "dark",
    mark: "叶",
    motion: "organic",
    preview: ["#071d18", "#10372c", "#62d8c7", "#f3e9ce"],
  }),
  theme({
    id: "desert-observatory",
    name: "星夜沙漠",
    description: "沙金地平线与青铜观测刻线",
    tone: "dark",
    mark: "✷",
    motion: "celestial",
    preview: ["#10142c", "#1b1f3a", "#d8a657", "#efe2c4"],
  }),
  theme({
    id: "sakura-valley",
    name: "樱花溪谷",
    description: "樱粉晨雾与柔和溪流",
    tone: "light",
    mark: "花",
    motion: "organic",
    preview: ["#f7eced", "#fff9f7", "#f3b6c6", "#313552"],
  }),
  theme({
    id: "volcanic-forge",
    name: "熔岩火山",
    description: "玄武岩、黑曜石与受控熔岩通道",
    tone: "dark",
    mark: "熔",
    motion: "precise",
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
