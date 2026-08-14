/**
 * WebSocket client for `/api/encounters/{id}/stream` — envelope protocol per
 * contracts/CONTRACTS.md:
 *   - binary frames (client -> server): raw 16 kHz mono Int16 PCM audio
 *   - text frames (both directions):   JSON envelope {"type": string, ...}
 *
 * Auth is a one-time short-lived ticket from POST /api/encounters, sent in the
 * `session.start` envelope — never in the query string.
 *
 * No silent auto-reconnect: the ticket is one-time, and spec §2.4 requires the
 * UI to show honest disconnect states instead of pretending. The UI owns any
 * "start a new session" affordance.
 */

import type { Fact, SpeakerRole, Suggestion, TranscriptSegment } from "./types";

/* ---------------------- client -> server envelopes ---------------------- */

export type SessionMode = "live" | "replay";

export interface SessionStartMessage {
  type: "session.start";
  mode: SessionMode;
  /** One-time short-lived ticket from POST /api/encounters. */
  ticket: string;
}

export interface SpeakerCorrectMessage {
  type: "speaker.correct";
  segment_id: string;
  speaker: SpeakerRole;
}

export interface SessionEndMessage {
  type: "session.end";
}

export interface SuggestionDismissMessage {
  type: "suggestion.dismiss";
  suggestion_id: string;
}

export type ClientMessage =
  | SessionStartMessage
  | SpeakerCorrectMessage
  | SessionEndMessage
  | SuggestionDismissMessage;

/* ---------------------- server -> client envelopes ---------------------- */

export type DeepgramStatus = "connected" | "reconnecting" | "degraded" | "error";

export interface TranscriptInterimEvent {
  type: "transcript.interim";
  segment: TranscriptSegment;
}
export interface TranscriptFinalEvent {
  type: "transcript.final";
  segment: TranscriptSegment;
}
export interface StateFactEvent {
  type: "state.fact";
  fact: Fact;
  change: "added" | "updated" | "disputed";
}
export interface SuggestionActiveEvent {
  type: "suggestion.active";
  suggestion: Suggestion;
}
export interface SuggestionRemoveEvent {
  type: "suggestion.remove";
  suggestion_id: string;
}
export interface ConnStatusEvent {
  type: "conn.status";
  deepgram: DeepgramStatus;
  detail: string;
}
export interface SessionFinalizingEvent {
  type: "session.finalizing";
}
export interface SessionFinalizedEvent {
  type: "session.finalized";
  encounter_id: string;
}
export interface StreamErrorEvent {
  type: "error";
  scope: "deepgram" | "openai" | "internal";
  message: string;
  recoverable: boolean;
}

export type ServerEvent =
  | TranscriptInterimEvent
  | TranscriptFinalEvent
  | StateFactEvent
  | SuggestionActiveEvent
  | SuggestionRemoveEvent
  | ConnStatusEvent
  | SessionFinalizingEvent
  | SessionFinalizedEvent
  | StreamErrorEvent;

/* ------------------------------ client ---------------------------------- */

export type SocketStatus =
  | "idle"
  | "connecting"
  | "open"
  | "closing"
  | "closed"
  | "error";

export interface EncounterStreamHandlers {
  onEvent: (event: ServerEvent) => void;
  onSocketStatus?: (status: SocketStatus) => void;
}

/**
 * WS base URL. REST goes through the Next proxy, but WebSockets connect to the
 * backend directly (Next standalone rewrites don't proxy WS). Demo default:
 * backend on the same host, port 8000.
 */
export function wsBaseUrl(): string {
  const fromEnv = process.env.NEXT_PUBLIC_WS_BASE;
  if (fromEnv) return fromEnv.replace(/\/$/, "");
  if (typeof window === "undefined") return "ws://localhost:8002";
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.hostname}:8002`;
}

export function encounterStreamUrl(encounterId: string): string {
  return `${wsBaseUrl()}/api/encounters/${encodeURIComponent(encounterId)}/stream`;
}

export class EncounterStream {
  private ws: WebSocket | null = null;
  private _status: SocketStatus = "idle";

  constructor(
    private readonly encounterId: string,
    private readonly handlers: EncounterStreamHandlers,
  ) {}

  get status(): SocketStatus {
    return this._status;
  }

  private setStatus(status: SocketStatus) {
    this._status = status;
    this.handlers.onSocketStatus?.(status);
  }

  /** Opens the socket and sends `session.start` once connected. */
  connect(opts: { mode: SessionMode; ticket: string }): void {
    if (this.ws) this.close();
    this.setStatus("connecting");

    const ws = new WebSocket(encounterStreamUrl(this.encounterId));
    ws.binaryType = "arraybuffer";
    this.ws = ws;

    ws.onopen = () => {
      this.setStatus("open");
      this.send({ type: "session.start", mode: opts.mode, ticket: opts.ticket });
    };

    ws.onmessage = (msg: MessageEvent) => {
      if (typeof msg.data !== "string") return; // server sends no binary frames
      let parsed: unknown;
      try {
        parsed = JSON.parse(msg.data);
      } catch {
        console.warn("[ws] non-JSON text frame ignored", msg.data);
        return;
      }
      const event = parsed as ServerEvent;
      if (!event || typeof event.type !== "string") {
        console.warn("[ws] envelope missing type", parsed);
        return;
      }
      this.handlers.onEvent(event);
    };

    ws.onerror = () => {
      this.setStatus("error");
    };

    ws.onclose = () => {
      if (this._status !== "error") this.setStatus("closed");
      this.ws = null;
    };
  }

  private send(message: ClientMessage): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message));
    }
  }

  /** Raw 16 kHz mono Int16 PCM audio (binary frame). */
  sendAudio(chunk: ArrayBuffer): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(chunk);
    }
  }

  /** Manual Doctor/Patient toggle (spec §8A — primary diarization path). */
  correctSpeaker(segmentId: string, speaker: SpeakerRole): void {
    this.send({ type: "speaker.correct", segment_id: segmentId, speaker });
  }

  /** Tells the server a suggestion was dismissed by the clinician. */
  sendSuggestionDismiss(suggestionId: string): void {
    this.send({ type: "suggestion.dismiss", suggestion_id: suggestionId });
  }

  /** Triggers server-side finalize; server flushes Deepgram. */
  endSession(): void {
    this.send({ type: "session.end" });
  }

  close(): void {
    if (!this.ws) return;
    this.setStatus("closing");
    try {
      this.ws.close();
    } finally {
      this.ws = null;
    }
  }
}
