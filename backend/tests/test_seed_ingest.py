"""Unit tests for the FHIR ingest mapping (app.context.fhir_ingest).

Pure parsing layer — no DB, no network, nothing mocked. A small inline
bundle exercises the seed pipeline's classification rules: scripted overlay
resources vs background Synthea noise, the noshow appointment → missed
follow-up beat, and deterministic fact ids reused from the bundle."""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.context import fhir_ingest

NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)

_SOURCE_TAG = {
    "tag": [
        {
            "system": "https://careloop.demo/tags/source_class",
            "code": "synthea_ehr",
            "display": "synthea_ehr",
        }
    ]
}

BACKGROUND_ENCOUNTER_ID = "e2671843-9a01-099e-0000-000000000001"
BACKGROUND_MED_ID = "e2671843-9a01-099e-0000-000000000002"


def make_bundle() -> dict:
    """Patient + one lab + noshow appointment + active scripted med +
    background stopped med (+ text-only condition, background encounter)."""
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {
                "resource": {
                    "resourceType": "Patient",
                    "id": "john-miller",
                    "meta": _SOURCE_TAG,
                    "identifier": [{"system": "https://careloop.demo/mrn", "value": "CL-DEMO-0001"}],
                    "name": [{"use": "official", "family": "Miller", "given": ["John"]}],
                    "gender": "male",
                    "birthDate": "1968-03-30",
                }
            },
            {
                "resource": {
                    "resourceType": "Observation",
                    "id": "obs-potassium",
                    "meta": _SOURCE_TAG,
                    "status": "final",
                    "code": {
                        "coding": [
                            {
                                "system": "http://loinc.org",
                                "code": "2823-3",
                                "display": "Potassium [Moles/volume] in Serum or Plasma",
                            }
                        ],
                        "text": "Potassium [Moles/volume] in Serum or Plasma",
                    },
                    "effectiveDateTime": "2026-05-14T05:56:37Z",
                    "valueQuantity": {"value": 4.2, "unit": "mmol/L"},
                }
            },
            {
                "resource": {
                    "resourceType": "Appointment",
                    "id": "appt-missed-followup",
                    "meta": _SOURCE_TAG,
                    "status": "noshow",
                    "description": "Hypertension follow-up — patient did not attend",
                    "start": "2026-07-15T05:56:37Z",
                    "end": "2026-07-15T06:26:37Z",
                }
            },
            {
                "resource": {
                    "resourceType": "MedicationRequest",
                    "id": "med-lisinopril",
                    "meta": _SOURCE_TAG,
                    "status": "active",
                    "intent": "order",
                    "medicationCodeableConcept": {"text": "lisinopril 10 MG Oral Tablet"},
                    "authoredOn": "2026-01-26T05:56:37Z",
                    "dosageInstruction": [{"text": "10 mg orally once daily"}],
                }
            },
            {
                # Background Synthea refill history — chart noise, must be skipped.
                "resource": {
                    "resourceType": "MedicationRequest",
                    "id": BACKGROUND_MED_ID,
                    "meta": _SOURCE_TAG,
                    "status": "stopped",
                    "intent": "order",
                    "medicationCodeableConcept": {"text": "Hydrochlorothiazide 25 MG Oral Tablet"},
                    "authoredOn": "2020-07-16T04:51:51+00:00",
                }
            },
            {
                # Text-only condition (no coding) — the "CKD risk" overlay shape.
                "resource": {
                    "resourceType": "Condition",
                    "id": "cond-ckd-risk",
                    "meta": _SOURCE_TAG,
                    "clinicalStatus": {"coding": [{"code": "active"}]},
                    "code": {"text": "CKD risk (declining eGFR — synthetic demo condition)"},
                    "recordedDate": "2026-04-16T05:56:37Z",
                }
            },
            {
                # Background encounter — maps to a timeline event.
                "resource": {
                    "resourceType": "Encounter",
                    "id": BACKGROUND_ENCOUNTER_ID,
                    "meta": _SOURCE_TAG,
                    "status": "finished",
                    "type": [{"text": "General examination of patient (procedure)"}],
                    "period": {
                        "start": "2024-08-08T04:51:51+00:00",
                        "end": "2024-08-08T05:06:51+00:00",
                    },
                }
            },
        ],
    }


def parse() -> fhir_ingest.ChartIngest:
    return fhir_ingest.parse_bundle(make_bundle())


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_patient_demographics_extracted():
    chart = parse()
    assert chart.patient.id == "john-miller"
    assert chart.patient.name == "John Miller"
    assert chart.patient.sex == "male"
    assert chart.patient.birth_date == date(1968, 3, 30)
    assert chart.patient.mrn == "CL-DEMO-0001"
    assert chart.patient.source_class == "synthea_ehr"
    assert chart.patient.age_on(date(2026, 8, 14)) == 58  # spec §6.2
    assert chart.patient.age_on(date(2026, 3, 29)) == 57  # day before birthday


