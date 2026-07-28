export const BACKGROUND_INTENSITY_STORAGE_KEY = "tokbrain.background-intensity";
export const DEFAULT_BACKGROUND_INTENSITY = 72;

export function normalizeBackgroundIntensity(value: unknown) {
  if (value == null || value === "") return DEFAULT_BACKGROUND_INTENSITY;
  const numeric = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(numeric)) return DEFAULT_BACKGROUND_INTENSITY;
  return Math.min(100, Math.max(0, Math.round(numeric)));
}

export function backgroundOverlayOpacity(intensity: number) {
  const normalized = normalizeBackgroundIntensity(intensity);
  return Math.max(0.28, 1 - normalized * 0.0072);
}
