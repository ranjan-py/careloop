"use client";

/**
 * Chart primitives for the pre-visit chart review (spec §7).
 *
 * Design rules applied here, in order: form first, color last.
 * - Blood pressure is a RANGE per reading (diastolic→systolic), plotted on a
 *   real time axis. No connecting line: the readings are weeks apart and
 *   mixed-provenance, so a line would draw values that were never measured.
 *   The empty stretch between the last reading and today is the finding, so
 *   the axis runs to today and the gap is annotated.
 * - Single-value labs get a reference-range meter (position against a limit),
 *   not a one-point trend.
 * - One mark hue (--color-cta #2557D6, 6.15:1 on white). Provenance rides a
 *   second, non-color channel — solid fill = EHR chart reading, outlined =
 *   patient-reported — so it survives any color-vision deficiency. Status
 *   colors stay reserved for state and always ship with a text label.
 * - Palette validated: lightness band, chroma floor, CVD separation
 *   (worst adjacent ΔE 23.8 deutan), normal-vision floor, contrast — all PASS.
 */

import { useId, useState } from "react";
import type { BloodPressureReading, LabSummary } from "@/lib/types";
import { daysAgo, formatDate } from "@/lib/format";
import { EyebrowLabel, StatusChip } from "./ui";

const MARK = "#2557D6"; // --color-cta
const GRID = "#E7E0D2"; // --color-hairline
const AXIS_TEXT = "#8A93A1"; // --color-ink-faint
const LABEL_TEXT = "#1B2430"; // --color-ink
const MUTED_TEXT = "#5B6472"; // --color-ink-muted
const GOAL_HUE = "#1F7A4D"; // --color-good

function isReported(source_class: string): boolean {
  return source_class === "patient_report";
}

/* ------------------------- Blood pressure plot -------------------------- */

/** Guideline BP goal for hypertension with diabetes/CKD (ACC/AHA). Shown as a
 * labelled reference band — it is not a per-patient target set by anyone. */
const GOAL = { systolic: 130, diastolic: 80 };

const PLOT = { w: 700, h: 252, padL: 40, padR: 18, padT: 30, padB: 46 };
const Y_TICKS = [60, 80, 100, 120, 140, 160, 180];
const BAR_W = 22; // ≤ 24px mark cap
const DAY_MS = 24 * 60 * 60 * 1000;

/** Index of the hovered/focused reading. */
type Hover = number | null;

