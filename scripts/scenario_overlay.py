#!/usr/bin/env python3
"""CareLoop deterministic scenario overlay (spec section 6.1 / 6.2).

Takes a base Synthea FHIR R4 bundle (or --fallback to build one from scratch)
and guarantees every scripted John Miller fact with dates computed as offsets
from runtime "now":

  - BP 148/92 at today-70d, BP 151/94 at today-45d   (clinic readings ~150/95)
  - A1C 7.8 at today-100d
  - creatinine/eGFR 58 at today-120d                  (CKD risk, stale renal labs)
  - potassium 4.2 at exactly today-92d
  - missed follow-up appointment at today-30d
  - lisinopril 10 mg ACTIVE in the EHR (the encounter reveal disputes this)
  - metformin 1000 mg active

Every resource carries meta.tag source_class=synthea_ehr and a synthetic-data
tag. The scripted demo beats come ONLY from this overlay — never from raw
Synthea output (spec 6.1). All dates are recomputed on every run, so re-run
seeding (make seed) the morning of any demo.

Usage:
  python scripts/scenario_overlay.py --fallback                  # build from scratch (always works)
  python scripts/scenario_overlay.py --base data/synthea/selected_bundle.json
  python scripts/scenario_overlay.py --fallback --now 2026-08-14T09:00:00Z   # deterministic tests

Outputs:
  data/scenario_overlay/john_miller_bundle.json
  data/scenario_overlay/secondary_patients.json   (2-3 display-only stubs)

SYNTHETIC DEMO DATA ONLY — no real PHI. Not for patient care.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "data" / "scenario_overlay" / "john_miller_bundle.json"
SECONDARY_OUT = REPO_ROOT / "data" / "scenario_overlay" / "secondary_patients.json"
DEFAULT_BASE = REPO_ROOT / "data" / "synthea" / "selected_bundle.json"

TAG_SOURCE_CLASS_SYSTEM = "https://careloop.demo/tags/source_class"
TAG_SYNTHETIC_SYSTEM = "https://careloop.demo/tags/data_provenance"

# Offsets (days before runtime "today") — spec section 6.2. Load-bearing.
OFFSET_EGFR_DAYS = 120
OFFSET_A1C_DAYS = 100
OFFSET_POTASSIUM_DAYS = 92          # exactly 92 (the "92 days old" beat)
OFFSET_BP_FIRST_DAYS = 70           # 148/92
OFFSET_BP_SECOND_DAYS = 45          # 151/94
OFFSET_MISSED_FOLLOWUP_DAYS = 30
OFFSET_LISINOPRIL_AUTHORED_DAYS = 200
OFFSET_METFORMIN_AUTHORED_DAYS = 400

# LOINC codes scripted by the overlay. When a base bundle is supplied, base
# observations with these codes are dropped so scripted values are unambiguous.
SCRIPTED_LOINC = {"85354-9", "8480-6", "8462-4", "4548-4", "33914-3", "2160-0", "2823-3"}

# Resource types kept from a base bundle (spec 6.1: ignore claims/immunization noise).
KEEP_RESOURCE_TYPES = {
    "Patient", "Condition", "MedicationRequest", "Observation", "Encounter", "Appointment",
}

ISO_DATETIME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T")
ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def meta_tags() -> dict:
    return {
        "tag": [
            {"system": TAG_SOURCE_CLASS_SYSTEM, "code": "synthea_ehr", "display": "synthea_ehr"},
            {
                "system": TAG_SYNTHETIC_SYSTEM,
                "code": "synthetic",
                "display": "Synthetic demo data — Synthea-style FHIR R4, not real PHI",
            },
        ]
    }


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def quantity(value: float, unit: str, code: str) -> dict:
    return {"value": value, "unit": unit, "system": "http://unitsofmeasure.org", "code": code}


def codeable(system: str, code: str, display: str, text: str | None = None) -> dict:
    return {"coding": [{"system": system, "code": code, "display": display}], "text": text or display}


def loinc(code: str, display: str) -> dict:
    return codeable("http://loinc.org", code, display)


def snomed(code: str, display: str) -> dict:
    return codeable("http://snomed.info/sct", code, display)


def rxnorm(code: str, display: str) -> dict:
    return codeable("http://www.nlm.nih.gov/research/umls/rxnorm", code, display)


PATIENT_REF = {"reference": "Patient/john-miller", "display": "John Miller (synthetic)"}


def patient_resource(now: datetime) -> dict:
    # Age 58 (spec 6.2); birthDate is runtime-relative so the age never drifts.
    birth = (now - timedelta(days=int(58 * 365.25) + 137)).date().isoformat()
    return {
        "resourceType": "Patient",
        "id": "john-miller",
        "meta": meta_tags(),
        "identifier": [{"system": "https://careloop.demo/mrn", "value": "CL-DEMO-0001"}],
        "name": [{"use": "official", "family": "Miller", "given": ["John"]}],
        "gender": "male",
        "birthDate": birth,
    }


def condition(cid: str, code: dict, onset: datetime, note: str | None = None) -> dict:
    res = {
        "resourceType": "Condition",
        "id": cid,
        "meta": meta_tags(),
        "clinicalStatus": codeable(
            "http://terminology.hl7.org/CodeSystem/condition-clinical", "active", "Active"
        ),
        "verificationStatus": codeable(
            "http://terminology.hl7.org/CodeSystem/condition-ver-status", "confirmed", "Confirmed"
        ),
        "code": code,
        "subject": PATIENT_REF,
        "onsetDateTime": iso(onset),
        "recordedDate": iso(onset),
    }
    if note:
        res["note"] = [{"text": note}]
    return res


def med_request(mid: str, code: dict, authored: datetime, dosage_text: str) -> dict:
    return {
        "resourceType": "MedicationRequest",
        "id": mid,
        "meta": meta_tags(),
        "status": "active",  # EHR says active — the encounter reveal disputes lisinopril (spec 6.2)
        "intent": "order",
        "medicationCodeableConcept": code,
        "subject": PATIENT_REF,
        "authoredOn": iso(authored),
        "dosageInstruction": [{"text": dosage_text}],
    }


def lab_obs(oid: str, code: dict, effective: datetime, value: dict) -> dict:
    return {
        "resourceType": "Observation",
        "id": oid,
        "meta": meta_tags(),
        "status": "final",
        "category": [
            codeable(
                "http://terminology.hl7.org/CodeSystem/observation-category",
                "laboratory",
                "Laboratory",
            )
        ],
        "code": code,
        "subject": PATIENT_REF,
        "effectiveDateTime": iso(effective),
        "issued": iso(effective),
        "valueQuantity": value,
    }


def bp_obs(oid: str, effective: datetime, systolic: int, diastolic: int) -> dict:
    return {
        "resourceType": "Observation",
        "id": oid,
        "meta": meta_tags(),
        "status": "final",
        "category": [
            codeable(
                "http://terminology.hl7.org/CodeSystem/observation-category",
                "vital-signs",
                "Vital Signs",
            )
        ],
        "code": loinc("85354-9", "Blood pressure panel with all children optional"),
        "subject": PATIENT_REF,
        "effectiveDateTime": iso(effective),
        "issued": iso(effective),
        "component": [
            {
                "code": loinc("8480-6", "Systolic blood pressure"),
                "valueQuantity": quantity(systolic, "mm[Hg]", "mm[Hg]"),
            },
            {
                "code": loinc("8462-4", "Diastolic blood pressure"),
                "valueQuantity": quantity(diastolic, "mm[Hg]", "mm[Hg]"),
            },
        ],
    }


def appointment(aid: str, status: str, start: datetime, description: str) -> dict:
    return {
        "resourceType": "Appointment",
        "id": aid,
        "meta": meta_tags(),
        "status": status,  # "noshow" models the missed follow-up (spec 6.2)
        "description": description,
        "start": iso(start),
        "end": iso(start + timedelta(minutes=30)),
        "participant": [{"actor": PATIENT_REF, "status": "accepted"}],
    }


def scripted_resources(now: datetime) -> list[dict]:
    """Every spec 6.2 fact, dates as offsets from runtime now.

    Offsets keep runtime now's time-of-day so that BOTH whole-day timedelta
    math and calendar-date math yield exactly N days at seed time — the
    "potassium is 92 days old" beat must never render as 91.
    """
    day = timedelta(days=1)
    at = lambda days_ago: now - days_ago * day  # noqa: E731
    return [
        patient_resource(now),
        condition(
            "cond-hypertension",
            snomed("59621000", "Essential hypertension"),
            at(3 * 365),
        ),
        condition(
            "cond-t2d",
            snomed("44054006", "Type 2 diabetes mellitus"),
            at(4 * 365),
            note="Stable this visit — deliberately deferred in the demo narrative (spec 6.2).",
        ),
        condition(
            "cond-ckd-risk",
            {"text": "CKD risk (declining eGFR — synthetic demo condition)"},
            at(OFFSET_EGFR_DAYS),
            note="Supported by eGFR 58 at today-120d; renal labs are stale (care gap).",
        ),
        med_request(
            "med-lisinopril",
            rxnorm("314076", "lisinopril 10 MG Oral Tablet"),
            at(OFFSET_LISINOPRIL_AUTHORED_DAYS),
            "10 mg orally once daily",
        ),
        med_request(
            "med-metformin",
            rxnorm("861007", "metformin hydrochloride 1000 MG Oral Tablet"),
            at(OFFSET_METFORMIN_AUTHORED_DAYS),
            "1000 mg orally twice daily",
        ),
        lab_obs(
            "obs-egfr",
            loinc("33914-3", "Glomerular filtration rate/1.73 sq M.predicted"),
            at(OFFSET_EGFR_DAYS),
            quantity(58, "mL/min/1.73m2", "mL/min/{1.73_m2}"),
        ),
        lab_obs(
            "obs-creatinine",
            loinc("2160-0", "Creatinine [Mass/volume] in Serum or Plasma"),
            at(OFFSET_EGFR_DAYS),
            quantity(1.38, "mg/dL", "mg/dL"),
        ),
        lab_obs(
            "obs-a1c",
            loinc("4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood"),
            at(OFFSET_A1C_DAYS),
            quantity(7.8, "%", "%"),
        ),
        lab_obs(
            "obs-potassium",
            loinc("2823-3", "Potassium [Moles/volume] in Serum or Plasma"),
            at(OFFSET_POTASSIUM_DAYS),
            quantity(4.2, "mmol/L", "mmol/L"),
        ),
        bp_obs("obs-bp-70d", at(OFFSET_BP_FIRST_DAYS), 148, 92),
        bp_obs("obs-bp-45d", at(OFFSET_BP_SECOND_DAYS), 151, 94),
        appointment(
            "appt-missed-followup",
            "noshow",
            at(OFFSET_MISSED_FOLLOWUP_DAYS),
            "Hypertension follow-up — patient did not attend (missed follow-up)",
        ),
        appointment(
            "appt-today",
            "booked",
            now.replace(hour=9, minute=30, second=0, microsecond=0),
            "Follow-up visit — hypertension, medication review",
        ),
    ]


# ---------------------------------------------------------------------------
# Base-bundle handling (real Synthea path)
# ---------------------------------------------------------------------------

def _walk_strings(node, fn):
    """Recursively apply fn to every string value; fn returns replacement or None."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str):
                replacement = fn(v)
                if replacement is not None:
                    node[k] = replacement
            else:
                _walk_strings(v, fn)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, str):
                replacement = fn(v)
                if replacement is not None:
                    node[i] = replacement
            else:
                _walk_strings(v, fn)


