"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { ActionCard } from "@/components/ActionCard";
import { AppShell } from "@/components/AppShell";
import { QADrawer } from "@/components/QADrawer";
import { RejectionModal } from "@/components/RejectionModal";
import { ErrorState, LoadingState } from "@/components/States";
import { EyebrowLabel, PillButton, StatusChip } from "@/components/ui";
import { api, describeError } from "@/lib/api";
import { rememberId, STORAGE_KEYS, useApi } from "@/lib/hooks";
import type { CarePlan, CarePlanAction, RejectionCategory } from "@/lib/types";

export default function CarePlanPage() {
  const params = useParams<{ id: string }>();
  const encounterId = params.id;
  const router = useRouter();

  /**
   * CONTRACTS v1 has no GET route for a care plan; POST /encounters/{id}/end
   * is the only producer and is assumed idempotent for an already-ended
   * encounter (flagged as a contract ambiguity in the build report).
   */
  const ended = useApi(() => api.endEncounter(encounterId), [encounterId]);

  const [plan, setPlan] = useState<CarePlan | null>(null);
  const [rejecting, setRejecting] = useState<CarePlanAction | null>(null);
  const [busyActionId, setBusyActionId] = useState<string | null>(null);
  const [decisionError, setDecisionError] = useState<string | null>(null);
  const [finalizing, setFinalizing] = useState(false);

  useEffect(() => {
    if (ended.data) {
      setPlan(ended.data.care_plan);
      rememberId(STORAGE_KEYS.lastCarePlanId, ended.data.care_plan.id);
    }
  }, [ended.data]);

  function replaceAction(updated: CarePlanAction) {
    setPlan((prev) =>
      prev
        ? {
            ...prev,
            actions: prev.actions.map((a) =>
              a.id === updated.id ? updated : a,
            ),
          }
        : prev,
    );
  }

  async function withBusy(
    action: CarePlanAction,
    fn: () => Promise<{ action: CarePlanAction }>,
  ) {
    setBusyActionId(action.id);
    setDecisionError(null);
    try {
      const res = await fn();
      replaceAction(res.action);
    } catch (err) {
      setDecisionError(describeError(err));
    } finally {
      setBusyActionId(null);
    }
  }

  async function submitRejection(category: RejectionCategory, remarks: string) {
    if (!rejecting) return;
    const target = rejecting;
    setBusyActionId(target.id);
    setDecisionError(null);
    try {
      const res = await api.rejectAction(target.id, { category, remarks });
      replaceAction(res.action);
      setRejecting(null);
    } catch (err) {
      setDecisionError(describeError(err));
    } finally {
      setBusyActionId(null);
    }
  }

  async function finalize() {
    if (!plan) return;
    setFinalizing(true);
    setDecisionError(null);
    try {
      await api.finalizeCarePlan(plan.id);
      router.push(`/encounters/${encounterId}/summary`);
    } catch (err) {
      setDecisionError(describeError(err));
      setFinalizing(false);
    }
  }

  const pendingCount =
    plan?.actions.filter((a) => a.status === "pending").length ?? 0;

  return (
    <AppShell>
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <EyebrowLabel>Care plan review</EyebrowLabel>
          <h1 className="mt-1 font-display text-4xl tracking-tight">
            Actions for review
          </h1>
          <p className="mt-2 max-w-2xl text-sm text-ink-muted">
            Each Action is reviewed individually — approve, modify, or reject
            with a reason. These are synthetic demo actions, not clinically
            validated recommendations.
          </p>
        </div>
        {plan && (
          <div className="flex items-center gap-3">
            <StatusChip tone={pendingCount === 0 ? "good" : "neutral"}>
              {pendingCount === 0
                ? "All actions decided"
                : `${pendingCount} pending`}
            </StatusChip>
            <PillButton onClick={finalize} disabled={finalizing || pendingCount > 0}>
              {finalizing ? "Finalizing…" : "Finalize care plan"}
            </PillButton>
          </div>
        )}
      </div>

      {decisionError && (
        <p className="mt-4 rounded-xl border border-bad/30 bg-bad-soft px-4 py-2.5 text-sm text-bad">
          {decisionError}
        </p>
      )}

      <div className="mt-8">
        {ended.loading ? (
          <LoadingState label="Running end-of-encounter pipeline (summary, care plan, evals)…" />
        ) : ended.error ? (
          <ErrorState
            error={ended.error}
            onRetry={ended.reload}
            title="The care plan could not be generated or loaded."
          />
        ) : plan && plan.actions.length > 0 ? (
          <div className="space-y-5">
            {plan.actions.map((action) => (
              <ActionCard
                key={action.id}
                action={action}
                busy={busyActionId === action.id}
                onApprove={(a) => withBusy(a, () => api.approveAction(a.id))}
                onModify={(a, body) =>
                  withBusy(a, () => api.modifyAction(a.id, body))
                }
                onReject={(a) => setRejecting(a)}
              />
            ))}
          </div>
        ) : (
          <p className="text-sm text-ink-muted">
            The care plan for this encounter contains no actions.
          </p>
        )}
      </div>

      {rejecting && (
        <RejectionModal
          action={rejecting}
          busy={busyActionId === rejecting.id}
          errorText={decisionError ?? undefined}
          onSubmit={submitRejection}
          onClose={() => setRejecting(null)}
        />
      )}

      <QADrawer />
    </AppShell>
  );
}
