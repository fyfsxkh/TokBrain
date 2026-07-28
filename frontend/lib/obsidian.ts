import { API_BASE, ObsidianManifest } from "./api";
import { mergeGeneratedMarkdown } from "./markdown";

declare global {
  interface Window {
    showDirectoryPicker?: (options?: {
      mode?: "read" | "readwrite";
      id?: string;
      startIn?: FileSystemHandle;
    }) => Promise<FileSystemDirectoryHandle>;
  }
}

type ExportResult = { succeeded: number; skipped: number; failed: number; messages: string[] };
export type ObsidianDestination = {
  imageDirectory: FileSystemDirectoryHandle;
  noteDirectory: FileSystemDirectoryHandle;
};
type IterableDirectory = FileSystemDirectoryHandle & {
  entries(): AsyncIterableIterator<[string, FileSystemHandle]>;
};
type PermissionDirectory = FileSystemDirectoryHandle & {
  queryPermission?: (options?: { mode?: "read" | "readwrite" }) => Promise<PermissionState>;
  requestPermission?: (options?: { mode?: "read" | "readwrite" }) => Promise<PermissionState>;
};

const HANDLE_DB = "shiguang-local-handles";
const HANDLE_STORE = "directory-handles";
const IMAGE_DIRECTORY_KEY = "obsidian-image-directory";
let sessionImageDirectory: FileSystemDirectoryHandle | null = null;

function openHandleDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(HANDLE_DB, 1);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(HANDLE_STORE)) {
        request.result.createObjectStore(HANDLE_STORE);
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("无法读取本地文件夹设置"));
  });
}

async function readSavedImageDirectory(): Promise<FileSystemDirectoryHandle | null> {
  if (sessionImageDirectory) return sessionImageDirectory;
  const database = await openHandleDatabase();
  try {
    return await new Promise((resolve, reject) => {
      const request = database.transaction(HANDLE_STORE).objectStore(HANDLE_STORE).get(IMAGE_DIRECTORY_KEY);
      request.onsuccess = () => resolve((request.result as FileSystemDirectoryHandle | undefined) || null);
      request.onerror = () => reject(request.error);
    });
  } finally {
    database.close();
  }
}

async function saveImageDirectory(handle: FileSystemDirectoryHandle) {
  sessionImageDirectory = handle;
  const database = await openHandleDatabase();
  try {
    await new Promise<void>((resolve, reject) => {
      const request = database.transaction(HANDLE_STORE, "readwrite").objectStore(HANDLE_STORE).put(handle, IMAGE_DIRECTORY_KEY);
      request.onsuccess = () => resolve();
      request.onerror = () => reject(request.error);
    });
  } finally {
    database.close();
  }
}

async function forgetSavedImageDirectory() {
  sessionImageDirectory = null;
  const database = await openHandleDatabase();
  try {
    await new Promise<void>((resolve, reject) => {
      const request = database.transaction(HANDLE_STORE, "readwrite").objectStore(HANDLE_STORE).delete(IMAGE_DIRECTORY_KEY);
      request.onsuccess = () => resolve();
      request.onerror = () => reject(request.error);
    });
  } finally {
    database.close();
  }
}

async function writeFile(handle: FileSystemFileHandle, value: string | Blob) {
  const writable = await handle.createWritable();
  await writable.write(value);
  await writable.close();
}

async function writeMarkdownFile(handle: FileSystemFileHandle, value: string) {
  const normalized = value.replace(/\r?\n/g, "\n");
  await writeFile(
    handle,
    new Blob(["\uFEFF", normalized], { type: "text/markdown;charset=utf-8" }),
  );
}

async function existingName(directory: FileSystemDirectoryHandle, platformId: string) {
  const suffix = `__douyin-${platformId}.md`;
  const iterable = directory as IterableDirectory;
  if (!iterable.entries) return null;
  for await (const [name, handle] of iterable.entries()) {
    if (handle.kind === "file" && name.endsWith(suffix)) return name;
  }
  return null;
}

