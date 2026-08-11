export const IMPORT_BATCH_STORAGE_KEY = "tokbrain.import-batches.v1";
export const LOCAL_IMPORT_BATCH_STORAGE_KEY = "tokbrain.local-import-batches.v1";
export const PACKAGE_IMPORT_STORAGE_KEY = "tokbrain.package-import-batches.v1";
export const LOCAL_IMPORT_RIGHTS_SESSION_KEY = "tokbrain.local-import-rights-attested.v1";

function storedBatchIds(storageKey: string, limit = 20) {
  try {
    const value = JSON.parse(window.localStorage.getItem(storageKey) || "[]");
    return Array.isArray(value)
      ? value.filter((item): item is string => typeof item === "string").slice(-limit)
      : [];
  } catch {
    return [];
  }
}

export function storedImportBatchIds() {
  return [...new Set([
    ...storedBatchIds(LOCAL_IMPORT_BATCH_STORAGE_KEY),
    ...storedBatchIds(IMPORT_BATCH_STORAGE_KEY),
  ])].slice(-20);
}

export function rememberImportBatch(batchId: string) {
  try {
    const ids = storedImportBatchIds().filter((item) => item !== batchId);
    window.localStorage.setItem(
      IMPORT_BATCH_STORAGE_KEY,
      JSON.stringify([...ids, batchId].slice(-20)),
    );
  } catch {
    // The active batch remains available in memory when storage is unavailable.
  }
}

export function rememberLocalImportBatch(batchId: string) {
  rememberImportBatch(batchId);
  try {
    const ids = storedBatchIds(LOCAL_IMPORT_BATCH_STORAGE_KEY).filter((item) => item !== batchId);
    window.localStorage.setItem(
      LOCAL_IMPORT_BATCH_STORAGE_KEY,
      JSON.stringify([...ids, batchId].slice(-20)),
    );
  } catch {
    // The active batch remains available in memory when storage is unavailable.
  }
}

export function rememberPackageBatch(batchId: string) {
  rememberImportBatch(batchId);
  try {
    const ids = storedBatchIds(PACKAGE_IMPORT_STORAGE_KEY, 10).filter((item) => item !== batchId);
    window.localStorage.setItem(
      PACKAGE_IMPORT_STORAGE_KEY,
      JSON.stringify([...ids, batchId].slice(-10)),
    );
  } catch {
    // Upload still works when browser storage is unavailable.
  }
}

export function storedPackageBatches() {
  return storedBatchIds(PACKAGE_IMPORT_STORAGE_KEY, 10);
}

export function storedLocalImportRightsAttestation() {
  try {
    return window.sessionStorage.getItem(LOCAL_IMPORT_RIGHTS_SESSION_KEY) === "true";
  } catch {
    return false;
  }
}

export function rememberLocalImportRightsAttestation(attested: boolean) {
  try {
    if (attested) {
      window.sessionStorage.setItem(LOCAL_IMPORT_RIGHTS_SESSION_KEY, "true");
    } else {
      window.sessionStorage.removeItem(LOCAL_IMPORT_RIGHTS_SESSION_KEY);
    }
  } catch {
    // Consent still works for this render when browser storage is unavailable.
  }
}
