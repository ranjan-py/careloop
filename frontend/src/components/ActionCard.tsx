"use client";

import { useEffect, useState } from "react";
import type { CarePlanAction, EvidenceSnippet } from "@/lib/types";
import { REJECTION_CATEGORY_LABELS } from "@/lib/types";
import { api, describeError } from "@/lib/api";
import { titleCase } from "@/lib/format";
import { EyebrowLabel, GhostButton, Hairline, PillButton, StatusChip } from "./ui";
import type { ChipTone } from "./ui";

const RISK_TONES: Record<CarePlanAction["risk_level"], ChipTone> = {
  low: "good",
  medium: "warn",
  high: "bad",
};

const PERMISSION_LABELS: Record<CarePlanAction["permission"], string> = {
  auto_demo: "Executed automatically — review",
  clinician_review: "Clinician review",
  required_clinician_decision: "Requires clinician decision",
};

const STATUS_TONES: Record<CarePlanAction["status"], ChipTone> = {
  pending: "neutral",
  approved: "good",
  modified: "info",
  rejected: "bad",
  executed: "good",
  denied: "bad",
};

export interface ActionCardProps {
  action: CarePlanAction;
  onApprove: (action: CarePlanAction) => void;
  onModify: (
    action: CarePlanAction,
    body: { final_title: string; final_description: string; reason: string },
  ) => void;
  onReject: (action: CarePlanAction) => void;
  busy?: boolean;
}

/**
 * Per-action review card (spec §13): title/description/rationale plus the
 * Evidence, Approve, Modify, Reject controls. Never approve/reject a plan as
 * one opaque block.
 */