async function cleanLegacyAssets(
  noteDirectory: FileSystemDirectoryHandle,
  platformId: string,
  exportNames: string[],
) {
  try {
    const assetsRoot = await noteDirectory.getDirectoryHandle("_assets");
    const shiguangRoot = await assetsRoot.getDirectoryHandle("拾光");
    for (const name of exportNames) {
      try { await shiguangRoot.removeEntry(name); } catch { /* The old flat file may not exist. */ }
    }
    try { await shiguangRoot.removeEntry(platformId, { recursive: true }); } catch { /* The old work folder may not exist. */ }
  } catch {
    // This destination was never exported with the legacy layout.
  }
}

export async function exportToObsidian(
  manifest: ObsidianManifest,
  onProgress?: (completed: number, total: number) => void,
  selectedDestination?: ObsidianDestination,
): Promise<ExportResult> {
  if (!window.showDirectoryPicker) throw new Error("当前浏览器不支持直接写入文件夹，请使用最新版 Chrome 或 Edge");
  const destination = selectedDestination || await pickObsidianDestination();
  const target = destination.noteDirectory;
  const sharedImages = destination.imageDirectory;
  const result: ExportResult = { succeeded: 0, skipped: 0, failed: 0, messages: [] };
  let completed = 0;
  for (const item of manifest.items) {
    try {
      const priorName = await existingName(target, item.platform_work_id);
      const filename = priorName || item.filename;
      const noteHandle = await target.getFileHandle(filename, { create: true });
      let markdown = item.markdown;
      if (priorName) {
        const existing = await (await noteHandle.getFile()).text();
        const merged = mergeGeneratedMarkdown(existing, item.markdown);
        if (merged == null) {
          result.skipped += 1;
          result.messages.push(`${filename}：已有文件没有 TokBrain 标记，已跳过`);
          completed += 1;
          onProgress?.(completed, manifest.items.length);
          continue;
        }
        markdown = merged;
      }
      for (const asset of item.assets) {
        const response = await fetch(`${API_BASE}${asset.url}`, { cache: "no-store" });
        if (!response.ok) throw new Error(`图片读取失败 (${response.status})`);
        const assetHandle = await sharedImages.getFileHandle(asset.export_name, { create: true });
        await writeFile(assetHandle, await response.blob());
      }
      await writeMarkdownFile(noteHandle, markdown);
      await cleanLegacyAssets(
        target,
        item.platform_work_id,
        item.assets.map((asset) => asset.export_name),
      );
      result.succeeded += 1;
    } catch (reason) {
      result.failed += 1;
      result.messages.push(`${item.filename}：${reason instanceof Error ? reason.message : "导出失败"}`);
    }
    completed += 1;
    onProgress?.(completed, manifest.items.length);
  }
  return result;
}

export async function pickObsidianDestination(): Promise<ObsidianDestination> {
  const imageDirectory = await getOrPickObsidianImageDirectory();
  const noteDirectory = await pickObsidianNoteDirectory();
  return { imageDirectory, noteDirectory };
}

export async function chooseObsidianImageDirectory(): Promise<FileSystemDirectoryHandle> {
  if (!window.showDirectoryPicker) throw new Error("当前浏览器不支持直接写入文件夹，请使用最新版 Chrome 或 Edge");
  const handle = await window.showDirectoryPicker({
    id: "shiguang-obsidian-images",
    mode: "readwrite",
  });
  await saveImageDirectory(handle);
  return handle;
}

export async function getOrPickObsidianImageDirectory(): Promise<FileSystemDirectoryHandle> {
  const saved = await readSavedImageDirectory().catch(() => null);
  if (saved) {
    const permissionHandle = saved as PermissionDirectory;
    const current = permissionHandle.queryPermission
      ? await permissionHandle.queryPermission({ mode: "readwrite" })
      : "granted";
    if (current === "granted") {
      sessionImageDirectory = saved;
      return saved;
    }
    if (current === "prompt" && permissionHandle.requestPermission) {
      const requested = await permissionHandle.requestPermission({ mode: "readwrite" });
      if (requested === "granted") {
        sessionImageDirectory = saved;
        return saved;
      }
    }
    await forgetSavedImageDirectory().catch(() => undefined);
  }
  return chooseObsidianImageDirectory();
}

export async function pickObsidianNoteDirectory(): Promise<FileSystemDirectoryHandle> {
  if (!window.showDirectoryPicker) throw new Error("当前浏览器不支持直接写入文件夹，请使用最新版 Chrome 或 Edge");
  return window.showDirectoryPicker({
    id: "shiguang-obsidian-notes",
    mode: "readwrite",
  });
}
