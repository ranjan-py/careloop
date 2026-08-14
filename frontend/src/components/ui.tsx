import type { ReactNode } from "react";

/* ------------------------------- Card ---------------------------------- */

export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-2xl border border-hairline bg-card p-6 shadow-[0_1px_2px_rgba(27,36,48,0.04)] ${className}`}
    >
      {children}
    </section>
  );
}

/* ---------------------------- EyebrowLabel ------------------------------ */

export function EyebrowLabel({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`font-mono text-[11px] font-medium uppercase tracking-[0.16em] text-ink-muted ${className}`}
    >
      {children}
    </div>
  );
}

/* ----------------------------- StatusChip ------------------------------- */

export type ChipTone = "good" | "bad" | "warn" | "info" | "neutral";

const CHIP_TONES: Record<ChipTone, string> = {
  good: "bg-good-soft text-good",
  bad: "bg-bad-soft text-bad",
  warn: "bg-warn-soft text-warn",
  info: "bg-cta-soft text-cta",
  neutral: "bg-well text-ink-muted",
};

export function StatusChip({
  tone = "neutral",
  children,
  className = "",
}: {
  tone?: ChipTone;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-medium ${CHIP_TONES[tone]} ${className}`}
    >
      {children}
    </span>
  );
}

/* --------------------------- SyntheticBadge ----------------------------- */

/** Required on patient pages (contracts/CONTRACTS.md cross-cutting rules). */
export function SyntheticBadge({ className = "" }: { className?: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border border-hairline-strong bg-well px-3 py-1 font-mono text-[11px] uppercase tracking-[0.08em] text-ink-muted ${className}`}
    >
      <span aria-hidden className="size-1.5 rounded-full bg-warn" />
      Synthetic patient — Synthea-generated FHIR R4
    </span>
  );
}

/* ------------------------------ Buttons --------------------------------- */

/** The one strong blue pill CTA. */
export function PillButton({
  children,
  onClick,
  disabled,
  type = "button",
  className = "",
}: {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  type?: "button" | "submit";
  className?: string;
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`inline-flex items-center justify-center gap-2 rounded-full bg-cta px-6 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-cta-hover disabled:cursor-not-allowed disabled:opacity-40 ${className}`}
    >
      {children}
    </button>
  );
}

export function GhostButton({
  children,
  onClick,
  disabled,
  type = "button",
  className = "",
}: {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  type?: "button" | "submit";
  className?: string;
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`inline-flex items-center justify-center gap-2 rounded-full border border-hairline-strong bg-card px-5 py-2 text-sm font-medium text-ink transition-colors hover:bg-well disabled:cursor-not-allowed disabled:opacity-40 ${className}`}
    >
      {children}
    </button>
  );
}

/* ------------------------------ Hairline -------------------------------- */

export function Hairline({ className = "" }: { className?: string }) {
  return <hr className={`border-0 border-t border-hairline ${className}`} />;
}
