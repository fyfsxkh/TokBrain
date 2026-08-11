export type LibraryState = "pending" | "in_library" | "supplement" | "issues" | "archived";

export type PerformOperation = (
  name: string,
  operation: () => Promise<unknown>,
  success: string,
) => Promise<boolean>;