export function BloodPressurePlot({
  readings,
}: {
  readings: BloodPressureReading[];
}) {
  const [hover, setHover] = useState<Hover>(null);
  const [showTable, setShowTable] = useState(false);
  const clipId = useId();

  if (readings.length === 0) {
    return (
      <p className="mt-3 text-sm text-ink-muted">
        No blood-pressure readings with a recorded value are on file.
      </p>
    );
  }

  const now = Date.now();
  const first = Date.parse(readings[0].observed_at);
  // Domain runs to today so the silence since the last reading is literal.
  const from = first - 12 * DAY_MS;
  const span = Math.max(now - from, DAY_MS);

  const yMin = Math.min(60, ...readings.map((r) => r.diastolic - 10));
  const yMax = Math.max(180, ...readings.map((r) => r.systolic + 10));
  const plotH = PLOT.h - PLOT.padT - PLOT.padB;
  const plotW = PLOT.w - PLOT.padL - PLOT.padR;

  const y = (v: number) => PLOT.padT + ((yMax - v) / (yMax - yMin)) * plotH;
  const x = (iso: string) =>
    PLOT.padL + ((Date.parse(iso) - from) / span) * plotW;

  const last = readings[readings.length - 1];
  const sinceLast = daysAgo(last.observed_at) ?? 0;
  const ticks = Y_TICKS.filter((t) => t >= yMin && t <= yMax);
  const hovered = hover === null ? null : readings[hover];

  // Readings taken hours apart land on the same pixel. Rather than stack or
  // nudge labels (which detaches them from their mark), drop the colliding
  // one — the tooltip and the table view still carry every value. The right
  // edge is reserved for the "Today" rule.
  const MIN_LABEL_GAP = 52;
  const RIGHT_EDGE_RESERVE = 34;
  const labelled = new Set<number>();
  let lastLabelX = -Infinity;
  readings.forEach((r, i) => {
    const cx = x(r.observed_at);
    if (cx - lastLabelX < MIN_LABEL_GAP) return;
    if (PLOT.padL + plotW - cx < RIGHT_EDGE_RESERVE) return;
    labelled.add(i);
    lastLabelX = cx;
  });

  return (
    <div>
      <div className="relative">
        <svg
          viewBox={`0 0 ${PLOT.w} ${PLOT.h}`}
          className="h-auto w-full"
          role="img"
          aria-label={`Blood pressure: ${readings
            .map((r) => `${r.systolic} over ${r.diastolic} ${formatDate(r.observed_at)}`)
            .join("; ")}. Goal below ${GOAL.systolic} over ${GOAL.diastolic}.`}
        >
          <defs>
            <clipPath id={clipId}>
              <rect x={PLOT.padL} y={PLOT.padT} width={plotW} height={plotH} />
            </clipPath>
          </defs>

          {/* Goal band — a labelled reference region, not a series. */}
          <rect
            x={PLOT.padL}
            y={y(GOAL.systolic)}
            width={plotW}
            height={y(GOAL.diastolic) - y(GOAL.systolic)}
            fill={GOAL_HUE}
            fillOpacity={0.08}
          />
          <line
            x1={PLOT.padL}
            x2={PLOT.padL + plotW}
            y1={y(GOAL.systolic)}
            y2={y(GOAL.systolic)}
            stroke={GOAL_HUE}
            strokeOpacity={0.35}
          />

          {/* Gridlines + y ticks — hairline, solid, recessive. */}
          {ticks.map((t) => (
            <g key={t}>
              <line
                x1={PLOT.padL}
                x2={PLOT.padL + plotW}
                y1={y(t)}
                y2={y(t)}
                stroke={GRID}
              />
              <text
                x={PLOT.padL - 8}
                y={y(t) + 3.5}
                textAnchor="end"
                fontSize={10}
                fill={AXIS_TEXT}
                style={{ fontVariantNumeric: "tabular-nums" }}
              >
                {t}
              </text>
            </g>
          ))}
          <text
            x={PLOT.padL - 8}
            y={PLOT.padT - 12}
            textAnchor="end"
            fontSize={9}
            fill={AXIS_TEXT}
          >
            mmHg
          </text>

          {/* Today rule — the right edge of the domain. */}
          <line
            x1={PLOT.padL + plotW}
            x2={PLOT.padL + plotW}
            y1={PLOT.padT}
            y2={PLOT.padT + plotH}
            stroke="#D8CFBC"
          />
          <text
            x={PLOT.padL + plotW}
            y={PLOT.padT + plotH + 15}
            textAnchor="end"
            fontSize={10}
            fill={MUTED_TEXT}
          >
            Today
          </text>

          {/* Gap annotation — the interval with no reading in it. */}
          {sinceLast > 14 && (
            <g>
              <line
                x1={x(last.observed_at)}
                x2={PLOT.padL + plotW}
                y1={PLOT.padT + plotH + 30}
                y2={PLOT.padT + plotH + 30}
                stroke={AXIS_TEXT}
              />
              <text
                x={(x(last.observed_at) + PLOT.padL + plotW) / 2}
                y={PLOT.padT + plotH + 26}
                textAnchor="middle"
                fontSize={10}
                fill={MUTED_TEXT}
              >
                {sinceLast}d with no reading
              </text>
            </g>
          )}

          {/* Range marks — one per reading, diastolic → systolic. */}
          <g clipPath={`url(#${clipId})`}>
            {readings.map((r, i) => {
              const cx = x(r.observed_at);
              const top = y(r.systolic);
              const height = Math.max(y(r.diastolic) - top, 4);
              const reported = isReported(r.source_class);
              return (
                <rect
                  key={r.id}
                  x={cx - BAR_W / 2}
                  y={top}
                  width={BAR_W}
                  height={height}
                  rx={4}
                  fill={reported ? "#FFFFFF" : MARK}
                  stroke={reported ? MARK : "none"}
                  strokeWidth={reported ? 2 : 0}
                  opacity={hover !== null && hover !== i ? 0.55 : 1}
                />
              );
            })}
          </g>

          {/* Direct labels — a handful of readings, so each surviving mark is
              labelled without becoming the "a number on every point" mess. */}
          {readings.map((r, i) => {
            if (!labelled.has(i)) return null;
            const cx = x(r.observed_at);
            const age = daysAgo(r.observed_at) ?? 0;
            return (
              <g key={`${r.id}-label`}>
                <text
                  x={cx}
                  y={y(r.systolic) - 9}
                  textAnchor="middle"
                  fontSize={12}
                  fontWeight={600}
                  fill={LABEL_TEXT}
                >
                  {r.systolic}/{r.diastolic}
                </text>
                <text
                  x={cx}
                  y={PLOT.padT + plotH + 15}
                  textAnchor="middle"
                  fontSize={10}
                  fill={AXIS_TEXT}
                >
                  {age <= 0 ? "today" : `${age}d ago`}
                </text>
              </g>
            );
          })}

          {/* Hit targets — wider than the mark, keyboard reachable. */}
          {readings.map((r, i) => {
            const cx = x(r.observed_at);
            return (
              <rect
                key={`${r.id}-hit`}
                x={cx - 16}
                y={PLOT.padT}
                width={32}
                height={plotH}
                fill="transparent"
                tabIndex={0}
                role="button"
                aria-label={`${r.systolic} over ${r.diastolic}, ${formatDate(r.observed_at)}`}
                onMouseEnter={() => setHover(i)}
                onMouseLeave={() => setHover(null)}
                onFocus={() => setHover(i)}
                onBlur={() => setHover(null)}
                className="cursor-pointer outline-none"
              />
            );
          })}
        </svg>

      </div>

      {/* Readout, not a floating tooltip: the plot is short enough that any
          overlay would cover the mark it describes. */}
      <div
        aria-live="polite"
        className="mt-2 flex min-h-[34px] items-center gap-3 rounded-xl border border-hairline bg-well px-3 py-2"
      >
        {hovered ? (
          <>
            <span className="font-mono text-[11px] uppercase tracking-wider text-ink-faint">
              {formatDate(hovered.observed_at)}
            </span>
            <span className="text-sm font-semibold text-ink">{hovered.value}</span>
            <span className="text-xs text-ink-muted">
              {isReported(hovered.source_class)
                ? "Patient-reported"
                : "EHR chart reading"}
              {hovered.method ? ` · ${hovered.method.replace(/_/g, " ")}` : ""}
            </span>
          </>
        ) : (
          <span className="text-xs text-ink-faint">
            Hover or tab a reading for its date and source.
          </span>
        )}
      </div>

      {/* Legend — provenance is the only distinction, and it is not color. */}
      <div className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-2">
        <span className="flex items-center gap-2 text-xs text-ink-muted">
          <span
            aria-hidden
            className="h-3.5 w-2.5 rounded-[3px]"
            style={{ backgroundColor: MARK }}
          />
          EHR chart reading
        </span>
        {readings.some((r) => isReported(r.source_class)) && (
          <span className="flex items-center gap-2 text-xs text-ink-muted">
            <span
              aria-hidden
              className="h-3.5 w-2.5 rounded-[3px] border-2 bg-card"
              style={{ borderColor: MARK }}
            />
            Patient-reported
          </span>
        )}
        <span className="flex items-center gap-2 text-xs text-ink-muted">
          <span
            aria-hidden
            className="h-3.5 w-2.5 rounded-[3px]"
            style={{ backgroundColor: GOAL_HUE, opacity: 0.18 }}
          />
          Goal band &lt; {GOAL.systolic}/{GOAL.diastolic}
        </span>
        <button
          onClick={() => setShowTable((v) => !v)}
          className="ml-auto text-xs text-ink-muted underline-offset-2 hover:text-ink hover:underline"
        >
          {showTable
            ? "Hide table"
            : labelled.size < readings.length
              ? `Table view — ${readings.length - labelled.size} unlabelled`
              : "Table view"}
        </button>
      </div>

      {showTable && (
        <table className="mt-3 w-full text-left text-sm">
          <thead>
            <tr className="border-b border-hairline text-xs uppercase tracking-wider text-ink-faint">
              <th className="py-1.5 font-medium">Reading</th>
              <th className="py-1.5 font-medium">Observed</th>
              <th className="py-1.5 font-medium">Source</th>
            </tr>
          </thead>
          <tbody>
            {[...readings].reverse().map((r) => (
              <tr key={`${r.id}-row`} className="border-b border-hairline last:border-0">
                <td className="py-1.5 tabular-nums text-ink">{r.value}</td>
                <td className="py-1.5 text-ink-muted">
                  {formatDate(r.observed_at)}
                </td>
                <td className="py-1.5 text-ink-muted">
                  {isReported(r.source_class) ? "Patient-reported" : "EHR"}
                  {r.method ? ` · ${r.method.replace(/_/g, " ")}` : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/* -------------------------- Reference meters ---------------------------- */

/**
 * Adult lab reference intervals — assay normals, NOT individualized treatment
 * targets, and labelled as such wherever they render. Only labs listed here
 * get a meter; anything else falls back to a plain value row rather than
 * having a range invented for it.
 */
const REFERENCE: Record<
  string,
  { low: number; high: number; min: number; max: number; unit: string; note?: string }
> = {
  a1c: { low: 4.0, high: 5.6, min: 4, max: 12, unit: "%", note: "non-diabetic reference" },
  egfr: { low: 60, high: 120, min: 0, max: 120, unit: "mL/min/1.73m²" },
  creatinine: { low: 0.74, high: 1.35, min: 0.4, max: 2.0, unit: "mg/dL", note: "adult male" },
  potassium: { low: 3.5, high: 5.1, min: 2.5, max: 6.5, unit: "mmol/L" },
};

const LEADING_NUMBER = /-?\d+(\.\d+)?/;

export function referenceFor(name: string) {
  return REFERENCE[name.trim().toLowerCase()] ?? null;
}

export function numericValue(raw: string): number | null {
  const match = LEADING_NUMBER.exec(raw);
  if (!match) return null;
  const n = Number(match[0]);
  return Number.isFinite(n) ? n : null;
}

export function ReferenceMeter({ lab }: { lab: LabSummary }) {
  const ref = referenceFor(lab.name);
  const value = numericValue(lab.value);
  const age = daysAgo(lab.observed_at);

  if (!ref || value === null) {
    // No published range on file for this name — show the value plainly
    // rather than measure it against a range that doesn't exist.
    return (
      <li className="py-3">
        <div className="flex items-baseline justify-between gap-3">
          <span className="text-sm text-ink">{lab.name}</span>
          <span className="text-sm text-ink-muted">{lab.value}</span>
        </div>
        <p className="mt-0.5 font-mono text-[11px] text-ink-faint">
          {age !== null && age <= 0 ? "today" : `${age}d ago`} · no reference range on file
        </p>
      </li>
    );
  }

  const clamp = (v: number) => Math.min(Math.max(v, ref.min), ref.max);
  const pct = (v: number) => ((clamp(v) - ref.min) / (ref.max - ref.min)) * 100;
  const inRange = value >= ref.low && value <= ref.high;
  const stale = age !== null && age > 90;

  return (
    <li className="py-3.5">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-sm font-medium text-ink">{lab.name}</span>
        <span className="text-sm text-ink">
          <span className="font-semibold">{value}</span>{" "}
          <span className="text-ink-faint">{ref.unit}</span>
        </span>
      </div>

      <div className="relative mt-2 h-2 w-full rounded-full bg-well">
        {/* Reference band */}
        <span
          aria-hidden
          className="absolute inset-y-0 rounded-full"
          style={{
            left: `${pct(ref.low)}%`,
            width: `${pct(ref.high) - pct(ref.low)}%`,
            backgroundColor: GOAL_HUE,
            opacity: 0.22,
          }}
        />
        {/* Measured value — 10px dot with a 2px surface ring */}
        <span
          aria-hidden
          className="absolute top-1/2 size-[10px] -translate-x-1/2 -translate-y-1/2 rounded-full ring-2 ring-card"
          style={{ left: `${pct(value)}%`, backgroundColor: MARK }}
        />
      </div>

      <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-mono text-[11px] text-ink-faint">
          ref {ref.low}–{ref.high}
          {ref.note ? ` (${ref.note})` : ""}
        </span>
        <span className="font-mono text-[11px] text-ink-faint">·</span>
        <span className="font-mono text-[11px] text-ink-faint">
          {age !== null && age <= 0 ? "today" : `${age}d old`}
        </span>
        {!inRange && (
          <StatusChip tone="warn">
            <span aria-hidden>▲</span> Outside reference
          </StatusChip>
        )}
        {stale && <StatusChip tone="neutral">Stale</StatusChip>}
      </div>
    </li>
  );
}

/* ------------------------------ Stat tile ------------------------------- */

export function StatTile({
  label,
  value,
  caption,
  chip,
}: {
  label: string;
  value: string;
  caption?: string;
  chip?: { tone: "good" | "bad" | "warn" | "neutral" | "info"; text: string };
}) {
  return (
    <div className="min-w-0">
      <EyebrowLabel>{label}</EyebrowLabel>
      {/* Proportional figures: tabular-nums makes display sizes look loose. */}
      <p className="mt-1.5 text-2xl font-semibold leading-none text-ink">{value}</p>
      <div className="mt-1.5 flex flex-wrap items-center gap-2">
        {caption && (
          <span className="font-mono text-[11px] text-ink-faint">{caption}</span>
        )}
        {chip && <StatusChip tone={chip.tone}>{chip.text}</StatusChip>}
      </div>
    </div>
  );
}
