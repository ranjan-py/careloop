/**
 * Display helpers. Ages ("92 days old") are ALWAYS derived from stored
 * timestamps at render time — never copied into strings at seed time (spec §7).
 */

const DAY_MS = 24 * 60 * 60 * 1000;

export function daysAgo(iso: string): number | null {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.floor((Date.now() - t) / DAY_MS);
}

/** "today" | "N days old" | "~N months old" */
export function formatAge(iso: string | undefined): string {
  if (!iso) return "";
  const d = daysAgo(iso);
  if (d === null) return "";
  if (d <= 0) return "today";
  if (d === 1) return "1 day old";
  if (d < 120) return `${d} days old`;
  return `~${Math.round(d / 30)} months old`;
}

/** Timeline offset, e.g. "today − 92d" or "Today". */
export function formatOffset(iso: string): string {
  const d = daysAgo(iso);
  if (d === null) return "";
  if (d <= 0) return "Today";
  return `today − ${d}d`;
}

export function formatDate(iso: string | undefined): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  return new Date(t).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

export function formatTime(iso: string | undefined): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  return new Date(t).toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
  });
}

/** mm:ss elapsed clock. */
export function formatClock(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const m = Math.floor(s / 60);
  const rest = s % 60;
  return `${String(m).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

export function titleCase(value: string): string {
  return value
    .split(/[_\s]+/)
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(" ");
}
