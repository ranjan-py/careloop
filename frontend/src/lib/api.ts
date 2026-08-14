/**
 * Typed fetch client for every REST route in contracts/CONTRACTS.md.
 *
 * All calls go to same-origin `/api/*`; next.config.ts proxies them to the
 * backend (BACKEND_URL). Auth is the session cookie set by login.
 *
 * Failure policy (spec §2.4): errors are surfaced, never papered over.
 *  - network failure  -> BackendUnreachableError (render "backend unreachable")
 *  - HTTP error       -> ApiError with status + body detail
 */

import type {
  AnalyticsFeedback,
  CarePlan,
  CarePlanAction,
  Clinician,
  CriterionRow,
  Encounter,
  EvalResult,
  EvidenceSnippet,
  ExecutionRecord,
  Fact,
  GraphHop,
  GraphNode,
  GraphRelationship,
  HealthResponse,
  PatientDetail,
  PatientListItem,
  RejectionCategory,
  Suggestion,
  TimelineEvent,
  TraceSummaryRow,
  TranscriptSegment,
} from "./types";

/** Override for unusual setups; default is same-origin (proxied). */
const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

export class BackendUnreachableError extends Error {
  constructor(cause?: unknown) {
    super(
      "Backend unreachable — the CareLoop API did not respond. " +
        "Check that the backend container is running on port 8000.",
    );
    this.name = "BackendUnreachableError";
    this.cause = cause;
  }
}

export class ApiError extends Error {
  status: number;
  body?: unknown;
  constructor(status: number, message: string, body?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api${path}`, {
      credentials: "include",
      cache: "no-store",
      ...init,
      headers: {
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...init?.headers,
      },
    });
  } catch (err) {
    throw new BackendUnreachableError(err);
  }

  if (!res.ok) {
    let body: unknown;
    let detail = res.statusText;
    try {
      body = await res.json();
      const b = body as { detail?: unknown; message?: unknown };
      if (typeof b?.detail === "string") detail = b.detail;
      else if (typeof b?.message === "string") detail = b.message;
    } catch {
      /* non-JSON error body — keep statusText */
    }
    throw new ApiError(res.status, `API error ${res.status}: ${detail}`, body);
  }

  return (await res.json()) as T;
}

const get = <T>(path: string) => request<T>(path);
const post = <T>(path: string, body?: unknown) =>
  request<T>(path, {
    method: "POST",
    body: body === undefined ? JSON.stringify({}) : JSON.stringify(body),
  });

/* ---------------- response envelopes (per CONTRACTS.md) ---------------- */

export interface LoginResponse {
  ok: boolean;
  clinician: Clinician;
}
export interface EncounterDetailResponse {
  encounter: Encounter;
  facts: Fact[];
  suggestions: Suggestion[];
  transcript: TranscriptSegment[];
}
export interface EndEncounterResponse {
  encounter: Encounter;
  care_plan: CarePlan;
}
export interface FinalizeResponse {
  care_plan: CarePlan;
  executions: ExecutionRecord[];
  clinician_summary: string;
  patient_instructions: string;
}
export interface GraphResponse {
  nodes: GraphNode[];
  relationships: GraphRelationship[];
}
export interface EvalsResponse {
  results: EvalResult[];
  launch_criteria: CriterionRow[];
}

export const api = {
  /* auth */
  login: (email: string, password: string) =>
    post<LoginResponse>("/auth/login", { email, password }),
  logout: () => post<{ ok: boolean }>("/auth/logout"),
  me: () => get<{ clinician: Clinician }>("/auth/me"),

  /* patients */
  listPatients: () => get<{ patients: PatientListItem[] }>("/patients"),
  getPatient: (patientId: string) =>
    get<{ patient: PatientDetail }>(`/patients/${encodeURIComponent(patientId)}`),
  getPatientTimeline: (patientId: string) =>
    get<{ events: TimelineEvent[] }>(
      `/patients/${encodeURIComponent(patientId)}/timeline`,
    ),

  /* encounters */
  createEncounter: (patientId: string) =>
    post<{ encounter: Encounter }>("/encounters", { patient_id: patientId }),
  getEncounter: (encounterId: string) =>
    get<EncounterDetailResponse>(
      `/encounters/${encodeURIComponent(encounterId)}`,
    ),
  /** Runs the spec §12 end-of-encounter pipeline. */
  endEncounter: (encounterId: string) =>
    post<EndEncounterResponse>(
      `/encounters/${encodeURIComponent(encounterId)}/end`,
    ),

  /* care plan */
  approveAction: (actionId: string) =>
    post<{ action: CarePlanAction }>(
      `/care-plan/actions/${encodeURIComponent(actionId)}/approve`,
    ),
  modifyAction: (
    actionId: string,
    body: { final_title: string; final_description: string; reason: string },
  ) =>
    post<{ action: CarePlanAction }>(
      `/care-plan/actions/${encodeURIComponent(actionId)}/modify`,
      body,
    ),
  rejectAction: (
    actionId: string,
    body: { category: RejectionCategory; remarks: string },
  ) =>
    post<{ action: CarePlanAction }>(
      `/care-plan/actions/${encodeURIComponent(actionId)}/reject`,
      body,
    ),
  finalizeCarePlan: (carePlanId: string) =>
    post<FinalizeResponse>(
      `/care-plan/${encodeURIComponent(carePlanId)}/finalize`,
    ),

  /* evidence (spec §11 retrieval corpus) */
  getEvidence: (ids: string[]) =>
    get<{ snippets: EvidenceSnippet[] }>(
      `/evidence?ids=${ids.map(encodeURIComponent).join(",")}`,
    ),

  /* context graph (read live from Neo4j) */
  getContextGraph: (patientId: string) =>
    get<GraphResponse>(`/context/${encodeURIComponent(patientId)}/graph`),
  /** Multi-hop provenance query, spec §18. */
  getProvenance: (patientId: string, instructionOrActionId: string) =>
    get<{ path: GraphHop[] }>(
      `/context/${encodeURIComponent(patientId)}/provenance/${encodeURIComponent(
        instructionOrActionId,
      )}`,
    ),

  /* analytics / evals / ops */
  getAnalyticsFeedback: () => get<AnalyticsFeedback>("/analytics/feedback"),
  /** Most recent encounter — AI Operations default after resets. */
  getLatestEncounter: () => get<{ encounter: Encounter }>("/encounters/latest"),
  getEvals: (encounterId: string) =>
    get<EvalsResponse>(`/evals/${encodeURIComponent(encounterId)}`),
  /** From app Postgres, NOT Langfuse (spec §21A). */
  getTraceSummary: (encounterId: string) =>
    get<{ rows: TraceSummaryRow[] }>(
      `/ops/trace-summary/${encodeURIComponent(encounterId)}`,
    ),
  getHealth: () => get<HealthResponse>("/health"),
};

/** Human-readable message for any error thrown by this client. */
export function describeError(err: unknown): string {
  if (err instanceof BackendUnreachableError) return err.message;
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}
