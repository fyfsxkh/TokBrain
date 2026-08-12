export type LibraryReturnContext = {
  workId: number;
  state: string;
  collectionId: number | null;
  scrollY: number;
  url: string;
  savedAt: number;
};

const STORAGE_KEY = "tokbrain.library-return.v1";
const MAX_AGE_MS = 24 * 60 * 60 * 1000;

export function rememberLibraryReturnContext(
  context: Omit<LibraryReturnContext, "savedAt">,
) {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ ...context, savedAt: Date.now() }),
    );
  } catch {
    // Browser history still preserves the previous page when storage is unavailable.
  }
}

export function readLibraryReturnContext(): LibraryReturnContext | null {
  if (typeof window === "undefined") return null;
  try {
    const parsed = JSON.parse(window.sessionStorage.getItem(STORAGE_KEY) || "null");
    if (
      !parsed
      || typeof parsed.workId !== "number"
      || typeof parsed.state !== "string"
      || typeof parsed.scrollY !== "number"
      || typeof parsed.url !== "string"
      || typeof parsed.savedAt !== "number"
      || Date.now() - parsed.savedAt > MAX_AGE_MS
    ) {
      window.sessionStorage.removeItem(STORAGE_KEY);
      return null;
    }
    return parsed as LibraryReturnContext;
  } catch {
    return null;
  }
}

export function clearLibraryReturnContext() {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Nothing else is required when storage is unavailable.
  }
}
