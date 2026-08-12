import type { Job } from "./contracts";

const ACTIVE_JOB_STATES = new Set(["queued", "running", "cancelling"]);

export function isActiveProcessingJob(job: Job) {
  return ACTIVE_JOB_STATES.has(job.state) && job.job_type !== "link_preview";
}

export function activeProcessingJobIds(jobs: Job[]) {
  return new Set(
    jobs
      .filter(isActiveProcessingJob)
      .map((job) => job.id),
  );
}

export function didAnyJobReachTerminal(previous: Job[], next: Job[]) {
  const previousActive = activeProcessingJobIds(previous);
  if (!previousActive.size) return false;
  const nextActive = activeProcessingJobIds(next);
  return [...previousActive].some((id) => !nextActive.has(id));
}

function isJobPayload(value: unknown): value is Pick<Job, "id" | "job_type" | "state"> {
  if (value === null || typeof value !== "object") return false;
  const candidate = value as Record<string, unknown>;
  return typeof candidate.id === "string"
    && typeof candidate.job_type === "string"
    && typeof candidate.state === "string";
}

export function operationIncludesJob(value: unknown) {
  if (isJobPayload(value)) return true;
  if (value === null || typeof value !== "object") return false;
  return isJobPayload((value as Record<string, unknown>).job);
}
