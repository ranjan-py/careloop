"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import { QADrawer } from "@/components/QADrawer";
import { DegradedNote, ErrorState, LoadingState } from "@/components/States";
import {
  Card,
  EyebrowLabel,
  GhostButton,
  PillButton,
  StatusChip,
} from "@/components/ui";
import type { ChipTone } from "@/components/ui";
import { api, describeError } from "@/lib/api";
import { MicCapture } from "@/lib/audio";
import { formatAge, formatClock, titleCase } from "@/lib/format";
import { rememberId, STORAGE_KEYS, useApi } from "@/lib/hooks";
import type { Fact, Suggestion, TranscriptSegment } from "@/lib/types";
import {
  EncounterStream,
  type DeepgramStatus,
  type ServerEvent,
  type SessionMode,
  type SocketStatus,
  type StreamErrorEvent,
} from "@/lib/ws";

const SOCKET_TONES: Record<SocketStatus, ChipTone> = {
  idle: "neutral",
  connecting: "warn",
  open: "good",
  closing: "warn",
  closed: "neutral",
  error: "bad",
};

const DEEPGRAM_TONES: Record<DeepgramStatus, ChipTone> = {
  connected: "good",
  reconnecting: "warn",
  degraded: "warn",
  error: "bad",
};

const SUGGESTION_KIND_LABELS: Record<Suggestion["kind"], string> = {
  question: "Next best question",
  info_gap: "Information gap",
  action: "Suggested action",
};

function ticketStorageKey(encounterId: string) {
  return `careloop.ticket.${encounterId}`;
}

