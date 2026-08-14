# CareLoop — Product Metrics

> **Metrics this product is designed to validate — not claims from this prototype.**

## The causal chain this demo instruments

Information latency → decision latency → execution latency compound into **days-to-control** —
the metric risk-bearing organizations monetize (quality/Stars performance, risk contracts,
utilization avoided, clinician capacity without added headcount).

| Metric | Definition | Where measured in demo |
|---|---|---|
| Information latency | patient says/reports something → structured state update | trace: transcript.final → state.fact |
| Decision latency | relevant state exists → prioritized guidance shown | trace: state.fact → suggestion.active |
| Encounter → finalized plan | End Visit → clinician-approved plan | encounter + decision timestamps |
| Plan → action initiation | approval → workflow tool start | decision → tool_execution timestamps |
| Acceptance / modification / rejection mix | human feedback quality | feedback analytics |
| Cost per encounter / per care plan | token + $ from traces | Langfuse cost data |
| Care-gap closure | future real-world metric | not measurable in prototype |
| Time to control | future north-star metric | not measurable in prototype |

## Why the feedback loop is the product

Every clinician override is categorized and stored. The aggregate ("Home BP twice daily —
38% modify/reject, top reason: no home monitor") is operational reality the system learns from —
the difference between decision support that gets ignored and an execution platform that adapts.
No automatic model change is made from feedback in this demo, deliberately.