def _collect_dates(bundle: dict) -> list[datetime]:
    found: list[datetime] = []

    def probe(s: str):
        m = ISO_DATETIME_RE.match(s) or ISO_DATE_RE.match(s)
        if m:
            try:
                found.append(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc))
            except ValueError:
                pass
        return None

    _walk_strings(bundle, probe)
    return found


def shift_bundle_dates(bundle: dict, now: datetime) -> int:
    """Shift every ISO date/datetime so the latest base date lands on today (spec 6.1)."""
    dates = _collect_dates(bundle)
    if not dates:
        return 0
    delta_days = (now.date() - max(dates).date()).days
    if delta_days == 0:
        return 0
    delta = timedelta(days=delta_days)

    def shift(s: str):
        m = ISO_DATE_RE.match(s)
        if m:
            try:
                d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
            return (d + delta).date().isoformat()
        m = ISO_DATETIME_RE.match(s)
        if m:
            try:
                d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
            return (d + delta).date().isoformat() + s[10:]
        return None

    _walk_strings(bundle, shift)
    return delta_days


def _obs_loinc_codes(resource: dict) -> set[str]:
    codes = set()
    for c in resource.get("code", {}).get("coding", []):
        codes.add(c.get("code"))
    for comp in resource.get("component", []):
        for c in comp.get("code", {}).get("coding", []):
            codes.add(c.get("code"))
    return codes


