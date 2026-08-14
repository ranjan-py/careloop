/** Persistent on EVERY screen (contracts/CONTRACTS.md cross-cutting rules). */
export function TopBanner() {
  return (
    <div
      role="note"
      className="w-full bg-ink px-4 py-1.5 text-center font-mono text-[11px] uppercase tracking-[0.14em] text-paper"
    >
      Synthetic clinical AI prototype — not for patient care.
    </div>
  );
}
