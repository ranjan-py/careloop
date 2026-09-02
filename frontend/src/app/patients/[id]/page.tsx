"use client";

import { useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import { QADrawer } from "@/components/QADrawer";
import { ErrorState, LoadingState } from "@/components/States";
import {
  BloodPressurePlot,
  ReferenceMeter,
  StatTile,
  numericValue,
  referenceFor,
} from "@/components/charts";
import {
  Card,
  EyebrowLabel,
  Hairline,
  PillButton,
  StatusChip,
  SyntheticBadge,
} from "@/components/ui";
import { api, describeError } from "@/lib/api";
import { rememberId, STORAGE_KEYS, useApi } from "@/lib/hooks";
import {
  daysAgo,
  formatAge,
  formatDate,
  formatDateOnly,
  formatOffset,
  formatTime,
  titleCase,
} from "@/lib/format";
import type { LabSummary, RiskLevel, TimelineEvent } from "@/lib/types";

const SEVERITY_TONE: Record<RiskLevel, "bad" | "warn" | "neutral"> = {
  high: "bad",
  medium: "warn",
  low: "neutral",
};

const EVENT_LABELS: Record<string, string> = {
  lab: "Lab",
  vital: "Vital",
  medication: "Medication",
  condition: "Condition",
  encounter: "Visit",
  care_gap: "Care gap",
  note: "Note",
};

/** A lab an encounter produced is not a chart result — the pre-visit chart
 * must show which is which (spec §7, contract v2.1). */
function isChartLab(lab: LabSummary): boolean {
  return lab.source_type === "ehr" && !lab.encounter_id;
}

function ProvenanceChip({ lab }: { lab: LabSummary }) {
  if (isChartLab(lab)) {
    return <StatusChip tone="neutral">EHR</StatusChip>;
  }
  return (
    <StatusChip tone="info">
      Patient-reported{lab.method ? ` · ${lab.method.replace(/_/g, " ")}` : ""}
    </StatusChip>
  );
}

/** Field row for the demographics grid. */
function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">
        {label}
      </dt>
      <dd className="mt-1 text-sm text-ink">{value}</dd>
    </div>
  );
}

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
  const events = timeline.data?.events ?? [];

  const chartLabs = p.labs.filter(isChartLab);
  const reportedLabs = p.labs.filter((lab) => !isChartLab(lab));
  const latestBp = p.blood_pressure.at(-1) ?? null;

  // Derived from the timeline the page already loads — no extra round trip.
  const now = Date.now();
  const pastVisits = events.filter(
    (e: TimelineEvent) =>
      e.event_type === "encounter" && Date.parse(e.occurred_at) <= now,
  );
  const lastVisit = pastVisits[0] ?? null; // timeline is newest-first
  const nextAppointment = p.appointment_time ?? null;
  const appointmentOffset = nextAppointment ? formatOffset(nextAppointment) : "";
  const missedFollowUp =
    events.find((e: TimelineEvent) => e.event_type === "care_gap") ?? null;

  const a1c = chartLabs.find((l) => l.name.toLowerCase() === "a1c");
  const egfr = chartLabs.find((l) => l.name.toLowerCase() === "egfr");

  function labChip(lab: LabSummary | undefined) {
    if (!lab) return undefined;
    const ref = referenceFor(lab.name);
    const value = numericValue(lab.value);
    if (!ref || value === null) return undefined;
    if (value >= ref.low && value <= ref.high) return undefined;
    return { tone: "warn" as const, text: "Outside reference" };
  }

  return (
    <AppShell>
      {/* ------------------------- Chart header ------------------------- */}
      <Card>
        <div className="flex flex-wrap items-start justify-between gap-6">
          <div className="min-w-0">
            <EyebrowLabel>Patient chart</EyebrowLabel>
            <h1 className="mt-1.5 font-display text-4xl tracking-tight">
              {p.name}
            </h1>
            <p className="mt-1.5 text-sm text-ink-muted">
              {p.age} years old · {titleCase(p.sex ?? "unknown")}
              {p.birth_date ? ` · DOB ${formatDateOnly(p.birth_date)}` : ""}
              {p.mrn ? ` · MRN ${p.mrn}` : ""}
            </p>
            <div className="mt-3">
              <SyntheticBadge />
            </div>
          </div>
          <div className="shrink-0 text-right">
            <PillButton onClick={startVisit} disabled={starting}>
              {starting ? "Starting…" : "Start Visit"}
            </PillButton>
            {nextAppointment && (
              <p className="mt-2 font-mono text-[11px] uppercase tracking-wider text-ink-faint">
                {appointmentOffset === "Today" ? "Today" : appointmentOffset} ·{" "}
                {formatTime(nextAppointment)}
              </p>
            )}
            {startError && (
              <p className="mt-2 max-w-xs text-sm text-bad">{startError}</p>
            )}
          </div>
        </div>

        <Hairline className="my-5" />

        <dl className="grid grid-cols-2 gap-x-6 gap-y-4 md:grid-cols-4 lg:grid-cols-6">
          <Field label="MRN" value={p.mrn ?? "Not on file"} />
          <Field
            label="Date of birth"
            value={p.birth_date ? formatDateOnly(p.birth_date) : "Not on file"}
          />
          <Field label="Age" value={`${p.age}`} />
          <Field label="Sex" value={titleCase(p.sex ?? "unknown")} />
          <Field
            label="Last visit"
            value={
              lastVisit
                ? `${formatDate(lastVisit.occurred_at)} (${formatOffset(lastVisit.occurred_at).replace("today − ", "")})`
                : "None in window"
            }
          />
          {/* The overlay bundle pins this appointment; it only reads "Today"
              when it actually is today, never by assumption. */}
          <Field
            label="Appointment"
            value={
              nextAppointment
                ? `${appointmentOffset === "Today" ? "Today" : formatDate(nextAppointment)}, ${formatTime(nextAppointment)}`
                : "Unscheduled"
            }
          />
        </dl>

        <Hairline className="my-5" />

        {/* Latest values — stat tiles, not one-bar charts. */}
        <div className="grid grid-cols-2 gap-x-6 gap-y-5 md:grid-cols-3 lg:grid-cols-5">
          <StatTile
            label="Blood pressure"
            value={latestBp ? `${latestBp.systolic}/${latestBp.diastolic}` : "—"}
            // The most recent reading may be something the patient reported,
            // not a clinic measurement — the tile has to say which.
            caption={
              latestBp
                ? `${formatAge(latestBp.observed_at)} · ${
                    latestBp.source_class === "patient_report"
                      ? "patient-reported"
                      : "EHR"
                  }`
                : "none on file"
            }
            chip={
              latestBp && (latestBp.systolic >= 130 || latestBp.diastolic >= 80)
                ? { tone: "bad", text: "Above goal" }
                : undefined
            }
          />
          <StatTile
            label="A1c"
            value={a1c?.value ?? "—"}
            caption={a1c ? formatAge(a1c.observed_at) : "none on file"}
            chip={labChip(a1c)}
          />
          <StatTile
            label="eGFR"
            value={egfr ? `${numericValue(egfr.value) ?? egfr.value}` : "—"}
            caption={egfr ? formatAge(egfr.observed_at) : "none on file"}
            chip={labChip(egfr)}
          />
          <StatTile
            label="Active medications"
            value={`${p.medications.length}`}
            caption={p.medications.map((m) => m.name).join(", ") || "none"}
          />
          <StatTile
            label="Open care gaps"
            value={`${p.care_gaps.length}`}
            caption={
              missedFollowUp
                ? `follow-up missed ${daysAgo(missedFollowUp.occurred_at)}d ago`
                : undefined
            }
            chip={
              p.care_gaps.length > 0
                ? { tone: "warn", text: "Needs attention" }
                : undefined
            }
          />
        </div>
      </Card>

      {/* --------------------------- The hero --------------------------- */}
      <div className="mt-10">
        <EyebrowLabel>Pre-visit intelligence</EyebrowLabel>
        <h2 className="mt-2 font-display text-5xl tracking-tight">
          What matters today?
        </h2>
      </div>

      <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
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

        {/* Care gaps carry the same detail text as the priorities above, so
            this surface shows labels only — the paragraph lives once, in the
            priorities list, and this stays readable as the gap ledger. */}
        <Card>
          <EyebrowLabel>Open care gaps</EyebrowLabel>
          <ul className="mt-3 space-y-2">
            {p.care_gaps.map((gap) => (
              <li key={gap.id} className="flex items-start gap-2.5 text-sm">
                <span
                  aria-hidden
                  className="mt-1.5 size-1.5 shrink-0 rounded-full bg-warn"
                />
                <span className="text-ink">{gap.label}</span>
              </li>
            ))}
            {p.care_gaps.length === 0 && (
              <li className="text-sm text-ink-muted">
                No open care gaps returned.
              </li>
            )}
          </ul>
          <Hairline className="my-4" />
          <p className="text-xs text-ink-faint">
            The canonical gap list the §22.1 completeness evaluator scores
            against — the same four items the pipeline is graded on finding.
          </p>
        </Card>
      </div>

      {/* ------------------------- Chart review -------------------------- */}
      <div className="mt-10 flex items-center gap-4">
        <EyebrowLabel>Chart review</EyebrowLabel>
        <Hairline className="flex-1" />
      </div>

      {/* items-start: cards size to their own content instead of stretching to
          the tallest in the row. */}
      <div className="mt-5 grid grid-cols-1 items-start gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <div className="flex flex-wrap items-baseline justify-between gap-3">
            <EyebrowLabel>Blood pressure</EyebrowLabel>
            <span className="text-xs text-ink-faint">
              Every recorded reading · goal band is a guideline reference
            </span>
          </div>
          <div className="mt-4">
            <BloodPressurePlot readings={p.blood_pressure} />
          </div>
        </Card>

        <Card>
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <EyebrowLabel>Labs vs reference</EyebrowLabel>
            <StatusChip tone="neutral">All EHR</StatusChip>
          </div>
          <ul className="mt-2 divide-y divide-hairline">
            {chartLabs.map((lab) => (
              <ReferenceMeter key={`${lab.name}-${lab.observed_at}`} lab={lab} />
            ))}
            {chartLabs.length === 0 && (
              <li className="py-3 text-sm text-ink-muted">
                No chart labs on record.
              </li>
            )}
          </ul>
          <p className="mt-3 text-xs text-ink-faint">
            Reference intervals are assay normals, not individualized targets.
          </p>
        </Card>

        <Card>
          <EyebrowLabel>Problem list</EyebrowLabel>
          <ul className="mt-3 space-y-2">
            {p.conditions.map((c) => (
              <li key={c} className="text-sm text-ink">
                {c}
              </li>
            ))}
            {p.conditions.length === 0 && (
              <li className="text-sm text-ink-muted">No conditions on record.</li>
            )}
          </ul>
        </Card>

        <Card>
          <EyebrowLabel>Medications</EyebrowLabel>
          <ul className="mt-3 space-y-2.5">
            {p.medications.map((m) => (
              <li
                key={m.name}
                className="flex items-center justify-between gap-2 text-sm"
              >
                <span className="text-ink">
                  {titleCase(m.name)}
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
          <p className="mt-4 text-xs text-ink-faint">
            EHR medication list — status is what the chart claims, not
            necessarily what the patient is taking.
          </p>
        </Card>

        {/* Only rendered when an encounter actually produced lab facts —
            otherwise it would just restate the meters card above. */}
        {reportedLabs.length > 0 && (
          <Card>
            <EyebrowLabel>Reported since last visit</EyebrowLabel>
            <ul className="mt-3 space-y-2.5">
              {reportedLabs.map((lab) => (
                <li key={`${lab.name}-${lab.observed_at}-row`} className="text-sm">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="text-ink">{lab.name}</span>
                    <span className="text-ink-muted">{lab.value}</span>
                  </div>
                  <div className="mt-1 flex items-center gap-2">
                    <ProvenanceChip lab={lab} />
                    <span className="font-mono text-[11px] text-ink-faint">
                      {formatAge(lab.observed_at)}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
            <p className="mt-4 text-xs text-ink-faint">
              Captured during an encounter — not an EHR result.
            </p>
          </Card>
        )}
      </div>

      {/* ------------------------- Care journey -------------------------- */}
      <div className="mt-10 flex items-center gap-4">
        <EyebrowLabel>Care journey</EyebrowLabel>
        <Hairline className="flex-1" />
      </div>

      <Card className="mt-5">
        {timeline.loading ? (
          <p className="text-sm text-ink-muted">Loading timeline…</p>
        ) : timeline.error ? (
          <ErrorState
            error={timeline.error}
            onRetry={timeline.reload}
            title="Timeline could not be loaded."
          />
        ) : events.length === 0 ? (
          <p className="text-sm text-ink-muted">No timeline events.</p>
        ) : (
          <ol className="relative">
            {events.map((event: TimelineEvent, i: number) => {
              const isToday = formatOffset(event.occurred_at) === "Today";
              const future = Date.parse(event.occurred_at) > now;
              const isGap = event.event_type === "care_gap";
              // Days of silence between this event and the one before it
              // (list is newest-first, so "before" is the next index).
              const prev = events[i + 1];
              const gapDays = prev
                ? Math.round(
                    (Date.parse(event.occurred_at) -
                      Date.parse(prev.occurred_at)) /
                      86_400_000,
                  )
                : 0;
              return (
                <li key={event.id}>
                  <div className="relative flex gap-4 pb-1">
                    {i < events.length - 1 && (
                      <span
                        aria-hidden
                        className="absolute left-[5px] top-4 h-full w-px bg-hairline-strong"
                      />
                    )}
                    <span
                      aria-hidden
                      className={`relative mt-1.5 size-[11px] shrink-0 rounded-full border-2 ${
                        isToday || future
                          ? "border-cta bg-cta"
                          : isGap
                            ? "border-bad bg-card"
                            : "border-hairline-strong bg-card"
                      }`}
                    />
                    <div className="min-w-0 pb-4">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-[11px] uppercase tracking-wider text-ink-faint">
                          {future ? "Today" : formatOffset(event.occurred_at)}
                        </span>
                        <span className="font-mono text-[11px] text-ink-faint">
                          {formatDate(event.occurred_at)}
                        </span>
                        <StatusChip tone={isGap ? "bad" : "neutral"}>
                          {EVENT_LABELS[event.event_type] ?? event.event_type}
                        </StatusChip>
                      </div>
                      <p className="mt-1 text-sm font-medium text-ink">
                        {event.label}
                      </p>
                      {event.detail && (
                        <p className="text-sm text-ink-muted">{event.detail}</p>
                      )}
                    </div>
                  </div>
                  {/* Silences longer than a quarter are themselves a finding. */}
                  {gapDays > 90 && (
                    <div className="relative flex gap-4 pb-4">
                      <span
                        aria-hidden
                        className="absolute left-[5px] top-0 h-full w-px bg-hairline-strong"
                      />
                      <span className="ml-[26px] font-mono text-[11px] uppercase tracking-wider text-ink-faint">
                        ····· {gapDays}d with no recorded contact
                      </span>
                    </div>
                  )}
                </li>
              );
            })}
          </ol>
        )}
      </Card>

      <QADrawer />
    </AppShell>
  );
}
