"use client";

import { useEffect, useMemo, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { ErrorState, LoadingState } from "@/components/States";
import { Card, EyebrowLabel, GhostButton, StatusChip } from "@/components/ui";
import type { ChipTone } from "@/components/ui";
import { api, describeError } from "@/lib/api";
import { STORAGE_KEYS, useApi, useStoredId } from "@/lib/hooks";
import type {
  GraphHop,
  GraphNode,
  GraphRelationship,
  TraceStepStatus,
} from "@/lib/types";

const TABS = ["Trace Summary", "Context Graph", "Evals"] as const;
type Tab = (typeof TABS)[number];

const TRACE_TONES: Record<TraceStepStatus, ChipTone> = {
  PASS: "good",
  FAIL: "bad",
  DEGRADED: "warn",
  BLOCKED: "warn",
  DENIED: "bad",
};

/* ------------------------------ Trace tab ------------------------------- */

function TraceSummaryTab({ encounterId }: { encounterId: string }) {
  const trace = useApi(() => api.getTraceSummary(encounterId), [encounterId]);

  if (!encounterId)
    return (
      <p className="text-sm text-ink-muted">
        Enter an encounter id above — run an encounter first and it is
        remembered automatically.
      </p>
    );
  if (trace.loading) return <LoadingState label="Loading trace summary…" />;
  if (trace.error)
    return <ErrorState error={trace.error} onRetry={trace.reload} />;

  return (
    <div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-hairline text-left font-mono text-[11px] uppercase tracking-wider text-ink-faint">
              <th className="py-2 pr-4 font-medium">Step</th>
              <th className="py-2 pr-4 font-medium">Status</th>
              <th className="py-2 pr-4 font-medium">Detail</th>
              <th className="py-2 text-right font-medium">Latency</th>
            </tr>
          </thead>
          <tbody>
            {(trace.data?.rows ?? []).map((row, i) => (
              <tr key={i} className="border-b border-hairline last:border-b-0">
                <td className="py-2.5 pr-4 text-ink">{row.step}</td>
                <td className="py-2.5 pr-4">
                  <StatusChip tone={TRACE_TONES[row.status]}>
                    {row.status}
                  </StatusChip>
                </td>
                <td className="py-2.5 pr-4 text-ink-muted">{row.detail}</td>
                <td className="py-2.5 text-right font-mono text-xs tabular-nums text-ink-muted">
                  {typeof row.latency_ms === "number"
                    ? `${row.latency_ms} ms`
                    : "—"}
                </td>
              </tr>
            ))}
            {(trace.data?.rows ?? []).length === 0 && (
              <tr>
                <td colSpan={4} className="py-4 text-ink-muted">
                  No trace rows recorded for this encounter yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="mt-4 flex items-center justify-between gap-4">
        <p className="text-xs text-ink-faint">
          Read from the app&rsquo;s own Postgres records — Langfuse ingestion is
          asynchronous, so this table is the stage-reliable view. Permission
          denials (BLOCKED/DENIED) appear here.
        </p>
        <a
          href="http://localhost:3101"
          target="_blank"
          rel="noreferrer"
          className="shrink-0 text-sm text-cta underline-offset-2 hover:underline"
        >
          Open in local Langfuse ↗
        </a>
      </div>
    </div>
  );
}

/* ------------------------------ Graph tab ------------------------------- */

function nodeDisplayName(node: GraphNode): string {
  const p = node.properties ?? {};
  for (const key of ["name", "title", "label", "subject", "text"]) {
    const v = p[key];
    if (typeof v === "string" && v) {
      const value = p["value"];
      return key === "subject" && typeof value === "string"
        ? `${v}: ${value}`
        : v;
    }
  }
  return `${node.labels[0] ?? "node"} ${node.id}`;
}

function TreeNode({
  node,
  out,
  path,
  depth,
  onSelect,
  selectedId,
}: {
  node: GraphNode;
  out: Map<string, { rel: GraphRelationship; node: GraphNode }[]>;
  path: Set<string>;
  depth: number;
  onSelect: (n: GraphNode) => void;
  selectedId: string | null;
}) {
  const children = (out.get(node.id) ?? []).filter(
    (c) => !path.has(c.node.id), // guard cycles
  );
  const nextPath = new Set(path);
  nextPath.add(node.id);

  return (
    <div className={depth > 0 ? "ml-5 border-l border-hairline pl-4" : ""}>
      <button
        onClick={() => onSelect(node)}
        className={`my-0.5 rounded-lg px-2 py-1 text-left text-sm transition-colors ${
          selectedId === node.id
            ? "bg-cta-soft text-ink"
            : "text-ink hover:bg-well"
        }`}
      >
        <span className="mr-2 font-mono text-[10px] uppercase tracking-wider text-ink-faint">
          {node.labels.join(" ")}
        </span>
        {nodeDisplayName(node)}
      </button>
      {children.map(({ rel, node: child }) => (
        <div key={`${rel.id}-${child.id}`}>
          <div className="ml-5 pl-4 font-mono text-[10px] uppercase tracking-wider text-cta">
            {rel.type} →
          </div>
          <TreeNode
            node={child}
            out={out}
            path={nextPath}
            depth={depth + 1}
            onSelect={onSelect}
            selectedId={selectedId}
          />
        </div>
      ))}
    </div>
  );
}

function ProvenanceHops({ path }: { path: GraphHop[] }) {
  if (path.length === 0)
    return (
      <p className="text-sm text-ink-muted">
        No provenance path returned for that id.
      </p>
    );
  return (
    <ol className="space-y-1.5">
      {path.map((hop, i) => (
        <li key={i} className="text-sm">
          <span className="text-ink">{nodeDisplayName(hop.from)}</span>
          <span className="mx-2 font-mono text-[10px] uppercase tracking-wider text-cta">
            —{hop.relationship.type}→
          </span>
          <span className="text-ink">{nodeDisplayName(hop.to)}</span>
        </li>
      ))}
    </ol>
  );
}

function ContextGraphTab({ patientId }: { patientId: string }) {
  const graph = useApi(() => api.getContextGraph(patientId), [patientId]);
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [provId, setProvId] = useState("");
  const [provState, setProvState] = useState<{
    loading: boolean;
    error: string | null;
    path: GraphHop[] | null;
  }>({ loading: false, error: null, path: null });

  const tree = useMemo(() => {
    const nodes = graph.data?.nodes ?? [];
    const rels = graph.data?.relationships ?? [];
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const out = new Map<string, { rel: GraphRelationship; node: GraphNode }[]>();
    const hasIncoming = new Set<string>();
    for (const rel of rels) {
      const child = byId.get(rel.end_id);
      if (!child || !byId.has(rel.start_id)) continue;
      if (!out.has(rel.start_id)) out.set(rel.start_id, []);
      out.get(rel.start_id)!.push({ rel, node: child });
      hasIncoming.add(rel.end_id);
    }
    let roots = nodes.filter((n) => n.labels.includes("Patient"));
    if (roots.length === 0) roots = nodes.filter((n) => !hasIncoming.has(n.id));
    if (roots.length === 0) roots = nodes.slice(0, 1);
    return { roots, out };
  }, [graph.data]);

  async function runProvenance() {
    if (!provId.trim()) return;
    setProvState({ loading: true, error: null, path: null });
    try {
      const res = await api.getProvenance(patientId, provId.trim());
      setProvState({ loading: false, error: null, path: res.path });
    } catch (err) {
      setProvState({ loading: false, error: describeError(err), path: null });
    }
  }

  if (!patientId)
    return (
      <p className="text-sm text-ink-muted">
        Enter a patient id above — visiting a patient page remembers it
        automatically.
      </p>
    );
  if (graph.loading)
    return <LoadingState label="Reading context graph from Neo4j…" />;
  if (graph.error)
    return (
      <ErrorState
        error={graph.error}
        onRetry={graph.reload}
        title="Context graph could not be read from Neo4j."
      />
    );

  return (
    <div>
      <p className="mb-4 text-xs text-ink-faint">
        Rendered from a live Neo4j read — {graph.data?.nodes.length ?? 0} nodes,{" "}
        {graph.data?.relationships.length ?? 0} relationships.
      </p>
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <div className="overflow-x-auto">
          {tree.roots.map((root) => (
            <TreeNode
              key={root.id}
              node={root}
              out={tree.out}
              path={new Set()}
              depth={0}
              onSelect={setSelected}
              selectedId={selected?.id ?? null}
            />
          ))}
        </div>
        <div className="space-y-6">
          <div className="rounded-xl border border-hairline p-4">
            <EyebrowLabel>Node detail</EyebrowLabel>
            {selected ? (
              <dl className="mt-3 space-y-1.5 text-sm">
                <div className="flex gap-2">
                  <dt className="w-32 shrink-0 font-mono text-xs uppercase tracking-wider text-ink-faint">
                    labels
                  </dt>
                  <dd className="text-ink">{selected.labels.join(", ")}</dd>
                </div>
                {Object.entries(selected.properties ?? {}).map(([k, v]) => (
                  <div key={k} className="flex gap-2">
                    <dt className="w-32 shrink-0 break-all font-mono text-xs uppercase tracking-wider text-ink-faint">
                      {k}
                    </dt>
                    <dd className="break-all text-ink">{String(v)}</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="mt-2 text-sm text-ink-muted">
                Click a node to see its source, time, confidence and encounter.
              </p>
            )}
          </div>

          <div className="rounded-xl border border-hairline p-4">
            <EyebrowLabel>Provenance query</EyebrowLabel>
            <p className="mt-2 text-sm text-ink-muted">
              Trace a patient instruction or care-plan action back through the
              approved action and generating recommendation to the transcript
              utterance behind it (multi-hop Neo4j query).
            </p>
            <div className="mt-3 flex gap-2">
              <input
                value={provId}
                onChange={(e) => setProvId(e.target.value)}
                placeholder="instruction or action id"
                className="w-full rounded-lg border border-hairline-strong bg-card px-3 py-2 font-mono text-xs outline-none focus:border-cta"
              />
              <GhostButton onClick={runProvenance} disabled={provState.loading}>
                {provState.loading ? "Tracing…" : "Trace"}
              </GhostButton>
            </div>
            <div className="mt-3">
              {provState.error && (
                <p className="text-sm text-bad">{provState.error}</p>
              )}
              {provState.path && <ProvenanceHops path={provState.path} />}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------- Evals tab ------------------------------ */

function EvalsTab({ encounterId }: { encounterId: string }) {
  const evals = useApi(() => api.getEvals(encounterId), [encounterId]);

  if (!encounterId)
    return (
      <p className="text-sm text-ink-muted">
        Enter an encounter id above — run an encounter first and it is
        remembered automatically.
      </p>
    );
  if (evals.loading) return <LoadingState label="Loading evaluator results…" />;
  if (evals.error)
    return <ErrorState error={evals.error} onRetry={evals.reload} />;

  return (
    <div className="space-y-8">
      <div>
        <div className="flex items-center justify-between">
          <EyebrowLabel>Session evaluators</EyebrowLabel>
          <StatusChip tone="warn">
            Prototype evaluator — not clinical validation
          </StatusChip>
        </div>
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-hairline text-left font-mono text-[11px] uppercase tracking-wider text-ink-faint">
                <th className="py-2 pr-4 font-medium">Evaluator</th>
                <th className="py-2 pr-4 font-medium">Kind</th>
                <th className="py-2 pr-4 font-medium">Score</th>
                <th className="py-2 pr-4 font-medium">Result</th>
                <th className="py-2 font-medium">Detail</th>
              </tr>
            </thead>
            <tbody>
              {(evals.data?.results ?? []).map((r, i) => (
                <tr key={i} className="border-b border-hairline last:border-b-0">
                  <td className="py-2.5 pr-4 text-ink">{r.evaluator}</td>
                  <td className="py-2.5 pr-4 text-ink-muted">{r.kind}</td>
                  <td className="py-2.5 pr-4 font-mono tabular-nums text-ink">
                    {r.score}
                  </td>
                  <td className="py-2.5 pr-4">
                    <StatusChip tone={r.passed ? "good" : "bad"}>
                      {r.passed ? "PASS" : "FAIL"}
                    </StatusChip>
                  </td>
                  <td className="py-2.5 text-ink-muted">{r.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {(evals.data?.results ?? []).length === 0 && (
            <p className="py-3 text-sm text-ink-muted">
              No evaluator results for this encounter yet.
            </p>
          )}
        </div>
      </div>

      <div>
        <EyebrowLabel>Launch criteria</EyebrowLabel>
        <p className="mt-1 text-xs text-ink-faint">
          What would have to be true to ship — a regression harness, never
          validation.
        </p>
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-hairline text-left font-mono text-[11px] uppercase tracking-wider text-ink-faint">
                <th className="py-2 pr-4 font-medium">Criterion</th>
                <th className="py-2 pr-4 font-medium">Target</th>
                <th className="py-2 pr-4 font-medium">Actual</th>
                <th className="py-2 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {(evals.data?.launch_criteria ?? []).map((c, i) => (
                <tr key={i} className="border-b border-hairline last:border-b-0">
                  <td className="py-2.5 pr-4 text-ink">{c.name}</td>
                  <td className="py-2.5 pr-4 font-mono text-xs text-ink-muted">
                    {c.target}
                  </td>
                  <td className="py-2.5 pr-4 font-mono text-xs text-ink">
                    {c.actual}
                  </td>
                  <td className="py-2.5">
                    <StatusChip tone={c.passed ? "good" : "bad"}>
                      {c.passed ? "GREEN" : "RED"}
                    </StatusChip>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {(evals.data?.launch_criteria ?? []).length === 0 && (
            <p className="py-3 text-sm text-ink-muted">
              No launch-criteria rows returned.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

/* --------------------------------- page --------------------------------- */

export default function AiOperationsPage() {
  const [tab, setTab] = useState<Tab>("Trace Summary");
  const [encounterId, setEncounterId] = useStoredId(
    STORAGE_KEYS.lastEncounterId,
  );
  const [patientId, setPatientId] = useStoredId(STORAGE_KEYS.lastPatientId);

  // Remembered ids go stale after `make reset-runtime` deletes encounters
  // (observed: honest 404 on the Trace Summary tab). Default to the most
  // recent encounter when nothing is remembered, and self-heal by re-checking
  // whenever the remembered encounter no longer exists.
  useEffect(() => {
    let cancelled = false;
    async function adopt() {
      try {
        if (encounterId) {
          try {
            await api.getEncounter(encounterId);
            return; // remembered id still exists
          } catch {
            /* stale — fall through to latest */
          }
        }
        const latest = await api.getLatestEncounter();
        if (!cancelled) {
          setEncounterId(latest.encounter.id);
          if (latest.encounter.patient_id) setPatientId(latest.encounter.patient_id);
        }
      } catch {
        /* no encounters at all — tabs show their honest empty/error states */
      }
    }
    adopt();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <AppShell>
      <EyebrowLabel>Developer view</EyebrowLabel>
      <h1 className="mt-1 font-display text-4xl tracking-tight">AI Operations</h1>

      <div className="mt-6 flex flex-wrap items-center gap-4">
        <div className="flex rounded-full border border-hairline bg-card p-1">
          {TABS.map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`rounded-full px-5 py-1.5 text-sm transition-colors ${
                tab === t
                  ? "bg-ink font-medium text-paper"
                  : "text-ink-muted hover:text-ink"
              }`}
            >
              {t}
            </button>
          ))}
        </div>
        <label className="flex items-center gap-2 text-xs text-ink-muted">
          encounter
          <input
            value={encounterId}
            onChange={(e) => setEncounterId(e.target.value)}
            placeholder="encounter id"
            className="w-44 rounded-lg border border-hairline-strong bg-card px-2.5 py-1.5 font-mono text-xs outline-none focus:border-cta"
          />
        </label>
        <label className="flex items-center gap-2 text-xs text-ink-muted">
          patient
          <input
            value={patientId}
            onChange={(e) => setPatientId(e.target.value)}
            placeholder="patient id"
            className="w-44 rounded-lg border border-hairline-strong bg-card px-2.5 py-1.5 font-mono text-xs outline-none focus:border-cta"
          />
        </label>
      </div>

      <Card className="mt-6">
        {tab === "Trace Summary" && <TraceSummaryTab encounterId={encounterId} />}
        {tab === "Context Graph" && <ContextGraphTab patientId={patientId} />}
        {tab === "Evals" && <EvalsTab encounterId={encounterId} />}
      </Card>
    </AppShell>
  );
}
