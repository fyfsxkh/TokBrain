import { API_BASE } from "./api";

export function localAssetUrl(value?: string | null) {
  return value?.startsWith("/api/") ? `${API_BASE}${value}` : null;
}

export function resolvedAssetUrl(value: string) {
  return value.startsWith("/api/") ? `${API_BASE}${value}` : value;
}
