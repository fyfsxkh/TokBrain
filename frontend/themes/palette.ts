import type { ThemePalette } from "./registry";

function channel(value: number) {
  const normalized = value / 255;
  return normalized <= 0.04045
    ? normalized / 12.92
    : ((normalized + 0.055) / 1.055) ** 2.4;
}

export function relativeLuminance(color: string) {
  const hex = color.replace(/^#/, "");
  if (!/^[0-9a-fA-F]{6}$/.test(hex)) return 0;
  const red = channel(Number.parseInt(hex.slice(0, 2), 16));
  const green = channel(Number.parseInt(hex.slice(2, 4), 16));
  const blue = channel(Number.parseInt(hex.slice(4, 6), 16));
  return red * 0.2126 + green * 0.7152 + blue * 0.0722;
}

export function contrastRatio(foreground: string, background: string) {
  const lighter = Math.max(relativeLuminance(foreground), relativeLuminance(background));
  const darker = Math.min(relativeLuminance(foreground), relativeLuminance(background));
  return (lighter + 0.05) / (darker + 0.05);
}

export function paletteContrastWarnings(palette: ThemePalette) {
  const checks: Array<[string, keyof ThemePalette, keyof ThemePalette, number]> = [
    ["主文字 / 页面", "text", "page", 4.5],
    ["主文字 / 卡片", "text", "surface", 4.5],
    ["次要文字 / 卡片", "muted", "surface", 4.5],
    ["主强调色 / 卡片", "primary", "surface", 3],
    ["危险色 / 卡片", "danger", "surface", 3],
  ];
  return checks.flatMap(([label, foreground, background, minimum]) => {
    const ratio = contrastRatio(palette[foreground], palette[background]);
    return ratio < minimum ? [`${label} 对比度 ${ratio.toFixed(2)}:1，建议至少 ${minimum}:1`] : [];
  });
}
