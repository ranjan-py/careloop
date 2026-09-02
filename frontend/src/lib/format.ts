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

/** Date-only value ("1968-03-30"). Parsed as a LOCAL date: Date.parse() reads a
 * bare date as UTC midnight, which renders as the previous day anywhere west of
 * Greenwich — a date of birth must not drift. */
export function formatDateOnly(value: string | null | undefined): string {
  if (!value) return "";
  const [y, m, d] = value.split("-").map(Number);
  if (!y || !m || !d) return "";
  return new Date(y, m - 1, d).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
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

/**
 * Present an extracted fact subject/value to a clinician.
 *
 * Extraction emits machine tokens for enum-ish values —
 * "not_restarted", "fresh_labs_needed_before_next_medication_decision" — which
 * read as debug output on screen AND, having no spaces, are unbreakable by the
 * layout engine. Underscores become spaces and the first letter is capitalised;
 * everything else is left alone so "eGFR", "1000 mg", and "150/95 mmHg" survive
 * untouched. Callers keep the raw string available (title attribute) — this is
 * presentation only, never a change to the stored fact.
 */
export function humanizeFactText(value: string): string {
  const spaced = value.includes("_") ? value.replace(/_/g, " ") : value;
  // Capitalise ONLY when the leading word carries no casing of its own.
  // "eGFR" must never become "EGFR" — that is a different marker entirely —
  // and "A1C", "mmHg", "150/95" must survive untouched.
  const leadingWord = spaced.match(/^[A-Za-z]+/)?.[0];
  if (leadingWord && leadingWord === leadingWord.toLowerCase()) {
    return spaced.replace(/^[a-z]/, (c) => c.toUpperCase());
  }
  return spaced;
}

export function titleCase(value: string): string {
  return value
    .split(/[_\s]+/)
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(" ");
}