def test_lab_extracted_with_value_unit_and_observed_time():
    chart = parse()
    assert len(chart.labs) == 1
    lab = chart.labs[0]
    assert lab.id == "obs-potassium"
    assert lab.name == "Potassium"  # LOINC 2823-3 short name
    assert lab.value == 4.2
    assert lab.unit == "mmol/L"
    assert lab.value_display == "4.2 mmol/L"
    assert lab.observed_at == datetime(2026, 5, 14, 5, 56, 37, tzinfo=timezone.utc)


def test_text_only_condition_parsed():
    chart = parse()
    ckd = next(c for c in chart.conditions if c.id == "cond-ckd-risk")
    assert ckd.is_text_only
    assert ckd.is_scripted
    assert ckd.name == "CKD risk"  # parenthetical stripped for display
    assert ckd.clinical_status == "active"
    assert chart.scripted_conditions == [ckd]


# ---------------------------------------------------------------------------
# Background-med skipping (chart noise)
# ---------------------------------------------------------------------------


def test_background_stopped_med_is_skipped():
    chart = parse()
    assert [med.name for med in chart.medications] == ["lisinopril"]
    med = chart.medications[0]
    assert med.dose == "10 mg"
    assert med.ehr_status == "active"
    assert med.is_scripted
    skipped_ids = [med.id for med in chart.skipped_medications]
    assert skipped_ids == [BACKGROUND_MED_ID]
    assert chart.skipped.get("background_medication_requests") == 1
    # Stopped meds are noise, not "still taking" leftovers.
    assert chart.background_active_medications == []


def test_patient_row_medications_only_current():
    row = fhir_ingest.build_patient_row(parse(), now=NOW)
    assert row["medications"] == [{"name": "lisinopril", "dose": "10 mg", "ehr_status": "active"}]
    assert row["conditions"] == ["CKD risk"]
    assert row["age"] == 58


# ---------------------------------------------------------------------------
# Missed follow-up mapping (noshow appointment)
# ---------------------------------------------------------------------------


def test_noshow_appointment_maps_to_missed_followup():
    chart = parse()
    assert len(chart.missed_followups) == 1
    missed = chart.missed_followups[0]
    assert missed.id == "appt-missed-followup"
    assert missed.is_missed
    assert missed.start == datetime(2026, 7, 15, 5, 56, 37, tzinfo=timezone.utc)


def test_missed_followup_becomes_care_gap_timeline_event():
    events = fhir_ingest.build_timeline_events(parse())
    by_id = {e["id"]: e for e in events}
    missed = by_id["tl-appt-missed-followup"]
    assert missed["event_type"] == "care_gap"
    assert missed["label"] == "Missed follow-up"
    assert missed["occurred_at"] == datetime(2026, 7, 15, 5, 56, 37, tzinfo=timezone.utc)


def test_background_encounter_becomes_timeline_event():
    events = fhir_ingest.build_timeline_events(parse())
    by_id = {e["id"]: e for e in events}
    enc = by_id[f"tl-{BACKGROUND_ENCOUNTER_ID}"]
    assert enc["event_type"] == "encounter"
    assert enc["label"] == "General examination of patient"  # "(procedure)" stripped
    assert enc["occurred_at"] == datetime(2024, 8, 8, 4, 51, 51, tzinfo=timezone.utc)
    # Skipped background meds never reach the timeline.
    assert f"tl-{BACKGROUND_MED_ID}" not in by_id


# ---------------------------------------------------------------------------
# Deterministic fact ids (reused from the bundle)
# ---------------------------------------------------------------------------


def test_fact_ids_are_deterministic_and_reuse_bundle_ids():
    chart = parse()
    first = fhir_ingest.build_chart_facts(chart, ingested_at=NOW)
    second = fhir_ingest.build_chart_facts(fhir_ingest.parse_bundle(make_bundle()), ingested_at=NOW)
    assert [f["id"] for f in first] == [f["id"] for f in second]
    assert {f["id"] for f in first} == {"cond-ckd-risk", "med-lisinopril", "obs-potassium"}


def test_chart_fact_shapes():
    facts = {f["id"]: f for f in fhir_ingest.build_chart_facts(parse(), ingested_at=NOW)}
    for fact in facts.values():
        assert fact["source_type"] == "ehr"
        assert fact["source_class"] == "synthea_ehr"
        assert fact["verification_status"] == "confirmed"
        assert fact["confidence"] == 1.0
        assert fact["encounter_id"] is None
        assert fact["ingested_at"] == NOW

    lab = facts["obs-potassium"]
    assert lab["fact_type"] == "lab"
    assert lab["subject"] == "Potassium"
    assert lab["value"] == "4.2 mmol/L"  # unit rides in the value
    assert lab["reported_at"] == datetime(2026, 5, 14, 5, 56, 37, tzinfo=timezone.utc)

    med = facts["med-lisinopril"]
    assert med["fact_type"] == "medication_status"
    assert med["subject"] == "lisinopril"
    assert med["value"] == "active"

    cond = facts["cond-ckd-risk"]
    assert cond["fact_type"] == "condition"
    assert cond["subject"] == "CKD risk"
    assert cond["value"] == "active"
