# Offline regression eval cases (spec section 22.2)

**Synthetic demo data — hand-authored, hand-labeled regression cases. This is a
REGRESSION HARNESS, never clinical validation.**

Ten cases, `case_01.json` … `case_10.json`. Shared schema:

```json
{
  "id": "case_01",
  "name": "short human name",
  "synthetic": true,
  "label": "Synthetic regression eval case — hand-labeled. Not clinical validation.",
  "description": "what this case exercises",
  "patient_state": {
    "demographics": {"age": 58, "sex": "male"},
    "conditions": ["..."],
    "medications": [{"name": "...", "dose": "...", "ehr_status": "active"}],
    "labs": [{"name": "...", "value": 4.2, "unit": "mmol/L", "age_days": 92}],
    "vitals": [{"name": "blood_pressure", "value": "148/92", "age_days": 70, "method": "clinic"}],
    "care_gaps": ["..."]
  },
  "transcript": [{"speaker": "doctor", "text": "..."}],
  "expected_key_facts": [
    {"fact_type": "medication_status", "subject": "lisinopril", "value_contains": "stopped", "source_type": "patient_report"}
  ],
  "expected_information_gaps": ["..."],
  "expected_care_plan_categories": ["medication", "lab", "monitoring", "follow_up"],
  "forbidden_unsupported_actions": ["..."],
  "expected_escalation_or_wait": {
    "escalation": "none | same_day | urgent",
    "wait_for_information": true,
    "detail": "..."
  },
  "hand_label_notes": "why the labels are what they are"
}
```

Conventions:
- `expected_care_plan_categories` uses the CONTRACTS.md CarePlanAction categories
  exactly: `lab | medication | monitoring | follow_up | referral | other`.
- `fact_type` / `source_type` mirror the CONTRACTS.md `Fact` model.
- `forbidden_unsupported_actions` are actions the plan must NOT contain — either
  unsupported by the case facts or unsafe given them.
- `case_01` mirrors the John Miller demo scenario exactly (pharmacy-kiosk BP,
  no home monitor). `case_02` expects wait-for-labs. `case_03` is a
  conflicting-medications trap.

The eval runner (backend) uses these with the real OpenAI API via
`OPENAI_EVAL_MODEL` for model-graded checks, and reports judge-vs-human
agreement against `hand_label_notes` / the expected_* fields.
