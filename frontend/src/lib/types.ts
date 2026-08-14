/**
 * CareLoop frontend types.
 *
 * The "Core models" section below mirrors contracts/CONTRACTS.md EXACTLY and is
 * the frozen half of this file (backend twin: backend/app/schemas/core.py).
 *
 * The "REST-referenced shapes" section further down covers names the contract
 * references. These are now PINNED by backend/app/schemas/core.py — any change
 * requires a matching backend update. A few shapes the backend does not model
 * yet remain flagged with `NOT PINNED` comments — reconcile with the backend
 * before changing those.
 */

/* ------------------------------------------------------------------ */
/* Core models — contracts/CONTRACTS.md (frozen)                       */
/* ------------------------------------------------------------------ */

export type FactType =
  | "medication_status"
  | "observation"
  | "symptom"
  | "lab"
  | "condition"
  | "care_gap";

export type FactSourceType = "ehr" | "patient_report" | "clinician";

/** Structurally distinct provenance (spec §6.1). */
export type SourceClass = "synthea_ehr" | "patient_report";

export type VerificationStatus = "unverified" | "confirmed" | "disputed";

export interface Fact {
  id: string;
  fact_type: FactType;
  subject: string;
  value: string;
  source_type: FactSourceType;
  source_class: SourceClass;
  /** e.g. "pharmacy_kiosk" */
  method?: string;
  /** ISO timestamp; display ages are derived at render time (spec §7). */
  reported_at: string;
  /** ISO timestamp. */
  ingested_at: string;
  confidence: number;
  verification_status: VerificationStatus;
  encounter_id?: string;
  /** id of the conflicting Fact. */
  conflicts_with?: string;
}

export type SuggestionKind = "question" | "info_gap" | "action";
export type SuggestionStatus = "active" | "dismissed" | "superseded";

export interface Suggestion {
  id: string;
  encounter_id: string;
  kind: SuggestionKind;
  text: string;
  rationale: string;
  created_at: string;
  status: SuggestionStatus;
}

export type ActionCategory =
  | "lab"
  | "medication"
  | "monitoring"
  | "follow_up"
  | "referral"
  | "other";

export type RiskLevel = "low" | "medium" | "high";

export type PermissionTier =
  | "auto_demo"
  | "clinician_review"
  | "required_clinician_decision";

export type ActionStatus =
  | "pending"
  | "approved"
  | "modified"
  | "rejected"
  | "executed"
  | "denied";

export interface ActionDecision {
  clinician_id: string;
  decided_at: string;
  model_version?: string;
  prompt_version?: string;
  /** modify */
  final_title?: string;
  final_description?: string;
  reason?: string;
  /** reject */
  category?: RejectionCategory;
  remarks?: string;
}

/** Care-plan item — UI vocabulary: "Action" (spec §25). */
export interface CarePlanAction {
  id: string;
  care_plan_id: string;
  category: ActionCategory;
  title: string;
  description: string;
  rationale: string;
  patient_facts_used: string[];
  /** Retrieval-produced evidence ids (spec §11). */
  evidence_refs: string[];
  risk_level: RiskLevel;
  permission: PermissionTier;
  status: ActionStatus;
  decision?: ActionDecision;
}

/**
 * SINGLE shared rejection enum (spec §14) — exact values everywhere,
 * including analytics. Display labels below are the only permitted labels.
 */
export type RejectionCategory =
  | "missing_patient_information"
  | "clinical_disagreement"
  | "patient_limitation"
  | "operational_workflow_limitation"
  | "organization_protocol_constraint"
  | "requires_supervision_escalation"
  | "other";

export const REJECTION_CATEGORIES: RejectionCategory[] = [
  "missing_patient_information",
  "clinical_disagreement",
  "patient_limitation",
  "operational_workflow_limitation",
  "organization_protocol_constraint",
  "requires_supervision_escalation",
  "other",
];

export const REJECTION_CATEGORY_LABELS: Record<RejectionCategory, string> = {
  missing_patient_information: "Missing patient information",
  clinical_disagreement: "Clinical disagreement",
  patient_limitation: "Patient limitation",
  operational_workflow_limitation: "Operational/workflow limitation",
  organization_protocol_constraint: "Organization/protocol constraint",
  requires_supervision_escalation: "Requires supervision/escalation",
  other: "Other",
};

export interface EvalResult {
  evaluator: string;
  kind: "deterministic" | "model";
  score: number;
  passed: boolean;
  detail: string;
  encounter_id: string;
  prompt_version?: string;
}

/** Launch-criteria table row (spec §22.1). */
export interface CriterionRow {
  name: string;
  target: string;
  actual: string;
  passed: boolean;
}

export type TraceStepStatus = "PASS" | "FAIL" | "DEGRADED" | "BLOCKED" | "DENIED";

export interface TraceSummaryRow {
  step: string;
  status: TraceStepStatus;
  detail: string;
  latency_ms?: number;
}