def _is_scripted_med(resource: dict) -> bool:
    text = json.dumps(resource.get("medicationCodeableConcept", {})).lower()
    return "lisinopril" in text or "metformin" in text


def filter_base_entries(bundle: dict) -> list[dict]:
    """Keep only useful background history from a Synthea bundle."""
    kept = []
    for entry in bundle.get("entry", []):
        res = entry.get("resource", {})
        rtype = res.get("resourceType")
        if rtype not in KEEP_RESOURCE_TYPES:
            continue
        if rtype == "Patient":
            continue  # replaced by the scripted John Miller patient
        if rtype == "Observation" and _obs_loinc_codes(res) & SCRIPTED_LOINC:
            continue  # scripted values must be unambiguous
        if rtype == "MedicationRequest" and _is_scripted_med(res):
            continue  # replaced by scripted lisinopril/metformin
        res.setdefault("meta", {}).setdefault("tag", []).extend(meta_tags()["tag"])
        # Repoint background history to the scripted patient id.
        if "subject" in res:
            res["subject"] = dict(PATIENT_REF)
        kept.append(entry)
    return kept


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build_bundle(now: datetime, base_path: Path | None) -> tuple[dict, str]:
    entries: list[dict] = []
    provenance = "fallback_overlay_only"
    if base_path is not None:
        base = json.loads(base_path.read_text())
        base = copy.deepcopy(base)
        shift_bundle_dates(base, now)
        entries.extend(filter_base_entries(base))
        provenance = f"synthea_base+overlay ({base_path.name})"

    for res in scripted_resources(now):
        entries.append(
            {
                "fullUrl": f"https://careloop.demo/fhir/{res['resourceType']}/{res['id']}",
                "resource": res,
            }
        )

    bundle = {
        "resourceType": "Bundle",
        "id": "john-miller-scenario",
        "meta": {
            "tag": meta_tags()["tag"]
            + [
                {
                    "system": TAG_SYNTHETIC_SYSTEM,
                    "code": "scenario_overlay",
                    "display": f"Deterministic scenario overlay — provenance: {provenance}",
                }
            ]
        },
        "type": "collection",
        "timestamp": iso(now),
        "entry": entries,
    }
    return bundle, provenance


