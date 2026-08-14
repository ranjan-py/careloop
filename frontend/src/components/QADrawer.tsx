"use client";

import { useState } from "react";
import { EyebrowLabel, StatusChip } from "./ui";

const EXAMPLE_QUESTIONS = [
  "Why is the potassium age relevant?",
  "What information is still missing?",
  "Which patient facts produced this recommendation?",
  "What alternatives should be reviewed?",
];

/**
 * "Ask Clinical AI" drawer — Concept-preview UI ONLY (spec §11, decision
 * 2026-08-14). No model call is made from here; the P0 retrieval layer serves
 * care-plan evidence_refs instead. Never present static text as live output.
 */
export function QADrawer() {
  const [open, setOpen] = useState(false);

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="fixed bottom-6 right-6 z-40 inline-flex items-center gap-2 rounded-full border border-hairline-strong bg-card px-5 py-2.5 text-sm font-medium text-ink shadow-lg transition-colors hover:bg-well"
      >
        <span aria-hidden className="size-2 rounded-full bg-cta" />
        Ask Clinical AI
      </button>

      {open && (
        <div className="fixed inset-0 z-50 flex justify-end bg-ink/30">
          <button
            aria-label="Close drawer"
            className="flex-1 cursor-default"
            onClick={() => setOpen(false)}
          />
          <aside className="flex h-full w-full max-w-md flex-col border-l border-hairline bg-card p-6 shadow-2xl">
            <div className="flex items-start justify-between gap-4">
              <div>
                <EyebrowLabel>Ask Clinical AI</EyebrowLabel>
                <h2 className="mt-1 font-display text-2xl">Clinician Q&amp;A</h2>
              </div>
              <button
                onClick={() => setOpen(false)}
                className="rounded-full px-3 py-1 text-sm text-ink-muted hover:bg-well"
              >
                Close
              </button>
            </div>

            <div className="mt-4 rounded-xl border border-warn/30 bg-warn-soft p-4">
              <p className="font-mono text-[11px] uppercase tracking-[0.12em] text-warn">
                Concept preview — Q&amp;A not implemented in this demo
              </p>
              <p className="mt-2 text-sm text-ink-muted">
                This drawer illustrates the intended experience. No question is
                answered by a live model here. A working version would reuse the
                same retrieval layer that produces care-plan evidence, and every
                answer would show the patient facts used and its evidence
                references.
              </p>
            </div>

            <div className="mt-6">
              <EyebrowLabel>Example questions</EyebrowLabel>
              <ul className="mt-2 space-y-2">
                {EXAMPLE_QUESTIONS.map((q) => (
                  <li
                    key={q}
                    className="rounded-xl border border-hairline bg-well px-4 py-2.5 text-sm text-ink-muted"
                  >
                    {q}
                  </li>
                ))}
              </ul>
            </div>

            <div className="mt-auto pt-6">
              <input
                disabled
                placeholder="Q&A disabled in this prototype"
                className="w-full cursor-not-allowed rounded-full border border-hairline bg-well px-4 py-2.5 text-sm text-ink-faint"
              />
              <div className="mt-3">
                <StatusChip tone="neutral">
                  Demo only / not clinical advice
                </StatusChip>
              </div>
            </div>
          </aside>
        </div>
      )}
    </>
  );
}
