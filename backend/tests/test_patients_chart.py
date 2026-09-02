"""Unit tests for the pre-visit chart projection (app.patients.router).

Covers the honesty rules the Screen 3 chart depends on: a BP point is only
plotted when the stored fact actually contains numbers, and every plotted
point carries the provenance of the fact it came from — an encounter-derived
patient report must never render as an EHR reading.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.db import models as m
from app.patients.router import blood_pressure_reading

NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)


def fact(
    *,
    fact_type: str = "observation",
    subject: str = "Blood pressure",
    value: str = "148/92 mmHg",
    source_type: str = "ehr",
    source_class: str = "synthea_ehr",
    method: str | None = None,
    encounter_id: str | None = None,
) -> m.Fact:
    return m.Fact(
        id="f1",
        patient_id="john-miller",
        fact_type=fact_type,
        subject=subject,
        value=value,
        source_type=source_type,
        source_class=source_class,
        method=method,
        encounter_id=encounter_id,
        reported_at=NOW,
        confidence=1.0,
        verification_status="confirmed",
    )


def test_chart_bp_parses_to_a_point():
    reading = blood_pressure_reading(fact())
    assert reading is not None
    assert (reading.systolic, reading.diastolic) == (148, 92)
    assert reading.value == "148/92 mmHg"  # verbatim stored string preserved
    assert reading.source_class == "synthea_ehr"
    assert reading.encounter_id is None


def test_kiosk_report_keeps_its_provenance():
    reading = blood_pressure_reading(
        fact(
            value="150/95 mmHg",
            source_type="patient_report",
            source_class="patient_report",
            method="pharmacy_kiosk",
            encounter_id="enc_001",
        )
    )
    assert reading is not None
    assert reading.source_class == "patient_report"
    assert reading.method == "pharmacy_kiosk"
    assert reading.encounter_id == "enc_001"


def test_approximate_report_plots_but_keeps_the_hedge_in_the_value():
    reading = blood_pressure_reading(fact(value="around 150/95 mmHg"))
    assert reading is not None
    assert (reading.systolic, reading.diastolic) == (150, 95)
    assert reading.value == "around 150/95 mmHg"


def test_unquantified_bp_speech_is_not_plottable():
    assert blood_pressure_reading(fact(value="creeping back up")) is None


def test_non_bp_observations_are_not_plottable():
    assert blood_pressure_reading(fact(subject="dizziness", value="resolved")) is None
    assert (
        blood_pressure_reading(
            fact(subject="blood_pressure_measurement_method", value="grocery_store_pharmacy_kiosk")
        )
        is None
    )


def test_lab_facts_are_not_read_as_vitals():
    assert blood_pressure_reading(fact(fact_type="lab", subject="Potassium", value="4.2 mmol/L")) is None
