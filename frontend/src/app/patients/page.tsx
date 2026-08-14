"use client";

import Link from "next/link";
import { AppShell } from "@/components/AppShell";
import { ErrorState, LoadingState } from "@/components/States";
import { EyebrowLabel, StatusChip } from "@/components/ui";
import { api } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { formatTime } from "@/lib/format";
import type { PatientListItem } from "@/lib/types";

function PatientRow({ patient }: { patient: PatientListItem }) {
  const inner = (
    <div className="flex items-center justify-between gap-6 px-6 py-5">
      <div className="min-w-0">
        <div className="flex items-baseline gap-3">
          <span className="font-display text-xl text-ink">{patient.name}</span>
          <span className="text-sm text-ink-muted">
            {patient.age}
            {patient.sex ? ` · ${patient.sex}` : ""}
          </span>
        </div>
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {patient.conditions.map((c) => (
            <StatusChip key={c} tone="neutral">
              {c}
            </StatusChip>
          ))}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-4">
        {patient.appointment_time && (
          <span className="font-mono text-sm text-ink-muted">
            {formatTime(patient.appointment_time)}
          </span>
        )}
        {typeof patient.attention_count === "number" &&
          patient.attention_count > 0 && (
            <StatusChip tone="warn">
              {patient.attention_count} items need attention
            </StatusChip>
          )}
        {patient.display_only ? (
          <span className="font-mono text-[10px] uppercase tracking-wider text-ink-faint">
            display only
          </span>
        ) : (
          <span aria-hidden className="text-ink-faint">
            →
          </span>
        )}
      </div>
    </div>
  );

  if (patient.display_only) {
    return (
      <li className="border-b border-hairline bg-card opacity-70 last:border-b-0">
        {inner}
      </li>
    );
  }
  return (
    <li className="border-b border-hairline bg-card transition-colors last:border-b-0 hover:bg-well">
      <Link href={`/patients/${patient.id}`}>{inner}</Link>
    </li>
  );
}

export default function PatientsPage() {
  const patients = useApi(() => api.listPatients(), []);

  return (
    <AppShell>
      <EyebrowLabel>Today</EyebrowLabel>
      <h1 className="mt-1 font-display text-4xl tracking-tight">
        Today&rsquo;s patients
      </h1>
      <p className="mt-2 text-sm text-ink-muted">
        Synthetic cohort — every chart here is Synthea-generated.
      </p>

      <div className="mt-8">
        {patients.loading ? (
          <LoadingState label="Loading today's schedule…" />
        ) : patients.error ? (
          <ErrorState error={patients.error} onRetry={patients.reload} />
        ) : (
          <ul className="overflow-hidden rounded-2xl border border-hairline">
            {(patients.data?.patients ?? []).map((p) => (
              <PatientRow key={p.id} patient={p} />
            ))}
            {(patients.data?.patients ?? []).length === 0 && (
              <li className="bg-card px-6 py-8 text-sm text-ink-muted">
                No patients seeded yet. Run <code className="font-mono">make seed</code>.
              </li>
            )}
          </ul>
        )}
      </div>
    </AppShell>
  );
}
