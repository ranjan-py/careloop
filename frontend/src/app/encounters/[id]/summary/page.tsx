"use client";

import { useParams } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import { ErrorState, LoadingState } from "@/components/States";
import { Card, EyebrowLabel, StatusChip } from "@/components/ui";
import { api } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import type { ExecutionRecord } from "@/lib/types";

function executionGlyph(status: string): { glyph: string; ok: boolean } {
  const s = status.toLowerCase();
  if (["executed", "success", "succeeded", "ok", "done", "completed"].includes(s))
    return { glyph: "✓", ok: true };
  return { glyph: "⊘", ok: false };
}

export default function SummaryPage() {
  const params = useParams<{ id: string }>();
  const encounterId = params.id;

  /**
   * CONTRACTS v1 has no GET route for finalized summaries — the finalize POST
   * is the only producer. This page re-runs end + finalize and relies on both
   * being idempotent for an already-finalized encounter (flagged as a
   * contract ambiguity in the build report).
   */
  const summary = useApi(async () => {
    const ended = await api.endEncounter(encounterId);
    return api.finalizeCarePlan(ended.care_plan.id);
  }, [encounterId]);

  if (summary.loading) {
    return (
      <AppShell>
        <LoadingState label="Loading finalized summary and execution results…" />
      </AppShell>
    );
  }
  if (summary.error || !summary.data) {
    return (
      <AppShell>
        <ErrorState
          error={summary.error}
          onRetry={summary.reload}
          title="The finalized summary could not be loaded."
        />
      </AppShell>
    );
  }

  const data = summary.data;
  const executions: ExecutionRecord[] = data.executions ?? [];

  return (
    <AppShell>
      <EyebrowLabel>Visit complete</EyebrowLabel>
      <h1 className="mt-1 font-display text-4xl tracking-tight">
        Encounter summary
      </h1>
      <p className="mt-2 text-sm text-ink-muted">
        Rendered from the clinician&rsquo;s final decisions — modified actions
        appear in their final form; rejected actions are excluded.
      </p>

      <div className="mt-8 grid grid-cols-1 gap-6 xl:grid-cols-3">
        <Card className="xl:col-span-1">
          <EyebrowLabel>Care plan execution</EyebrowLabel>
          {executions.length === 0 ? (
            <p className="mt-3 text-sm text-ink-muted">
              No tool executions were recorded for this care plan.
            </p>
          ) : (
            <ul className="mt-4 space-y-3">
              {executions.map((ex) => {
                const { glyph, ok } = executionGlyph(ex.status);
                return (
                  <li key={ex.id} className="flex items-start gap-3">
                    <span
                      aria-hidden
                      className={`mt-0.5 font-mono text-sm ${ok ? "text-good" : "text-ink-faint"}`}
                    >
                      {glyph}
                    </span>
                    <div>
                      <p className="text-sm text-ink">{ex.tool_name}</p>
                      <p className="font-mono text-[11px] text-ink-faint">
                        {ex.permission_tier} · {ex.status}
                      </p>
                      {ex.error && (
                        <p className="mt-0.5 text-xs text-bad">{ex.error}</p>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
          <div className="mt-5">
            <StatusChip tone="warn">
              Demo execution — no real clinical order was placed.
            </StatusChip>
          </div>
        </Card>

        <Card className="xl:col-span-1">
          <EyebrowLabel>Clinician summary</EyebrowLabel>
          <div className="mt-3 whitespace-pre-wrap text-sm leading-relaxed text-ink">
            {data.clinician_summary || (
              <span className="text-ink-muted">
                No clinician summary was returned.
              </span>
            )}
          </div>
        </Card>

        <Card className="xl:col-span-1">
          <EyebrowLabel>Patient instructions</EyebrowLabel>
          <div className="mt-3 whitespace-pre-wrap font-display text-[15px] leading-relaxed text-ink">
            {data.patient_instructions || (
              <span className="font-body text-sm text-ink-muted">
                No patient instructions were returned.
              </span>
            )}
          </div>
          <div className="mt-4">
            <StatusChip tone="neutral">
              Demo only / not clinical advice
            </StatusChip>
          </div>
        </Card>
      </div>
    </AppShell>
  );
}
