import type { WorksPage } from "./contracts";

export function mergeWorksPage(
  current: WorksPage,
  incoming: WorksPage,
  append: boolean,
): WorksPage {
  if (!append) return incoming;
  const known = new Set(current.items.map((work) => work.id));
  return {
    ...incoming,
    items: [
      ...current.items,
      ...incoming.items.filter((work) => !known.has(work.id)),
    ],
  };
}
