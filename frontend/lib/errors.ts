export function isUserCancelled(value: unknown) {
  if (!(value instanceof Error)) return false;
  return (
    value.name === "AbortError"
    || /user aborted a request|request was aborted|operation was aborted/i.test(value.message)
  );
}
