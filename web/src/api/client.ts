// Thin typed wrapper around the FastAPI backend. Types come from the backend's OpenAPI
// schema (`npm run gen:api`), so a renamed field breaks the TypeScript build, not the UI.
import type { components } from "./schema";

type S = components["schemas"];
export type Card = S["Card"];
export type Detail = S["Detail"];
export type InboxItem = S["InboxItem"];
export type StatusCounts = S["StatusCounts"];
export type Paste = S["Paste"];
export type RunInfo = S["RunInfo"];
export type RunLog = S["RunLog"];
export type RunKind = RunInfo["kind"];
export type Health = S["Health"];
export type Coverage = S["Coverage"];
export type Fit = S["Fit"];
export type Insight = S["Insight"];
export type SkillReview = S["SkillReview"];
export type AlertFunnel = S["AlertFunnel"];
export type TodayQueue = S["TodayQueue"];
export type AlertJob = S["AlertJob"];
export type Status = Card["status"];
export type InsightKind = "gaps" | "unlock" | "coverage" | "roles" | "together";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body.detail;
    throw new Error(typeof detail === "string" ? detail : response.statusText);
  }
  return body as T;
}

// Writes are JSON: the backend rejects anything else (cross-site form protection).
function post<T>(path: string, body: object = {}): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export const api = {
  status: () => request<StatusCounts>("/api/status"),
  health: () => request<Health>("/api/health"),
  applications: (status?: Status) =>
    request<Card[]>(`/api/applications${status ? `?status=${status}` : ""}`),
  application: (jobId: number) => request<Detail>(`/api/applications/${jobId}`),
  fit: (jobId: number) => request<Fit>(`/api/applications/${jobId}/fit`),
  coverage: (ids: number[]): Promise<Record<string, Coverage>> =>
    ids.length ? request(`/api/coverage?ids=${ids.join(",")}`) : Promise.resolve({}),
  select: (jobId: number, letter: boolean, note: string | null) =>
    post<Card>(`/api/applications/${jobId}/select`, { letter, note }),
  letter: (jobId: number, note: string | null) => post<Card>(`/api/applications/${jobId}/letter`, { note }),
  skip: (jobId: number, note: string | null) => post<Card>(`/api/applications/${jobId}/skip`, { note }),
  applied: (jobId: number, note: string | null) => post<Card>(`/api/applications/${jobId}/applied`, { note }),
  inbox: () => request<InboxItem[]>("/api/inbox"),
  alerts: () => request<AlertFunnel>("/api/alerts"),
  queue: () => request<TodayQueue>("/api/queue"),
  dismissOlder: () => post<{ detail: string }>("/api/queue/dismiss-older"),
  paste: (jobId: number, paste: Paste) => post<{ detail: string }>(`/api/inbox/${jobId}`, paste),
  resumePdfUrl: (jobId: number) => `/api/applications/${jobId}/resume.pdf`,
  runs: () => request<RunInfo[]>("/api/runs"),
  run: (id: number, offset: number) => request<RunLog>(`/api/runs/${id}?offset=${offset}`),
  startRun: (kind: RunKind, k = 10, maxExtract = 30) =>
    post<RunInfo>("/api/runs", { kind, k, max_extract: maxExtract }),
  cancelRun: (id: number) => post<RunInfo>(`/api/runs/${id}/cancel`),
  insights: (kind: InsightKind, scope: "eligible" | "target", k = 20, skill?: string) =>
    request<Insight>(
      `/api/skills/insights?kind=${kind}&scope=${scope}&k=${k}${skill ? `&skill=${encodeURIComponent(skill)}` : ""}`,
    ),
  skillReview: () => request<SkillReview>("/api/skills/review"),
  exportSkills: () => post<{ detail: string }>("/api/skills/export"),
};
