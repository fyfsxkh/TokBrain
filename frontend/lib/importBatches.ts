import type { ImportBatch } from "./contracts";

export function removeConfirmedImportItems(
  batch: ImportBatch,
  confirmedItemIds: ReadonlySet<number>,
): ImportBatch {
  return {
    ...batch,
    items: batch.items.filter((item) => !confirmedItemIds.has(item.id)),
  };
}
