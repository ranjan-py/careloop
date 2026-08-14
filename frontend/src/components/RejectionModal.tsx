"use client";

import { useState } from "react";
import type { CarePlanAction, RejectionCategory } from "@/lib/types";
import { REJECTION_CATEGORIES, REJECTION_CATEGORY_LABELS } from "@/lib/types";
import { EyebrowLabel, GhostButton, PillButton } from "./ui";

/**
 * Required rejection-reason modal (spec §14): the SINGLE shared 7-category
 * enum from contracts/CONTRACTS.md plus required free-text remarks.
 */
export function RejectionModal({
  action,
  busy,
  errorText,
  onSubmit,
  onClose,
}: {
  action: CarePlanAction;
  busy?: boolean;
  errorText?: string;
  onSubmit: (category: RejectionCategory, remarks: string) => void;
  onClose: () => void;
}) {
  const [category, setCategory] = useState<RejectionCategory | null>(null);
  const [remarks, setRemarks] = useState("");

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 p-6"
      role="dialog"
      aria-modal="true"
      aria-label="Reject action"
    >
      <div className="w-full max-w-lg rounded-2xl border border-hairline bg-card p-6 shadow-xl">
        <EyebrowLabel>Reject action</EyebrowLabel>
        <h3 className="mt-1 font-display text-xl">{action.title}</h3>
        <p className="mt-1 text-sm text-ink-muted">
          A category and remarks are required. The clinician-selected category
          is authoritative.
        </p>

        <fieldset className="mt-4">
          <legend className="sr-only">Rejection category</legend>
          <div className="space-y-1.5">
            {REJECTION_CATEGORIES.map((value) => (
              <label
                key={value}
                className={`flex cursor-pointer items-center gap-3 rounded-xl border px-4 py-2.5 text-sm transition-colors ${
                  category === value
                    ? "border-cta bg-cta-soft"
                    : "border-hairline hover:bg-well"
                }`}
              >
                <input
                  type="radio"
                  name="rejection-category"
                  value={value}
                  checked={category === value}
                  onChange={() => setCategory(value)}
                  className="accent-cta"
                />
                {REJECTION_CATEGORY_LABELS[value]}
              </label>
            ))}
          </div>
        </fieldset>

        <label className="mt-4 block text-sm">
          <span className="text-ink-muted">Remarks (required)</span>
          <textarea
            value={remarks}
            onChange={(e) => setRemarks(e.target.value)}
            rows={3}
            placeholder="e.g. Patient checks BP at a pharmacy kiosk occasionally; no home monitor for a twice-daily protocol."
            className="mt-1 w-full rounded-lg border border-hairline-strong bg-card px-3 py-2 text-sm outline-none focus:border-cta"
          />
        </label>

        {errorText && <p className="mt-3 text-sm text-bad">{errorText}</p>}

        <div className="mt-5 flex items-center justify-end gap-2">
          <GhostButton onClick={onClose} disabled={busy}>
            Cancel
          </GhostButton>
          <PillButton
            onClick={() => category && onSubmit(category, remarks.trim())}
            disabled={busy || !category || remarks.trim().length === 0}
          >
            Reject action
          </PillButton>
        </div>
      </div>
    </div>
  );
}
