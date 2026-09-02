"""FHIR R4 bundle → structured chart intermediate (spec §6.1).

Pure parsing layer: no DB, no network, no settings. The seed pipeline
(app.context.seed) feeds the overlay bundle through parse_bundle() and turns
the result into authoritative Postgres rows via the build_* helpers, which
mint DETERMINISTIC ids by reusing the bundle resource ids.

Classification rules (spec §6.1/§6.2):
- Scripted overlay resources carry ids prefixed obs-/med-/cond-/appt-;
  everything else is background Synthea chart history.
- source_class rides on meta.tag (system https://careloop.demo/tags/source_class)
  and defaults to "synthea_ehr".
- Background MedicationRequests (stopped/completed refill history) are chart
  noise: parsed, counted, and skipped — the current medication state is pinned
  by the overlay (lisinopril + metformin, exactly one MedicationRequest each).
  Background resources with status "active" are still skipped but surfaced via
  background_active_medications so the seed can flag them honestly.
- Background Encounters are kept — they become timeline events.
- An Appointment with status "noshow" maps to the "missed follow-up" beat.

All data is synthetic ("Synthetic patient — Synthea-generated FHIR R4").
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

SOURCE_CLASS_TAG_SYSTEM = "https://careloop.demo/tags/source_class"
DEFAULT_SOURCE_CLASS = "synthea_ehr"
SCRIPTED_ID_PREFIXES = ("obs-", "med-", "cond-", "appt-")

TIMELINE_ID_PREFIX = "tl-"

# LOINC → short display names for the chart labs (fallback: the code text).
LOINC_SHORT_NAMES = {
    "2823-3": "Potassium",
    "33914-3": "eGFR",
    "2160-0": "Creatinine",
    "4548-4": "A1C",
}
LOINC_SYSTOLIC = "8480-6"
LOINC_DIASTOLIC = "8462-4"

_PAREN_SUFFIX = re.compile(r"\s*\([^()]*\)\s*$")
_DOSE_MG = re.compile(r"(\d+(?:\.\d+)?)\s*MG\b", re.IGNORECASE)


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _source_class(resource: dict) -> str:
    for tag in (resource.get("meta") or {}).get("tag", []):
        if tag.get("system") == SOURCE_CLASS_TAG_SYSTEM and tag.get("code"):
            return tag["code"]
    return DEFAULT_SOURCE_CLASS


def _is_scripted(resource_id: str) -> bool:
    return resource_id.startswith(SCRIPTED_ID_PREFIXES)


def _clean_display(text: str) -> str:
    """Strip trailing parentheticals — 'Prediabetes (finding)' → 'Prediabetes'."""
    return _PAREN_SUFFIX.sub("", text).strip()


def _code_text(resource: dict, key: str = "code") -> str:
    code = resource.get(key) or {}
    if code.get("text"):
        return code["text"]
    for coding in code.get("coding", []):
        if coding.get("display"):
            return coding["display"]
    return ""


def _loinc(resource: dict) -> str | None:
    for coding in (resource.get("code") or {}).get("coding", []):
        if coding.get("system") == "http://loinc.org":
            return coding.get("code")
    return None


def _format_quantity(value: float, unit: str | None) -> str:
    if unit is None:
        return f"{value:g}"
    if unit == "%":
        return f"{value:g}%"
    return f"{value:g} {unit}"


# ---------------------------------------------------------------------------
# Parsed intermediate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatientDemographics:
    id: str
    name: str
    sex: str
    birth_date: date | None
    mrn: str | None
    source_class: str

    def age_on(self, on: date) -> int:
        if self.birth_date is None:
            return 0
        b = self.birth_date
        return on.year - b.year - ((on.month, on.day) < (b.month, b.day))


@dataclass(frozen=True)
class ParsedCondition:
    id: str
    name: str
    clinical_status: str
    onset: datetime | None
    recorded: datetime | None
    source_class: str
    is_scripted: bool
    is_text_only: bool  # code has no coding — e.g. the "CKD risk" overlay condition
    note: str | None = None


@dataclass(frozen=True)
class ParsedMedication:
    id: str
    name: str
    dose: str | None
    full_text: str
    ehr_status: str
    authored_on: datetime | None
    source_class: str
    is_scripted: bool


@dataclass(frozen=True)
class ParsedLab:
    id: str
    name: str
    code_text: str
    value: float
    unit: str | None
    observed_at: datetime
    loinc: str | None

    @property
    def value_display(self) -> str:
        return _format_quantity(self.value, self.unit)


@dataclass(frozen=True)
class ParsedBloodPressure:
    id: str
    systolic: float
    diastolic: float
    observed_at: datetime

    @property
    def value_display(self) -> str:
        return f"{self.systolic:g}/{self.diastolic:g} mmHg"

    @property
    def label(self) -> str:
        return f"BP {self.systolic:g}/{self.diastolic:g}"


@dataclass(frozen=True)
class ParsedEncounter:
    id: str
    label: str
    started_at: datetime
    ended_at: datetime | None
    source_class: str


@dataclass(frozen=True)
class ParsedAppointment:
    id: str
    status: str  # noshow | booked | ...
    description: str
    start: datetime
    end: datetime | None

    @property
    def is_missed(self) -> bool:
        return self.status == "noshow"


@dataclass
class ChartIngest:
    """Structured intermediate for one patient bundle."""

    patient: PatientDemographics
    conditions: list[ParsedCondition] = field(default_factory=list)  # all parsed
    medications: list[ParsedMedication] = field(default_factory=list)  # current, scripted+active
    skipped_medications: list[ParsedMedication] = field(default_factory=list)  # chart noise
    background_active_medications: list[ParsedMedication] = field(default_factory=list)
    labs: list[ParsedLab] = field(default_factory=list)
    bp_readings: list[ParsedBloodPressure] = field(default_factory=list)
    encounters: list[ParsedEncounter] = field(default_factory=list)  # background history
    missed_followups: list[ParsedAppointment] = field(default_factory=list)  # status noshow
    upcoming_appointments: list[ParsedAppointment] = field(default_factory=list)  # status booked
    skipped: dict[str, int] = field(default_factory=dict)

    @property
    def scripted_conditions(self) -> list[ParsedCondition]:
        return [c for c in self.conditions if c.is_scripted]


# ---------------------------------------------------------------------------
# Bundle parsing
# ---------------------------------------------------------------------------


def load_bundle(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _parse_patient(resource: dict) -> PatientDemographics:
    names = resource.get("name") or [{}]
    official = next((n for n in names if n.get("use") == "official"), names[0])
    given = " ".join(official.get("given", []))
    family = official.get("family", "")
    mrn = None
    for identifier in resource.get("identifier", []):
        if "mrn" in (identifier.get("system") or ""):
            mrn = identifier.get("value")
    birth = resource.get("birthDate")
    return PatientDemographics(
        id=resource["id"],
        name=" ".join(part for part in (given, family) if part) or "Unknown",
        sex=resource.get("gender", "unknown"),
        birth_date=date.fromisoformat(birth) if birth else None,
        mrn=mrn,
        source_class=_source_class(resource),
    )


def _parse_condition(resource: dict) -> ParsedCondition:
    code = resource.get("code") or {}
    status_codings = (resource.get("clinicalStatus") or {}).get("coding", [{}])
    notes = resource.get("note") or []
    return ParsedCondition(
        id=resource["id"],
        name=_clean_display(_code_text(resource)),
        clinical_status=status_codings[0].get("code", "active") if status_codings else "active",
        onset=_parse_dt(resource.get("onsetDateTime")),
        recorded=_parse_dt(resource.get("recordedDate")),
        source_class=_source_class(resource),
        is_scripted=_is_scripted(resource["id"]),
        is_text_only=not code.get("coding"),
        note=notes[0].get("text") if notes else None,
    )


def _parse_medication(resource: dict) -> ParsedMedication:
    text = _code_text(resource, "medicationCodeableConcept")
    dose_match = _DOSE_MG.search(text)
    dose = f"{dose_match.group(1)} mg" if dose_match else None
    name = text.split()[0].lower() if text else resource["id"]
    return ParsedMedication(
        id=resource["id"],
        name=name,
        dose=dose,
        full_text=text,
        ehr_status=resource.get("status", "unknown"),
        authored_on=_parse_dt(resource.get("authoredOn")),
        source_class=_source_class(resource),
        is_scripted=_is_scripted(resource["id"]),
    )


def _parse_observation(resource: dict) -> ParsedLab | ParsedBloodPressure | None:
    observed_at = _parse_dt(resource.get("effectiveDateTime")) or _parse_dt(resource.get("issued"))
    if observed_at is None:
        return None
    components = resource.get("component") or []
    systolic = diastolic = None
    for comp in components:
        codes = {c.get("code") for c in (comp.get("code") or {}).get("coding", [])}
        value = (comp.get("valueQuantity") or {}).get("value")
        if value is None:
            continue
        if LOINC_SYSTOLIC in codes:
            systolic = value
        elif LOINC_DIASTOLIC in codes:
            diastolic = value
    if systolic is not None and diastolic is not None:
        return ParsedBloodPressure(
            id=resource["id"], systolic=systolic, diastolic=diastolic, observed_at=observed_at
        )
    quantity = resource.get("valueQuantity") or {}
    if quantity.get("value") is None:
        return None
    loinc = _loinc(resource)
    code_text = _code_text(resource)
    return ParsedLab(
        id=resource["id"],
        name=LOINC_SHORT_NAMES.get(loinc or "", "") or _clean_display(code_text),
        code_text=code_text,
        value=quantity["value"],
        unit=quantity.get("unit"),
        observed_at=observed_at,
        loinc=loinc,
    )


def _parse_encounter(resource: dict) -> ParsedEncounter | None:
    period = resource.get("period") or {}
    started_at = _parse_dt(period.get("start"))
    if started_at is None:
        return None
    types = resource.get("type") or [{}]
    label = _clean_display(types[0].get("text") or "Encounter")
    return ParsedEncounter(
        id=resource["id"],
        label=label,
        started_at=started_at,
        ended_at=_parse_dt(period.get("end")),
        source_class=_source_class(resource),
    )


def _parse_appointment(resource: dict) -> ParsedAppointment | None:
    start = _parse_dt(resource.get("start"))
    if start is None:
        return None
    return ParsedAppointment(
        id=resource["id"],
        status=resource.get("status", "unknown"),
        description=resource.get("description", ""),
        start=start,
        end=_parse_dt(resource.get("end")),
    )


def parse_bundle(bundle: dict) -> ChartIngest:
    """Parse a FHIR R4 bundle into the structured chart intermediate."""
    if bundle.get("resourceType") != "Bundle":
        raise ValueError("Not a FHIR Bundle")

    patient: PatientDemographics | None = None
    chart = ChartIngest(patient=PatientDemographics("", "", "", None, None, DEFAULT_SOURCE_CLASS))
    skipped: dict[str, int] = {}

    def skip(kind: str) -> None:
        skipped[kind] = skipped.get(kind, 0) + 1

    for entry in bundle.get("entry", []):
        resource = entry.get("resource") or {}
        rtype = resource.get("resourceType")
        if not rtype or "id" not in resource:
            skip("malformed")
            continue
        if rtype == "Patient":
            patient = _parse_patient(resource)
        elif rtype == "Condition":
            chart.conditions.append(_parse_condition(resource))
        elif rtype == "MedicationRequest":
            med = _parse_medication(resource)
            if med.is_scripted and med.ehr_status == "active":
                chart.medications.append(med)
            else:
                # Background refill history (stopped/completed) is chart noise.
                chart.skipped_medications.append(med)
                skip("background_medication_requests")
                if med.ehr_status == "active":
                    # Synthea leftover contradicting the scripted med state —
                    # surfaced so the seed can flag it (never silently merged).
                    chart.background_active_medications.append(med)
        elif rtype == "Observation":
            parsed = _parse_observation(resource)
            if isinstance(parsed, ParsedBloodPressure):
                chart.bp_readings.append(parsed)
            elif isinstance(parsed, ParsedLab):
                chart.labs.append(parsed)
            else:
                skip("unparsed_observations")
        elif rtype == "Encounter":
            enc = _parse_encounter(resource)
            if enc is not None:
                chart.encounters.append(enc)
            else:
                skip("unparsed_encounters")
        elif rtype == "Appointment":
            appt = _parse_appointment(resource)
            if appt is None:
                skip("unparsed_appointments")
            elif appt.is_missed:
                chart.missed_followups.append(appt)
            elif appt.status == "booked":
                chart.upcoming_appointments.append(appt)
            else:
                skip("other_appointments")
        else:
            skip(f"ignored_{rtype.lower()}")  # claims/immunization noise (spec §6.1)

    if patient is None:
        raise ValueError("Bundle contains no Patient resource")
    chart.patient = patient
    chart.skipped = skipped
    return chart


# ---------------------------------------------------------------------------
# Row builders — pure, deterministic (ids reuse the bundle resource ids)
# ---------------------------------------------------------------------------


def build_patient_row(chart: ChartIngest, *, now: datetime) -> dict:
    """Base Patient-row fields (priorities/care_gaps are scenario copy owned by seed)."""
    upcoming = sorted(chart.upcoming_appointments, key=lambda a: a.start)
    return {
        "id": chart.patient.id,
        "name": chart.patient.name,
        "age": chart.patient.age_on(now.date()),
        "sex": chart.patient.sex,
        "mrn": chart.patient.mrn,
        "birth_date": chart.patient.birth_date,
        "conditions": [c.name for c in chart.scripted_conditions],
        "medications": [
            {"name": med.name, "dose": med.dose, "ehr_status": med.ehr_status}
            for med in chart.medications
        ],
        "appointment_time": upcoming[0].start if upcoming else None,
        "source_class": chart.patient.source_class,
    }


def build_chart_facts(chart: ChartIngest, *, ingested_at: datetime) -> list[dict]:
    """Chart facts (source_type ehr, confirmed, confidence 1.0) with ids reused
    verbatim from the bundle — re-running the seed rewrites the same rows."""
    patient_id = chart.patient.id
    common = {
        "patient_id": patient_id,
        "source_type": "ehr",
        "method": None,
        "ingested_at": ingested_at,
        "confidence": 1.0,
        "verification_status": "confirmed",
        "encounter_id": None,
        "conflicts_with": None,
    }
    facts: list[dict] = []
    for cond in chart.scripted_conditions:
        facts.append(
            {
                **common,
                "id": cond.id,
                "fact_type": "condition",
                "subject": cond.name,
                "value": cond.clinical_status,
                "source_class": cond.source_class,
                "reported_at": cond.recorded or cond.onset or ingested_at,
            }
        )
    for med in chart.medications:
        facts.append(
            {
                **common,
                "id": med.id,
                "fact_type": "medication_status",
                "subject": med.name,
                "value": med.ehr_status,
                "source_class": med.source_class,
                "reported_at": med.authored_on or ingested_at,
            }
        )
    for lab in chart.labs:
        facts.append(
            {
                **common,
                "id": lab.id,
                "fact_type": "lab",
                "subject": lab.name,
                "value": lab.value_display,  # unit carried in the value (contract LabSummary)
                "source_class": chart.patient.source_class,
                "reported_at": lab.observed_at,
            }
        )
    for bp in chart.bp_readings:
        facts.append(
            {
                **common,
                "id": bp.id,
                "fact_type": "observation",
                "subject": "Blood pressure",
                "value": bp.value_display,
                "source_class": chart.patient.source_class,
                "reported_at": bp.observed_at,
            }
        )
    return facts


def build_timeline_events(chart: ChartIngest) -> list[dict]:
    """TimelineEvent rows: labs, BPs, missed follow-up, background encounters,
    and the booked visit. occurred_at comes straight from the bundle (already
    runtime-relative — spec §6.1). Ids: 'tl-' + bundle resource id."""
    patient_id = chart.patient.id
    events: list[dict] = []
    for lab in chart.labs:
        events.append(
            {
                "id": f"{TIMELINE_ID_PREFIX}{lab.id}",
                "patient_id": patient_id,
                "event_type": "lab",
                "label": f"{lab.name} {lab.value_display}",
                "occurred_at": lab.observed_at,
                "detail": lab.code_text or None,
            }
        )
    for bp in chart.bp_readings:
        events.append(
            {
                "id": f"{TIMELINE_ID_PREFIX}{bp.id}",
                "patient_id": patient_id,
                "event_type": "vital",
                "label": bp.label,
                "occurred_at": bp.observed_at,
                "detail": "Clinic blood pressure reading",
            }
        )
    for appt in chart.missed_followups:
        events.append(
            {
                "id": f"{TIMELINE_ID_PREFIX}{appt.id}",
                "patient_id": patient_id,
                "event_type": "care_gap",
                "label": "Missed follow-up",
                "occurred_at": appt.start,
                "detail": appt.description or None,
            }
        )
    for enc in chart.encounters:
        events.append(
            {
                "id": f"{TIMELINE_ID_PREFIX}{enc.id}",
                "patient_id": patient_id,
                "event_type": "encounter",
                "label": enc.label,
                "occurred_at": enc.started_at,
                "detail": None,
            }
        )
    for appt in chart.upcoming_appointments:
        events.append(
            {
                "id": f"{TIMELINE_ID_PREFIX}{appt.id}",
                "patient_id": patient_id,
                "event_type": "encounter",
                "label": appt.description or "Scheduled visit",
                "occurred_at": appt.start,
                "detail": "Booked appointment",
            }
        )
    return events
