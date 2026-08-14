"use client";

import { useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import { QADrawer } from "@/components/QADrawer";
import { ErrorState, LoadingState } from "@/components/States";
import {
  Card,
  EyebrowLabel,
  PillButton,
  StatusChip,
  SyntheticBadge,
} from "@/components/ui";
import { api, describeError } from "@/lib/api";
import { rememberId, STORAGE_KEYS, useApi } from "@/lib/hooks";
import { formatAge, formatOffset } from "@/lib/format";
import type { RiskLevel } from "@/lib/types";

const SEVERITY_TONE: Record<RiskLevel, "bad" | "warn" | "neutral"> = {
  high: "bad",
  medium: "warn",
  low: "neutral",
};

export default function PatientOverviewPage() {
  const params = useParams<{ id: string }>();
  const patientId = params.id;
  const router = useRouter();

  const patient = useApi(() => api.getPatient(patientId), [patientId]);
  const timeline = useApi(() => api.getPatientTimeline(patientId), [patientId]);

  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  async function startVisit() {
    setStarting(true);
    setStartError(null);
    try {
      const { encounter } = await api.createEncounter(patientId);
      rememberId(STORAGE_KEYS.lastEncounterId, encounter.id);
      rememberId(STORAGE_KEYS.lastPatientId, patientId);
      // One-time WS ticket from POST /api/encounters — hand it to the live
      // page via sessionStorage (it is not retrievable again later).
      if (encounter.stream_ticket) {
        try {
          window.sessionStorage.setItem(
            `careloop.ticket.${encounter.id}`,
            encounter.stream_ticket,
          );
        } catch {
          /* storage unavailable — live page will surface the missing ticket */
        }
      }
      router.push(`/encounters/${encounter.id}/live`);
    } catch (err) {
      setStartError(describeError(err));
      setStarting(false);
    }
  }

  if (patient.loading) {
    return (
      <AppShell>
        <LoadingState label="Loading pre-visit intelligence…" />
      </AppShell>
    );
  }
  if (patient.error || !patient.data) {
    return (
      <AppShell>
        <ErrorState error={patient.error} onRetry={patient.reload} />
      </AppShell>
    );
  }

  const p = patient.data.patient;

  return (
    <AppShell>
      <div className="flex items-start justify-between gap-6">
        <div>
          <EyebrowLabel>
            Pre-visit intelligence — {p.name}, {p.age}
          </EyebrowLabel>
          <h1 className="mt-2 font-display text-5xl tracking-tight">
            What matters today?
          </h1>
          <div className="mt-3">
            <SyntheticBadge />
          </div>
        </div>
        <div className="shrink-0 pt-2 text-right">
          <PillButton onClick={startVisit} disabled={starting}>
            {starting ? "Starting…" : "Start Visit"}
          </PillButton>
          {startError && (
            <p className="mt-2 max-w-xs text-sm text-bad">{startError}</p>
          )}
        </div>
      </div>

      <div className="mt-8 grid grid-cols-1 gap-6 lg:grid-cols-3">
        {/* Priorities + chart facts */}
        <div className="space-y-6 lg:col-span-2">
          <Card>
            <EyebrowLabel>Priorities</EyebrowLabel>
            {p.priorities.length === 0 ? (
              <p className="mt-3 text-sm text-ink-muted">
                No prioritized items returned for this patient.
              </p>
            ) : (
              <ul className="mt-3 divide-y divide-hairline">
                {p.priorities.map((item) => (
                  <li key={item.id} className="flex items-start gap-3 py-3">
                    <StatusChip tone={SEVERITY_TONE[item.severity ?? "medium"]}>
                      {item.severity ?? "review"}
                    </StatusChip>
                    <div>
                      <p className="text-sm font-medium text-ink">{item.label}</p>
                      {item.detail && (
                        <p className="mt-0.5 text-sm text-ink-muted">
                          {item.detail}
                        </p>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
            <Card>
              <EyebrowLabel>Conditions</EyebrowLabel>
              <ul className="mt-3 space-y-2">
                {p.conditions.map((c) => (
                  <li key={c} className="text-sm text-ink">
                    {c}
                  </li>
                ))}
              </ul>
            </Card>
            <Card>
              <EyebrowLabel>Medications</EyebrowLabel>
              <ul className="mt-3 space-y-2">
                {p.medications.map((m) => (
                  <li
                    key={m.name}
                    className="flex items-center justify-between gap-2 text-sm"
                  >
                    <span>
                      {m.name}
                      {m.dose ? ` ${m.dose}` : ""}
                    </span>
                    {m.ehr_status && (
                      <StatusChip tone="neutral">{m.ehr_status}</StatusChip>
                    )}
                  </li>
                ))}
                {p.medications.length === 0 && (
                  <li className="text-sm text-ink-muted">None on record.</li>
                )}
              </ul>
            </Card>
            <Card>
              <EyebrowLabel>Labs</EyebrowLabel>
              <ul className="mt-3 space-y-2">
                {p.labs.map((lab) => (
                  <li
                    key={lab.name}
                    className="flex items-center justify-between gap-2 text-sm"
                  >
                    <span>
                      {lab.name}: {lab.value}
                      {lab.unit ? ` ${lab.unit}` : ""}
                    </span>
                    <span className="font-mono text-xs text-ink-faint">
                      {formatAge(lab.observed_at)}
                    </span>
                  </li>
                ))}
                {p.labs.length === 0 && (
                  <li className="text-sm text-ink-muted">No labs on record.</li>
                )}
              </ul>
            </Card>
            <Card>
              <EyebrowLabel>Care gaps</EyebrowLabel>
              <ul className="mt-3 space-y-2">
                {p.care_gaps.map((gap) => (
                  <li key={gap.id} className="text-sm">
                    <span className="text-ink">{gap.label}</span>
                    {gap.detail && (
                      <span className="text-ink-muted"> — {gap.detail}</span>
                    )}
                  </li>
                ))}
                {p.care_gaps.length === 0 && (
                  <li className="text-sm text-ink-muted">
                    No open care gaps returned.
                  </li>
                )}
              </ul>
            </Card>
          </div>
        </div>

        {/* Timeline */}
        <Card className="h-fit">
          <EyebrowLabel>Timeline</EyebrowLabel>
          {timeline.loading ? (
            <p className="mt-3 text-sm text-ink-muted">Loading timeline…</p>
          ) : timeline.error ? (
            <div className="mt-3">
              <ErrorState
                error={timeline.error}
                onRetry={timeline.reload}
                title="Timeline could not be loaded."
              />
            </div>
          ) : (
            <ol className="mt-4 space-y-0">
              {(timeline.data?.events ?? []).map((event, i, arr) => (
                <li key={event.id} className="relative flex gap-4 pb-5">
                  {i < arr.length - 1 && (
                    <span
                      aria-hidden
                      className="absolute left-[5px] top-4 h-full w-px bg-hairline-strong"
                    />
                  )}
                  <span
                    aria-hidden
                    className={`relative mt-1.5 size-[11px] shrink-0 rounded-full border-2 ${
                      formatOffset(event.occurred_at) === "Today"
                        ? "border-cta bg-cta"
                        : "border-hairline-strong bg-card"
                    }`}
                  />
                  <div>
                    <p className="font-mono text-[11px] uppercase tracking-wider text-ink-faint">
                      {formatOffset(event.occurred_at)}
                    </p>
                    <p className="text-sm font-medium text-ink">{event.label}</p>
                    {event.detail && (
                      <p className="text-sm text-ink-muted">{event.detail}</p>
                    )}
                  </div>
                </li>
              ))}
              {(timeline.data?.events ?? []).length === 0 && (
                <li className="text-sm text-ink-muted">No timeline events.</li>
              )}
            </ol>
          )}
        </Card>
      </div>

      <QADrawer />
    </AppShell>
  );
}
