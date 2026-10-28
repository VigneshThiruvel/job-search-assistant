export const REC_COLOR = { apply: "teal", maybe: "yellow", skip: "red" } as const;
export const STATUS_COLOR = {
  queued: "gray",
  selected: "blue",
  ready: "teal",
  applied: "violet",
  skipped: "red",
  screening: "cyan",
  interviewing: "indigo",
  offer: "lime",
  accepted: "green",
  declined: "gray",
  rejected: "red",
  withdrawn: "gray",
  ghosted: "orange",
} as const;
export const RUN_COLOR = {
  running: "blue",
  done: "teal",
  failed: "red",
  cancelled: "gray",
  interrupted: "orange",
} as const;

export function when(iso: string | null | undefined): string {
  if (!iso) return "–";
  const d = new Date(iso);
  const today = new Date();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === today.toDateString() ? `today ${time}` : `${d.toLocaleDateString()} ${time}`;
}

export function duration(start: string, end: string | null): string {
  const secs = Math.round(((end ? new Date(end) : new Date()).getTime() - new Date(start).getTime()) / 1000);
  return secs < 60 ? `${secs}s` : `${Math.floor(secs / 60)}m ${secs % 60}s`;
}

export function isToday(iso: string | null | undefined): boolean {
  return !!iso && new Date(iso).toDateString() === new Date().toDateString();
}