/* ------------------------------------------------------------------ */
/* REST-referenced shapes — named by the contract; pinned by            */
/* backend/app/schemas/core.py unless flagged NOT PINNED below.         */
/* ------------------------------------------------------------------ */

export interface Clinician {
  id: string;
  name: string;
  email: string;
  role?: string;
}

/** NOT PINNED — row for /patients (spec §7 screen 2). */
export interface PatientListItem {
  id: string;
  name: string;
  age: number;
  sex?: string;
  conditions: string[];
  /** ISO timestamp of today's appointment. */
  appointment_time?: string;
  /** "N items need attention". */
  attention_count?: number;
  /** Non-interactive synthetic filler rows (spec §7: display-only). */
  display_only?: boolean;
}

/** NOT PINNED. */
export interface PatientPriority {
  id: string;
  label: string;
  detail?: string;
  severity?: RiskLevel;
}

/** NOT PINNED. */
export interface CareGap {
  id: string;
  label: string;
  detail?: string;
}

/** Pinned by backend/app/schemas/core.py. */
export interface MedicationSummary {
  name: string;
  dose?: string;
  /** e.g. "active" per EHR. */
  ehr_status: string;
}

/** Pinned by backend/app/schemas/core.py. */
export interface LabSummary {
  name: string;
  value: string;
  unit?: string;
  /** ISO timestamp — age rendered at display time. */
  observed_at: string;
}

/** /patients/{id} incl. priorities[] and care_gaps[] — structured shapes the
 * backend is adopting (priorities/care_gaps stay object arrays). */
export interface PatientDetail extends PatientListItem {
  priorities: PatientPriority[];
  care_gaps: CareGap[];
  medications: MedicationSummary[];
  labs: LabSummary[];
}

/** /patients/{id}/timeline — pinned by backend/app/schemas/core.py. */
export interface TimelineEvent {
  id: string;
  event_type: string;
  label: string;
  /** ISO timestamp (seeded relative to runtime "today", spec §6.1);
   * display ages derived at render time. */
  occurred_at: string;
  detail?: string;
}

export type SpeakerRole = "doctor" | "patient";

/** Transcript segment as carried in WS envelopes and GET /encounters/{id}. */
export interface TranscriptSegment {
  id: string;
  speaker: SpeakerRole;
  text: string;
  /** Float seconds from session start. */
  ts: number;
}

/** Encounter row. `stream_ticket` is the one-time WS ticket the contract says
 * comes "from POST /api/encounters". */
export interface Encounter {
  id: string;
  patient_id: string;
  status: string;
  started_at?: string;
  ended_at?: string;
  stream_ticket?: string;
}

/** NOT PINNED. */
export interface CarePlan {
  id: string;
  encounter_id: string;
  status?: string;
  created_at?: string;
  actions: CarePlanAction[];
}

/** Tool-execution record from finalize (spec §15) — matches the backend
 * finalize response exactly. */
export interface ExecutionRecord {
  id: string;
  action_id: string;
  tool_name: string;
  status: string;
  permission_tier: string;
  result?: unknown;
  error?: string;
}

/** Synthetic guideline/evidence snippet (spec §11 retrieval corpus) — pinned
 * by backend/app/schemas/core.py. */
export interface EvidenceSnippet {
  id: string;
  title: string;
  body: string;
  topic_tags: string[];
}

/** NOT PINNED — Neo4j projection read via /context/{patient_id}/graph. */
export interface GraphNode {
  id: string;
  labels: string[];
  properties: Record<string, unknown>;
}

/** NOT PINNED. */
export interface GraphRelationship {
  id: string;
  type: string;
  start_id: string;
  end_id: string;
  properties?: Record<string, unknown>;
}

/** NOT PINNED — one hop of a multi-hop provenance path (spec §18). */
export interface GraphHop {
  from: GraphNode;
  relationship: GraphRelationship;
  to: GraphNode;
}

/** NOT PINNED — decision mix percentages (spec §23). */
export interface DecisionMix {
  accepted: number;
  modified: number;
  rejected: number;
}

/** NOT PINNED — rejection-reason count; labels MUST match
 * REJECTION_CATEGORY_LABELS exactly (single shared enum, spec §14/§23). */
export interface ReasonCount {
  category: RejectionCategory;
  label: string;
  count: number;
}

/** NOT PINNED — high-friction recommendation row (spec §23). */
export interface HighFrictionItem {
  title: string;
  modify_reject_pct: number;
  trend?: string;
  top_reason?: string;
  organizations?: number;
}

/** GET /api/analytics/feedback response (field names per CONTRACTS.md;
 * inner shapes NOT PINNED). */
export interface AnalyticsFeedback {
  decision_mix: DecisionMix;
  reasons: ReasonCount[];
  high_friction: HighFrictionItem[];
  /** e.g. "Synthetic demo cohort" */
  cohort_label: string;
}

export type DependencyName =
  | "postgres"
  | "neo4j"
  | "langfuse"
  | "openai"
  | "deepgram";

export interface HealthResponse {
  status: string;
  deps: Record<DependencyName, { status: string; detail?: string }>;
}