export default function LiveEncounterPage() {
  const params = useParams<{ id: string }>();
  const encounterId = params.id;
  const router = useRouter();

  const detail = useApi(() => api.getEncounter(encounterId), [encounterId]);

  /* ----------------------------- local state ---------------------------- */
  const [finalSegments, setFinalSegments] = useState<TranscriptSegment[]>([]);
  const [interim, setInterim] = useState<Record<string, TranscriptSegment>>({});
  const [facts, setFacts] = useState<Record<string, Fact>>({});
  const [factChanges, setFactChanges] = useState<Record<string, string>>({});
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [dismissed, setDismissed] = useState<Record<string, boolean>>({});
  const [socketStatus, setSocketStatus] = useState<SocketStatus>("idle");
  const [deepgram, setDeepgram] = useState<{
    status: DeepgramStatus;
    detail: string;
  } | null>(null);
  const [mode, setMode] = useState<SessionMode | null>(null);
  const [micOn, setMicOn] = useState(false);
  const [finalizing, setFinalizing] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [streamErrors, setStreamErrors] = useState<StreamErrorEvent[]>([]);
  const [uiError, setUiError] = useState<string | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const hydrated = useRef(false);

  const streamRef = useRef<EncounterStream | null>(null);
  const micRef = useRef<MicCapture | null>(null);
  const transcriptRef = useRef<HTMLDivElement | null>(null);

  /* ----------------------- hydrate from GET /encounters ------------------ */
  useEffect(() => {
    const d = detail.data;
    if (!d || hydrated.current) return;
    hydrated.current = true;
    setFinalSegments(d.transcript ?? []);
    setFacts(Object.fromEntries((d.facts ?? []).map((f) => [f.id, f])));
    setSuggestions((d.suggestions ?? []).filter((s) => s.status === "active"));
    rememberId(STORAGE_KEYS.lastEncounterId, d.encounter.id);
    if (d.encounter.patient_id) {
      rememberId(STORAGE_KEYS.lastPatientId, d.encounter.patient_id);
    }
  }, [detail.data]);

  /* ------------------------------ elapsed clock -------------------------- */
  useEffect(() => {
    if (startedAt === null) return;
    const t = window.setInterval(
      () => setElapsed((Date.now() - startedAt) / 1000),
      1000,
    );
    return () => window.clearInterval(t);
  }, [startedAt]);

  /* ------------------------------ WS handling ---------------------------- */
  const finishAndGo = useCallback(async () => {
    setGenerating(true);
    try {
      const res = await api.endEncounter(encounterId);
      rememberId(STORAGE_KEYS.lastCarePlanId, res.care_plan.id);
      router.push(`/encounters/${encounterId}/care-plan`);
    } catch (err) {
      setGenerating(false);
      setFinalizing(false);
      setUiError(describeError(err));
    }
  }, [encounterId, router]);

  const handleEvent = useCallback(
    (event: ServerEvent) => {
      switch (event.type) {
        case "transcript.interim":
          setInterim((prev) => ({ ...prev, [event.segment.id]: event.segment }));
          break;
        case "transcript.final":
          setInterim((prev) => {
            const next = { ...prev };
            delete next[event.segment.id];
            return next;
          });
          setFinalSegments((prev) => {
            const idx = prev.findIndex((s) => s.id === event.segment.id);
            if (idx >= 0) {
              const next = prev.slice();
              next[idx] = event.segment;
              return next;
            }
            return [...prev, event.segment];
          });
          break;
        case "state.fact":
          setFacts((prev) => ({ ...prev, [event.fact.id]: event.fact }));
          setFactChanges((prev) => ({ ...prev, [event.fact.id]: event.change }));
          break;
        case "suggestion.active":
          setSuggestions((prev) => {
            const without = prev.filter((s) => s.id !== event.suggestion.id);
            return [...without, event.suggestion];
          });
          break;
        case "suggestion.remove":
          setSuggestions((prev) =>
            prev.filter((s) => s.id !== event.suggestion_id),
          );
          break;
        case "conn.status":
          setDeepgram({ status: event.deepgram, detail: event.detail });
          break;
        case "session.finalizing":
          setFinalizing(true);
          break;
        case "session.finalized":
          setFinalizing(true);
          void finishAndGo();
          break;
        case "error":
          setStreamErrors((prev) => [...prev, event]);
          break;
      }
    },
    [finishAndGo],
  );
  const handleEventRef = useRef(handleEvent);
  handleEventRef.current = handleEvent;

  const takeTicket = useCallback((): string | null => {
    try {
      const stored = window.sessionStorage.getItem(ticketStorageKey(encounterId));
      if (stored) return stored;
    } catch {
      /* storage unavailable */
    }
    return detail.data?.encounter.stream_ticket ?? null;
  }, [detail.data, encounterId]);

  const startSession = useCallback(
    async (sessionMode: SessionMode) => {
      setUiError(null);
      const ticket = takeTicket();
      if (!ticket) {
        setUiError(
          "No WebSocket ticket available for this encounter. Tickets are " +
            "one-time and minted by POST /api/encounters — start the visit " +
            "from the patient page (or start a new encounter).",
        );
        return;
      }
      const stream = new EncounterStream(encounterId, {
        onEvent: (ev) => handleEventRef.current(ev),
        onSocketStatus: setSocketStatus,
      });
      streamRef.current = stream;
      stream.connect({ mode: sessionMode, ticket });
      try {
        window.sessionStorage.removeItem(ticketStorageKey(encounterId));
      } catch {
        /* fine */
      }
      setMode(sessionMode);
      setStartedAt(Date.now());

      if (sessionMode === "live") {
        const mic = new MicCapture();
        micRef.current = mic;
        try {
          await mic.start((chunk) => streamRef.current?.sendAudio(chunk));
          setMicOn(true);
        } catch (err) {
          setUiError(describeError(err));
          streamRef.current?.close();
          streamRef.current = null;
          setMode(null);
          setStartedAt(null);
        }
      }
    },
    [encounterId, takeTicket],
  );

  const endVisit = useCallback(async () => {
    setUiError(null);
    if (micRef.current) {
      await micRef.current.stop();
      micRef.current = null;
      setMicOn(false);
    }
    if (streamRef.current && streamRef.current.status === "open") {
      setFinalizing(true);
      streamRef.current.endSession();
      // navigation continues on the session.finalized event
    } else {
      // No live socket — run the end-of-encounter pipeline over REST directly.
      setFinalizing(true);
      void finishAndGo();
    }
  }, [finishAndGo]);

  /* cleanup on unmount */
  useEffect(() => {
    return () => {
      void micRef.current?.stop();
      streamRef.current?.close();
    };
  }, []);

  const toggleSpeaker = useCallback((segment: TranscriptSegment) => {
    const next = segment.speaker === "doctor" ? "patient" : "doctor";
    setFinalSegments((prev) =>
      prev.map((s) => (s.id === segment.id ? { ...s, speaker: next } : s)),
    );
    streamRef.current?.correctSpeaker(segment.id, next);
  }, []);

  /* ------------------------------- derived -------------------------------- */
  const sessionActive = mode !== null && !finalizing;
  const allFacts = Object.values(facts);
  const newToday = allFacts
    .filter((f) => f.encounter_id === encounterId)
    .sort((a, b) => (a.ingested_at < b.ingested_at ? 1 : -1));
  const chartFacts = allFacts.filter((f) => f.encounter_id !== encounterId);
  const activeSuggestions = suggestions
    .filter((s) => !dismissed[s.id])
    .slice(-2);
  const interimSegments = Object.values(interim);

  // Sticky auto-scroll: follow the live transcript unless the clinician has
  // deliberately scrolled up to review earlier turns (>160px from bottom).
  useEffect(() => {
    const el = transcriptRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 160;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [finalSegments.length, interimSegments.length]);

  if (detail.loading) {
    return (
      <AppShell>
        <LoadingState label="Loading encounter…" />
      </AppShell>
    );
  }
  if (detail.error || !detail.data) {
    return (
      <AppShell>
        <ErrorState error={detail.error} onRetry={detail.reload} />
      </AppShell>
    );
  }

  return (
    <AppShell>
      {/* header / controls */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <EyebrowLabel>Live encounter</EyebrowLabel>
          <h1 className="mt-1 font-display text-3xl tracking-tight">
            Encounter workspace
          </h1>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="mr-2 font-mono text-lg tabular-nums text-ink">
            {formatClock(elapsed)}
          </span>
          <StatusChip tone={SOCKET_TONES[socketStatus]}>
            socket: {socketStatus}
          </StatusChip>
          <StatusChip tone={deepgram ? DEEPGRAM_TONES[deepgram.status] : "neutral"}>
            deepgram: {deepgram?.status ?? "not started"}
          </StatusChip>
          {!sessionActive && !finalizing && (
            <>
              <PillButton onClick={() => startSession("replay")}>
                Start replay
              </PillButton>
              <GhostButton onClick={() => startSession("live")}>
                Start microphone
              </GhostButton>
            </>
          )}
          {sessionActive && (
            <>
              {micOn && <StatusChip tone="good">mic live</StatusChip>}
              {mode === "replay" && (
                <StatusChip tone="info">replay fixture</StatusChip>
              )}
              <button
                onClick={endVisit}
                className="inline-flex items-center rounded-full bg-ink px-6 py-2.5 text-sm font-semibold text-paper hover:opacity-90"
              >
                End Visit
              </button>
            </>
          )}
          {finalizing && (
            <StatusChip tone="warn">
              {generating ? "Generating care plan…" : "Finalizing…"}
            </StatusChip>
          )}
          {!sessionActive && !finalizing && finalSegments.length > 0 && (
            <GhostButton onClick={endVisit}>Generate care plan</GhostButton>
          )}
        </div>
      </div>

      {deepgram && deepgram.status !== "connected" && (
        <div className="mt-4">
          <DegradedNote
            text={`Deepgram ${deepgram.status}${deepgram.detail ? ` — ${deepgram.detail}` : ""}`}
          />
        </div>
      )}
      {uiError && (
        <p className="mt-4 rounded-xl border border-bad/30 bg-bad-soft px-4 py-2.5 text-sm text-bad">
          {uiError}
        </p>
      )}
      {streamErrors.map((err, i) => (
        <p
          key={i}
          className="mt-2 rounded-xl border border-bad/30 bg-bad-soft px-4 py-2 text-sm text-bad"
        >
          {err.scope} error: {err.message}
          {err.recoverable ? " (recoverable)" : ""}
        </p>
      ))}

      {/* three-column workspace (spec §8) — fills the viewport below the
          header on desktop; each column scrolls internally (user feedback:
          the fixed 32rem transcript cap left the column at half height). */}
      <div className="mt-6 grid grid-cols-1 gap-6 xl:h-[calc(100vh-15rem)] xl:min-h-[28rem] xl:grid-cols-12">
        {/* Column A — Conversation */}
        <Card className="xl:col-span-5 xl:flex xl:min-h-0 xl:flex-col xl:overflow-hidden">
          <div className="flex items-center justify-between">
            <EyebrowLabel>Conversation</EyebrowLabel>
            <span className="text-xs text-ink-faint">
              tap a speaker label to correct it
            </span>
          </div>
          <div
            ref={transcriptRef}
            className="mt-4 max-h-[32rem] space-y-4 overflow-y-auto pr-2 xl:max-h-none xl:min-h-0 xl:flex-1"
          >
            {finalSegments.length === 0 && interimSegments.length === 0 && (
              <p className="text-sm text-ink-muted">
                No transcript yet. Start the replay fixture or the microphone
                to begin streaming through Deepgram.
              </p>
            )}
            {finalSegments.map((seg) => (
              <div key={seg.id} className="flex gap-3">
                <button
                  onClick={() => toggleSpeaker(seg)}
                  title="Toggle Doctor/Patient"
                  className={`h-fit shrink-0 rounded-full px-2.5 py-0.5 font-mono text-[10px] uppercase tracking-wider transition-colors ${
                    seg.speaker === "doctor"
                      ? "bg-cta-soft text-cta hover:bg-cta/20"
                      : "bg-well text-ink-muted hover:bg-hairline"
                  }`}
                >
                  {seg.speaker}
                </button>
                <p className="text-sm leading-relaxed text-ink">{seg.text}</p>
              </div>
            ))}
            {interimSegments.map((seg) => (
              <div key={seg.id} className="flex gap-3 opacity-70">
                <span className="h-fit shrink-0 rounded-full border border-dashed border-hairline-strong px-2.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-ink-faint">
                  {seg.speaker}
                </span>
                <p className="font-display text-sm italic leading-relaxed text-ink-muted">
                  {seg.text}
                  <span className="ml-1 font-mono text-[10px] not-italic text-ink-faint">
                    interim
                  </span>
                </p>
              </div>
            ))}
          </div>
        </Card>

        {/* Column B — Live patient state */}
        <Card className="xl:col-span-4 xl:min-h-0 xl:overflow-y-auto">
          <EyebrowLabel>Live patient state</EyebrowLabel>
          {newToday.length > 0 && (
            <div className="mt-4">
              <p className="font-mono text-[11px] font-semibold uppercase tracking-[0.16em] text-cta">
                New today
              </p>
              <ul className="mt-2 space-y-3">
                {newToday.map((fact) => (
                  <FactRow
                    key={fact.id}
                    fact={fact}
                    change={factChanges[fact.id]}
                    conflictsWith={
                      fact.conflicts_with ? facts[fact.conflicts_with] : undefined
                    }
                  />
                ))}
              </ul>
            </div>
          )}
          <div className="mt-5">
            <p className="font-mono text-[11px] uppercase tracking-[0.16em] text-ink-faint">
              From chart
            </p>
            {chartFacts.length === 0 ? (
              <p className="mt-2 text-sm text-ink-muted">
                No chart facts loaded for this encounter.
              </p>
            ) : (
              <ul className="mt-2 space-y-3">
                {chartFacts.map((fact) => (
                  <FactRow
                    key={fact.id}
                    fact={fact}
                    conflictsWith={
                      fact.conflicts_with ? facts[fact.conflicts_with] : undefined
                    }
                  />
                ))}
              </ul>
            )}
          </div>
        </Card>

        {/* Column C — Next best question / action */}
        <Card className="xl:col-span-3 xl:min-h-0 xl:overflow-y-auto">
          <EyebrowLabel>Next best</EyebrowLabel>
          {activeSuggestions.length === 0 ? (
            <p className="mt-3 text-sm text-ink-muted">
              No active suggestion. Suggestions appear as new facts change the
              working state.
            </p>
          ) : (
            <ul className="mt-3 space-y-3">
              {activeSuggestions.map((s) => (
                <li
                  key={s.id}
                  className="rounded-xl border border-cta/25 bg-cta-soft/60 p-4"
                >
                  <p className="font-mono text-[10px] uppercase tracking-wider text-cta">
                    {SUGGESTION_KIND_LABELS[s.kind]}
                  </p>
                  <p className="mt-1.5 text-sm font-medium leading-snug text-ink">
                    {s.text}
                  </p>
                  <p className="mt-1.5 text-xs leading-relaxed text-ink-muted">
                    {s.rationale}
                  </p>
                  <button
                    onClick={() => {
                      setDismissed((prev) => ({ ...prev, [s.id]: true }));
                      streamRef.current?.sendSuggestionDismiss(s.id);
                    }}
                    className="mt-2 text-xs text-ink-faint underline-offset-2 hover:text-ink hover:underline"
                  >
                    Dismiss
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <QADrawer />
    </AppShell>
  );
}

function FactRow({
  fact,
  change,
  conflictsWith,
}: {
  fact: Fact;
  change?: string;
  conflictsWith?: Fact;
}) {
  const disputed = fact.verification_status === "disputed";
  return (
    <li
      className={`rounded-xl border p-3.5 ${
        disputed ? "border-bad/30 bg-bad-soft/40" : "border-hairline bg-card"
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <p className="text-sm font-medium text-ink">
          {titleCase(fact.subject)}: {fact.value}
        </p>
        {disputed ? (
          <StatusChip tone="bad">disputed</StatusChip>
        ) : change === "added" ? (
          <StatusChip tone="info">new</StatusChip>
        ) : change === "updated" ? (
          <StatusChip tone="warn">updated</StatusChip>
        ) : null}
      </div>
      <p className="mt-1 font-mono text-[11px] text-ink-faint">
        {fact.source_class === "synthea_ehr" ? "EHR (Synthea)" : "patient report"}
        {fact.method ? ` · ${fact.method}` : ""}
        {fact.reported_at ? ` · ${formatAge(fact.reported_at)}` : ""}
        {typeof fact.confidence === "number"
          ? ` · conf ${fact.confidence.toFixed(2)}`
          : ""}
      </p>
      {conflictsWith && (
        <p className="mt-1.5 text-xs text-bad">
          Conflicts with: {titleCase(conflictsWith.subject)} ={" "}
          {conflictsWith.value} ({conflictsWith.source_class})
        </p>
      )}
    </li>
  );
}
