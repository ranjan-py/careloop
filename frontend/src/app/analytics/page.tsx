"use client";

import { useState } from "react";
import { AppShell } from "@/components/AppShell";
import { ErrorState, LoadingState } from "@/components/States";
import { Card, EyebrowLabel, GhostButton, StatusChip } from "@/components/ui";
import { api } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { REJECTION_CATEGORY_LABELS } from "@/lib/types";
import type { HighFrictionItem } from "@/lib/types";

/* Decision-mix palette — validated trio (green/blue/red on white surface). */
const MIX = [
  { key: "accepted" as const, label: "Accepted", color: "#1F7A4D" },
  { key: "modified" as const, label: "Modified", color: "#2557D6" },
  { key: "rejected" as const, label: "Rejected", color: "#BE3D3D" },
];

function HighFrictionRow({
  item,
  maxPct,
}: {
  item: HighFrictionItem;
  maxPct: number;
}) {
  const [open, setOpen] = useState(false);
  const width = maxPct > 0 ? (item.modify_reject_pct / maxPct) * 100 : 0;
  return (
    <li className="py-3">
      <div className="flex items-center gap-4">
        <span className="w-56 shrink-0 truncate text-sm text-ink">
          {item.title}
        </span>
        <span
          className="h-3 rounded-[4px] bg-cta"
          style={{ width: `${Math.max(width, 2)}%` }}
          aria-hidden
        />
        <span className="font-mono text-sm tabular-nums text-ink">
          {item.modify_reject_pct}%
        </span>
        {item.trend && (
          <span className="font-mono text-xs text-ink-faint">
            {item.trend === "increasing" ? "↑ increasing" : item.trend}
          </span>
        )}
        <GhostButton onClick={() => setOpen((v) => !v)} className="ml-auto">
          Review pattern
        </GhostButton>
      </div>
      {open && (
        <div className="ml-1 mt-3 rounded-xl bg-well p-4 text-sm text-ink-muted">
          <p className="font-medium text-ink">{item.title}</p>
          {item.top_reason && <p className="mt-1">Top reason: {item.top_reason}</p>}
          {typeof item.organizations === "number" && (
            <p className="mt-0.5">
              Observed across: {item.organizations} synthetic organizations
            </p>
          )}
          {item.trend && <p className="mt-0.5">Trend: {item.trend}</p>}
        </div>
      )}
    </li>
  );
}

export default function AnalyticsPage() {
  const feedback = useApi(() => api.getAnalyticsFeedback(), []);

  return (
    <AppShell>
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <EyebrowLabel>Feedback analytics</EyebrowLabel>
          <h1 className="mt-1 font-display text-4xl tracking-tight">
            Clinician decisions
          </h1>
        </div>
        <StatusChip tone="warn">
          {feedback.data?.cohort_label ?? "Synthetic demo cohort"}
        </StatusChip>
      </div>

      <div className="mt-8">
        {feedback.loading ? (
          <LoadingState label="Loading synthetic feedback analytics…" />
        ) : feedback.error ? (
          <ErrorState error={feedback.error} onRetry={feedback.reload} />
        ) : feedback.data ? (
          <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
            {/* Decision mix */}
            <Card>
              <EyebrowLabel>Decision mix</EyebrowLabel>
              <div className="mt-5 grid grid-cols-3 gap-4">
                {MIX.map(({ key, label, color }) => (
                  <div key={key}>
                    <div className="flex items-center gap-2">
                      <span
                        aria-hidden
                        className="size-2.5 rounded-full"
                        style={{ backgroundColor: color }}
                      />
                      <span className="text-sm text-ink-muted">{label}</span>
                    </div>
                    <p className="mt-1 font-display text-4xl tabular-nums text-ink">
                      {feedback.data!.decision_mix[key]}%
                    </p>
                  </div>
                ))}
              </div>
              <div
                className="mt-5 flex h-4 w-full gap-[2px] overflow-hidden rounded-[4px]"
                role="img"
                aria-label={MIX.map(
                  (m) => `${m.label} ${feedback.data!.decision_mix[m.key]}%`,
                ).join(", ")}
              >
                {MIX.map(({ key, color }) => (
                  <span
                    key={key}
                    style={{
                      backgroundColor: color,
                      width: `${feedback.data!.decision_mix[key]}%`,
                    }}
                  />
                ))}
              </div>
            </Card>

            {/* Reasons */}
            <Card>
              <EyebrowLabel>Rejection reasons</EyebrowLabel>
              {feedback.data.reasons.length === 0 ? (
                <p className="mt-3 text-sm text-ink-muted">
                  No rejection events in the cohort.
                </p>
              ) : (
                <ul className="mt-4 space-y-2.5">
                  {(() => {
                    const max = Math.max(
                      ...feedback.data.reasons.map((r) => r.count),
                      1,
                    );
                    return feedback.data.reasons.map((reason) => (
                      <li
                        key={reason.category}
                        className="flex items-center gap-3"
                      >
                        <span className="w-64 shrink-0 truncate text-sm text-ink">
                          {reason.label ||
                            REJECTION_CATEGORY_LABELS[reason.category]}
                        </span>
                        <span
                          aria-hidden
                          className="h-3 rounded-[4px] bg-cta"
                          style={{
                            width: `${Math.max((reason.count / max) * 100, 2)}%`,
                          }}
                        />
                        <span className="font-mono text-sm tabular-nums text-ink">
                          {reason.count}
                        </span>
                      </li>
                    ));
                  })()}
                </ul>
              )}
            </Card>

            {/* High friction */}
            <Card className="xl:col-span-2">
              <EyebrowLabel>High-friction recommendations</EyebrowLabel>
              {feedback.data.high_friction.length === 0 ? (
                <p className="mt-3 text-sm text-ink-muted">
                  No high-friction recommendations in the cohort.
                </p>
              ) : (
                <ul className="mt-2 divide-y divide-hairline">
                  {feedback.data.high_friction.map((item) => (
                    <HighFrictionRow
                      key={item.title}
                      item={item}
                      maxPct={Math.max(
                        ...feedback.data!.high_friction.map(
                          (i) => i.modify_reject_pct,
                        ),
                        1,
                      )}
                    />
                  ))}
                </ul>
              )}
              <p className="mt-4 text-xs text-ink-faint">
                Feedback is captured for review — no automatic model change is
                made from these patterns.
              </p>
            </Card>
          </div>
        ) : null}
      </div>
    </AppShell>
  );
}
