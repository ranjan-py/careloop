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
import { formatAge, formatClock, humanizeFactText } from "@/lib/format";
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
  // Chronological (oldest first) — the column reverses it for display.
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  // Superseded by the §9 max-active cap: kept on screen, de-emphasised.
  const [superseded, setSuperseded] = useState<Record<string, boolean>>({});
  // Dismissed BY THE CLINICIAN — that is a decision, so the card goes away.
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
    // Keep superseded history across a reload (the API returns every status,
    // ordered chronologically); only clinician-dismissed ones are dropped.
    const priorSuggestions = (d.suggestions ?? []).filter(
      (s) => s.status !== "dismissed",
    );
    setSuggestions(priorSuggestions);
    setSuperseded(
      Object.fromEntries(
        priorSuggestions
          .filter((s) => s.status === "superseded")
          .map((s) => [s.id, true]),
      ),
    );
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
          // A re-activated id is active again, whatever it was before.
          setSuperseded((prev) => {
            if (!prev[event.suggestion.id]) return prev;
            const next = { ...prev };
            delete next[event.suggestion.id];
            return next;
          });
          break;
        case "suggestion.remove":
          // The server only emits this for the §9 max-active supersede (a
          // clinician dismissal is persisted without a broadcast), so the card
          // is demoted rather than deleted — a card that silently disappears
          // mid-visit reads as a glitch, not as a policy.
          setSuperseded((prev) => ({ ...prev, [event.suggestion_id]: true }));
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
  // Newest first: the current suggestion is the one at the top of the column,
  // where it stays visible without scrolling. Older ACTIVE ones sit below it
  // (muted, still live); superseded ones collect underneath those.
  const visibleSuggestions = suggestions.filter((s) => !dismissed[s.id]);
  const activeSuggestions = visibleSuggestions
    .filter((s) => !superseded[s.id])
    .reverse();
  const supersededSuggestions = visibleSuggestions
    .filter((s) => superseded[s.id])
    .reverse();
  const interimSegments = Object.values(interim);

  const dismissSuggestion = (id: string) => {
    setDismissed((prev) => ({ ...prev, [id]: true }));
    streamRef.current?.sendSuggestionDismiss(id);
  };

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
        <Card className="min-w-0 xl:col-span-5 xl:flex xl:min-h-0 xl:flex-col xl:overflow-hidden">
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
                <p className={`text-sm leading-relaxed text-ink ${WRAP_ANYWHERE}`}>
                  {seg.text}
                </p>
              </div>
            ))}
            {interimSegments.map((seg) => (
              <div key={seg.id} className="flex gap-3 opacity-70">
                <span className="h-fit shrink-0 rounded-full border border-dashed border-hairline-strong px-2.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-ink-faint">
                  {seg.speaker}
                </span>
                <p
                  className={`font-display text-sm italic leading-relaxed text-ink-muted ${WRAP_ANYWHERE}`}
                >
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
        <Card className="min-w-0 xl:col-span-4 xl:min-h-0 xl:overflow-y-auto">
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
        <Card className="min-w-0 xl:col-span-3 xl:min-h-0 xl:overflow-y-auto">
          <EyebrowLabel>Next best</EyebrowLabel>
          {activeSuggestions.length === 0 && (
            <p className="mt-3 text-sm text-ink-muted">
              No active suggestion. Suggestions appear as new facts change the
              working state.
            </p>
          )}
          {activeSuggestions.length > 0 && (
            <ul className="mt-3 space-y-3">
              {activeSuggestions.map((s, i) => (
                <SuggestionCard
                  key={s.id}
                  suggestion={s}
                  variant={i === 0 ? "active" : "muted"}
                  onDismiss={dismissSuggestion}
                />
              ))}
            </ul>
          )}
          {supersededSuggestions.length > 0 && (
            <div className="mt-6">
              <p className="font-mono text-[11px] uppercase tracking-[0.16em] text-ink-faint">
                Earlier this visit
              </p>
              <p className="mt-1 text-xs text-ink-faint">
                Superseded to keep at most two suggestions live at a time — kept
                here, and logged.
              </p>
              <ul className="mt-2.5 space-y-2">
                {supersededSuggestions.map((s) => (
                  <SuggestionCard
                    key={s.id}
                    suggestion={s}
                    variant="superseded"
                  />
                ))}
              </ul>
            </div>
          )}
        </Card>
      </div>

      <QADrawer />
    </AppShell>
  );
}

/**
 * Extraction output is unbounded text the layout must survive. A single long
 * token with no spaces ("fresh_labs_needed_before_next_medication_decision")
 * sets a min-content width that a grid/flex child will NOT shrink below, so it
 * widens the column and scrolls the whole page sideways. Three things are
 * needed together and any one alone is insufficient: min-w-0 on the grid child
 * (the Card), min-w-0 on the flex child, and a break rule that can split
 * inside a word.
 */
const WRAP_ANYWHERE = "min-w-0 break-words [overflow-wrap:anywhere]";

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
        {/* title carries the raw extracted strings — the humanised form is
            presentation only. */}
        <p
          className={`flex-1 text-sm font-medium text-ink ${WRAP_ANYWHERE}`}
          title={`${fact.subject}: ${fact.value}`}
        >
          {humanizeFactText(fact.subject)}: {humanizeFactText(fact.value)}
        </p>
        {disputed ? (
          <StatusChip tone="bad" className="shrink-0">
            disputed
          </StatusChip>
        ) : change === "added" ? (
          <StatusChip tone="info" className="shrink-0">
            new
          </StatusChip>
        ) : change === "updated" ? (
          <StatusChip tone="warn" className="shrink-0">
            updated
          </StatusChip>
        ) : null}
      </div>
      <p className={`mt-1 font-mono text-[11px] text-ink-faint ${WRAP_ANYWHERE}`}>
        {fact.source_class === "synthea_ehr" ? "EHR (Synthea)" : "patient report"}
        {fact.method ? ` · ${fact.method.replace(/_/g, " ")}` : ""}
        {fact.reported_at ? ` · ${formatAge(fact.reported_at)}` : ""}
        {typeof fact.confidence === "number"
          ? ` · conf ${fact.confidence.toFixed(2)}`
          : ""}
      </p>
      {conflictsWith && (
        <p className={`mt-1.5 text-xs text-bad ${WRAP_ANYWHERE}`}>
          Conflicts with: {humanizeFactText(conflictsWith.subject)} ={" "}
          {humanizeFactText(conflictsWith.value)} ({conflictsWith.source_class})
        </p>
      )}
    </li>
  );
}