def secondary_patients(now: datetime) -> dict:
    """2-3 display-only synthetic patient stubs for the Today's Patients list (spec 7 / 30)."""
    today = now.date().isoformat()
    return {
        "label": "Synthetic demo patients — display-only stubs, Synthea-style. Not real PHI.",
        "generated_at": iso(now),
        "patients": [
            {
                "id": "pt-rosa-delgado",
                "name": "Rosa Delgado",
                "age": 62,
                "sex": "female",
                "conditions": ["Type 2 diabetes mellitus", "Hyperlipidemia"],
                "appointment_time": f"{today}T10:30:00Z",
                "attention_summary": "Routine visit — no open items",
                "display_only": True,
            },
            {
                "id": "pt-samuel-okafor",
                "name": "Samuel Okafor",
                "age": 57,
                "sex": "male",
                "conditions": ["Essential hypertension"],
                "appointment_time": f"{today}T11:15:00Z",
                "attention_summary": "1 item needs attention",
                "display_only": True,
            },
            {
                "id": "pt-linda-tran",
                "name": "Linda Tran",
                "age": 64,
                "sex": "female",
                "conditions": ["Osteoarthritis", "Essential hypertension"],
                "appointment_time": f"{today}T13:45:00Z",
                "attention_summary": "Routine visit — no open items",
                "display_only": True,
            },
        ],
    }