export function ActionCard({
  action,
  onApprove,
  onModify,
  onReject,
  busy,
}: ActionCardProps) {
  const [showEvidence, setShowEvidence] = useState(false);
  const [snippets, setSnippets] = useState<EvidenceSnippet[] | null>(null);
  const [snippetsError, setSnippetsError] = useState<string | null>(null);
  const [showModify, setShowModify] = useState(false);
  const [finalTitle, setFinalTitle] = useState(action.title);
  const [finalDescription, setFinalDescription] = useState(action.description);
  const [reason, setReason] = useState("");

  /* Resolve evidence ids to snippets when the panel is opened. */
  useEffect(() => {
    if (!showEvidence || action.evidence_refs.length === 0 || snippets !== null)
      return;
    let cancelled = false;
    setSnippetsError(null);
    api
      .getEvidence(action.evidence_refs)
      .then((res) => {
        if (!cancelled) setSnippets(res.snippets);
      })
      .catch((err) => {
        if (!cancelled) setSnippetsError(describeError(err));
      });
    return () => {
      cancelled = true;
    };
  }, [showEvidence, action.evidence_refs, snippets]);

  const decided = action.status !== "pending";
  const displayTitle = action.decision?.final_title ?? action.title;
  const displayDescription =
    action.decision?.final_description ?? action.description;

  return (
    <article className="rounded-2xl border border-hairline bg-card p-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <EyebrowLabel>{titleCase(action.category)}</EyebrowLabel>
          <h3 className="mt-1 font-display text-xl text-ink">{displayTitle}</h3>
        </div>
        <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">
          <StatusChip tone={RISK_TONES[action.risk_level]}>
            {titleCase(action.risk_level)} risk
          </StatusChip>
          <StatusChip tone={action.permission === "required_clinician_decision" ? "warn" : "neutral"}>
            {PERMISSION_LABELS[action.permission]}
          </StatusChip>
          <StatusChip tone={STATUS_TONES[action.status]}>
            {titleCase(action.status)}
          </StatusChip>
        </div>
      </div>

      <p className="mt-3 text-sm leading-relaxed text-ink">{displayDescription}</p>

      <div className="mt-4 rounded-xl bg-well p-4">
        <EyebrowLabel>Rationale</EyebrowLabel>
        <p className="mt-1 text-sm leading-relaxed text-ink-muted">
          {action.rationale}
        </p>
      </div>

      {action.status === "modified" && action.decision?.reason && (
        <p className="mt-3 text-sm text-ink-muted">
          <span className="font-medium text-ink">Modified — reason:</span>{" "}
          {action.decision.reason}
        </p>
      )}
      {action.status === "rejected" && action.decision && (
        <div className="mt-3 rounded-xl border border-bad/20 bg-bad-soft/50 p-4 text-sm">
          <p>
            <span className="font-medium">Rejected —</span>{" "}
            {action.decision.category
              ? REJECTION_CATEGORY_LABELS[action.decision.category]
              : "category unavailable"}
          </p>
          {action.decision.remarks && (
            <p className="mt-1 text-ink-muted">{action.decision.remarks}</p>
          )}
          <p className="mt-2 text-xs text-ink-faint">
            Feedback captured. No automatic model change is made.
          </p>
        </div>
      )}

      {showEvidence && (
        <div className="mt-4 rounded-xl border border-hairline p-4">
          <EyebrowLabel>Evidence</EyebrowLabel>
          {action.evidence_refs.length === 0 ? (
            <p className="mt-2 text-sm text-ink-muted">
              No retrieved evidence attached to this action.
            </p>
          ) : snippetsError ? (
            <>
              <p className="mt-2 text-sm text-bad">
                Evidence snippets could not be loaded: {snippetsError}
              </p>
              <ul className="mt-2 space-y-1">
                {action.evidence_refs.map((ref) => (
                  <li key={ref} className="font-mono text-xs text-ink-muted">
                    {ref}
                  </li>
                ))}
              </ul>
            </>
          ) : snippets === null ? (
            <p className="mt-2 text-sm text-ink-muted">Loading evidence…</p>
          ) : (
            <ul className="mt-2 space-y-3">
              {snippets.map((s) => (
                <li key={s.id}>
                  <p className="text-sm font-medium text-ink">{s.title}</p>
                  <p className="mt-0.5 text-sm leading-relaxed text-ink-muted">
                    {s.body}
                  </p>
                </li>
              ))}
            </ul>
          )}
          <p className="mt-3 text-xs text-ink-faint">
            Retrieved from the local synthetic guideline corpus (spec §11).
          </p>
          {action.patient_facts_used.length > 0 && (
            <>
              <Hairline className="my-3" />
              <EyebrowLabel>Patient facts used</EyebrowLabel>
              <ul className="mt-2 space-y-1">
                {action.patient_facts_used.map((factId) => (
                  <li key={factId} className="font-mono text-xs text-ink-muted">
                    {factId}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}

      {showModify && !decided && (
        <form
          className="mt-4 space-y-3 rounded-xl border border-hairline p-4"
          onSubmit={(e) => {
            e.preventDefault();
            onModify(action, {
              final_title: finalTitle,
              final_description: finalDescription,
              reason,
            });
          }}
        >
          <EyebrowLabel>Modify action</EyebrowLabel>
          <label className="block text-sm">
            <span className="text-ink-muted">Final title</span>
            <input
              value={finalTitle}
              onChange={(e) => setFinalTitle(e.target.value)}
              required
              className="mt-1 w-full rounded-lg border border-hairline-strong bg-card px-3 py-2 text-sm outline-none focus:border-cta"
            />
          </label>
          <label className="block text-sm">
            <span className="text-ink-muted">Final description</span>
            <textarea
              value={finalDescription}
              onChange={(e) => setFinalDescription(e.target.value)}
              required
              rows={3}
              className="mt-1 w-full rounded-lg border border-hairline-strong bg-card px-3 py-2 text-sm outline-none focus:border-cta"
            />
          </label>
          <label className="block text-sm">
            <span className="text-ink-muted">Reason for modification (required)</span>
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              required
              rows={2}
              className="mt-1 w-full rounded-lg border border-hairline-strong bg-card px-3 py-2 text-sm outline-none focus:border-cta"
            />
          </label>
          <div className="flex gap-2">
            <PillButton type="submit" disabled={busy || !reason.trim()}>
              Save modification
            </PillButton>
            <GhostButton onClick={() => setShowModify(false)}>Cancel</GhostButton>
          </div>
        </form>
      )}

      <Hairline className="my-4" />

      <div className="flex flex-wrap items-center gap-2">
        <GhostButton onClick={() => setShowEvidence((v) => !v)}>
          {showEvidence ? "Hide evidence" : "Evidence"}
        </GhostButton>
        {!decided && (
          <>
            <PillButton onClick={() => onApprove(action)} disabled={busy}>
              Approve
            </PillButton>
            <GhostButton onClick={() => setShowModify((v) => !v)} disabled={busy}>
              Modify
            </GhostButton>
            <button
              onClick={() => onReject(action)}
              disabled={busy}
              className="inline-flex items-center rounded-full border border-bad/40 px-5 py-2 text-sm font-medium text-bad transition-colors hover:bg-bad-soft disabled:opacity-40"
            >
              Reject
            </button>
          </>
        )}
      </div>
    </article>
  );
}