/* ---------------------------- Next best cards ---------------------------- */

type SuggestionVariant = "active" | "muted" | "superseded";

/**
 * One suggestion card. Exactly ONE card on screen wears the accent — the most
 * recent active one — so "what should I ask next" is unambiguous and the
 * design system's single-CTA rule holds. Older ACTIVE suggestions demote to a
 * neutral card but stay fully readable and dismissible: they are less recent,
 * not less valid. Superseded ones (spec §9 caps active suggestions at 2) are
 * kept on screen in a faint state rather than deleted, so the policy is
 * visible instead of looking like a card that vanished.
 */
function SuggestionCard({
  suggestion,
  variant,
  onDismiss,
}: {
  suggestion: Suggestion;
  variant: SuggestionVariant;
  onDismiss?: (id: string) => void;
}) {
  const shell =
    variant === "active"
      ? "border-cta/25 bg-cta-soft/60"
      : variant === "muted"
        ? "border-hairline bg-card"
        : "border-hairline bg-well/40";
  const kindTone =
    variant === "active"
      ? "text-cta"
      : variant === "muted"
        ? "text-ink-muted"
        : "text-ink-faint";

  return (
    <li
      className={`rounded-xl border transition-colors duration-300 ${shell} ${
        variant === "superseded" ? "p-3" : "p-4"
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <p
          className={`font-mono text-[10px] uppercase tracking-wider ${kindTone} ${WRAP_ANYWHERE}`}
        >
          {SUGGESTION_KIND_LABELS[suggestion.kind]}
        </p>
        {/* State is never carried by colour alone. */}
        {variant === "active" && (
          <StatusChip tone="info" className="shrink-0">
            now
          </StatusChip>
        )}
        {variant === "superseded" && (
          <StatusChip tone="neutral" className="shrink-0">
            superseded
          </StatusChip>
        )}
      </div>
      <p
        className={`mt-1.5 text-sm leading-snug ${WRAP_ANYWHERE} ${
          variant === "superseded"
            ? "text-ink-muted"
            : "font-medium text-ink"
        }`}
      >
        {suggestion.text}
      </p>
      {variant !== "superseded" && (
        <p className={`mt-1.5 text-xs leading-relaxed text-ink-muted ${WRAP_ANYWHERE}`}>
          {suggestion.rationale}
        </p>
      )}
      {onDismiss && (
        <button
          onClick={() => onDismiss(suggestion.id)}
          className="mt-2 text-xs text-ink-faint underline-offset-2 hover:text-ink hover:underline"
        >
          Dismiss
        </button>
      )}
    </li>
  );
}