def summarize(now: datetime) -> str:
    day = timedelta(days=1)
    lines = [
        "Scripted facts (runtime-relative):",
        f"  eGFR 58 / creatinine 1.38      {(now - OFFSET_EGFR_DAYS * day).date()}  (today-{OFFSET_EGFR_DAYS}d)",
        f"  A1C 7.8                        {(now - OFFSET_A1C_DAYS * day).date()}  (today-{OFFSET_A1C_DAYS}d)",
        f"  Potassium 4.2                  {(now - OFFSET_POTASSIUM_DAYS * day).date()}  (today-{OFFSET_POTASSIUM_DAYS}d, exact)",
        f"  BP 148/92                      {(now - OFFSET_BP_FIRST_DAYS * day).date()}  (today-{OFFSET_BP_FIRST_DAYS}d)",
        f"  BP 151/94                      {(now - OFFSET_BP_SECOND_DAYS * day).date()}  (today-{OFFSET_BP_SECOND_DAYS}d)",
        f"  Missed follow-up (noshow)      {(now - OFFSET_MISSED_FOLLOWUP_DAYS * day).date()}  (today-{OFFSET_MISSED_FOLLOWUP_DAYS}d)",
        "  Lisinopril 10 mg               ACTIVE in EHR (encounter reveal disputes this)",
        "  Metformin 1000 mg              active",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", type=Path, default=None, help="Base Synthea FHIR R4 bundle JSON")
    ap.add_argument("--fallback", action="store_true", help="Build the bundle from scratch (no Synthea)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--now", type=str, default=None, help="Override runtime now (ISO 8601, tests only)")
    args = ap.parse_args(argv)

    if args.now:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00")).astimezone(timezone.utc)
    else:
        now = datetime.now(timezone.utc)

    base_path: Path | None = None
    if not args.fallback:
        candidate = args.base or DEFAULT_BASE
        if candidate.exists():
            base_path = candidate
        elif args.base is not None:
            print(f"ERROR: base bundle not found: {candidate}", file=sys.stderr)
            print("Hint: run scripts/generate_synthea.sh + scripts/select_patient.py, or use --fallback.", file=sys.stderr)
            return 2
        else:
            print(f"NOTE: no Synthea base bundle at {candidate}; using --fallback path (allowed by spec 6.1).")

    bundle, provenance = build_bundle(now, base_path)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(bundle, indent=2) + "\n")
    SECONDARY_OUT.parent.mkdir(parents=True, exist_ok=True)
    SECONDARY_OUT.write_text(json.dumps(secondary_patients(now), indent=2) + "\n")

    print(f"Wrote {args.out}  ({len(bundle['entry'])} entries, provenance: {provenance})")
    print(f"Wrote {SECONDARY_OUT}  (display-only stubs)")
    print(summarize(now))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
