// Thin typed wrapper around the FastAPI backend. Types come from the backend's OpenAPI
// schema (`npm run gen:api`), so a renamed field breaks the TypeScript build, not the UI.
import type { components } from "./schema";

export type Card = components["schemas"]["Card"];
export type Detail = components["schemas"]["Detail"];
export type InboxItem = components["schemas"]["InboxItem"];
export type StatusCounts = components["schemas"]["StatusCounts"];
export type Paste = components["schemas"]["Paste"];
export type Status = Card["status"];

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(typeof body.detail === "string" ? body.detail : response.statusText);
  }
  return body as T;
}

// Writes are JSON: the backend rejects anything else (cross-site form protection).
function post<T>(path: string, body: object): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export const api = {
  status: () => request<StatusCounts>("/api/status"),
  applications: (status?: Status) =>
    request<Card[]>(`/api/applications${status ? `?status=${status}` : ""}`),
  application: (jobId: number) => request<Detail>(`/api/applications/${jobId}`),
  select: (jobId: number, letter: boolean, note: string | null) =>
    post<Card>(`/api/applications/${jobId}/select`, { letter, note }),
  letter: (jobId: number, note: string | null) =>
    post<Card>(`/api/applications/${jobId}/letter`, { note }),
  skip: (jobId: number, note: string | null) =>
    post<Card>(`/api/applications/${jobId}/skip`, { note }),
  applied: (jobId: number, note: string | null) =>
    post<Card>(`/api/applications/${jobId}/applied`, { note }),
  inbox: () => request<InboxItem[]>("/api/inbox"),
  paste: (jobId: number, paste: Paste) => post<{ detail: string }>(`/api/inbox/${jobId}`, paste),
  resumePdfUrl: (jobId: number) => `/api/applications/${jobId}/resume.pdf`,
};
