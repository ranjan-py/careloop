# How to read CareLoop's metrics

These are prototype regression and workflow signals, not clinical validation. Values in a screenshot belong to that encounter; they are not product-wide accuracy claims.

## Session evaluators

| Displayed evaluator | Calculation in the implementation | What it reveals / limitation |
| --- | --- | --- |
| `schema_validity` | Valid persisted facts, suggestions, actions and transcript segments / all checked objects. Pass requires no failures and at least one object. | Structural correctness, not factual correctness. |
| `modified_action_fidelity` | Fraction of modified actions with final text represented across stored reports and superseded original wording absent. Uses normalized matching or ≥50% distinctive final-title token overlap. | Whether edits propagate; it is a text heuristic, not exact semantic equivalence. No modifications yields a labeled vacuous 100%. |
| `rejected_action_leakage` | Count of rejected-title matches detected in stored reports. | Rejected actions should not reappear; zero with no reports is explicitly vacuous, not evidence of a completed check. |
| `tool_selection_validity` | Correct category-to-tool mappings / linked executions checked. | Whether the demo selected the registered tool. No linked executions is a vacuous pass; orphan executions are skipped. |
| `permission_behavior` | Binary pass/fail: no rejected action executed and no non-automatic execution without an accepted clinician decision. | Whether permission gates held for recorded executions; denial rows are reported when present. |
| `latency` | p95 next-best-action latency from available Langfuse generation spans; otherwise anchored fact-persistence → suggestion-persistence intervals. Target <6 seconds. | Responsiveness within one encounter; caption the source and sample count. Missing/unanchored measurements must not become zero. |
| `cost_per_encounter` | Reconstructed model-call/token estimates using chars/4, overhead assumptions and placeholder model rates. Target <$0.50. | Rough model-cost estimate, NOT an invoice or total system cost; excludes an authoritative measurement of hosting/transcription charges. |
| `grounding` | Model judge: fraction of care-plan actions with claims supported by facts/transcript; pass requires all supported. | Flags unsupported action claims. A judge can be wrong. |
| `unsupported_fact_detector` | Model judge checks outputs for invented medications, labs, symptoms or patient facts; clean score 1.0 and no inventions passes. | Hallucination signal. The launch table displays `(1 − score) × 100%`, not a measured population error rate. |
| `care_plan_completeness` | Model judge: addressed canonical gaps / 4. | Coverage of uncontrolled BP, medication reconciliation, stale renal labs and overdue follow-up; scenario-specific. |
| `next_best_action_quality` | Model judge: fraction of suggestions addressing a real information gap; all must do so to pass. | Relevance of follow-up questions, not proof that advice is clinically safe. |

## Additional timing details

- **First transcript:** encounter start → first persisted final transcript; includes session setup.
- **State update:** fired extraction trigger → next ingested fact within the allowed cycle window; detail reports median over fact-producing cycles.
- **Care-plan generation:** encounter end → last generated action persisted.

## Six launch-criteria rows

Schema validity 100%; unsupported-fact rate 0%; rejected-action leakage 0; modified-action fidelity 100%; p95 next-best-action latency <6 seconds; estimated cost <$0.50. The table reuses persisted evaluator pass/fail values. Missing results show unmeasured/red. All six being green does not imply every evaluator passes: grounding and other judge checks are separate from these six rows.

## Feedback analytics

- **Accepted / modified / rejected:** each category count divided by total decisions, rounded to one decimal percent. Combines seeded synthetic cohort with the latest recorded decision per action. Although code labels that component “current-session,” the query spans stored actions, not just the encounter selected on the evaluator page.
- **Rejection reasons:** counts by seven categories, combining seeded and recorded decisions. Counts are not percentages.
- **High-friction recommendations:** `(modified + rejected) / total` for each recommendation in the seeded synthetic cohort, sorted descending. This is not rejection rate alone.
- **Trend, top reason and organizations:** enrichment from bundled synthetic historical aggregates; not an independently measured live longitudinal trend.

Source: `backend/app/evals/evaluators.py`, `backend/app/evals/criteria.py`, `backend/app/analytics/router.py`.
