"""Pre-decision encounter summary (spec §12 step 4) — mocked model call."""

from __future__ import annotations

import asyncio

import pytest

from app.ai import encounter_summary
from app.ai.encounter_summary import (
    GeneratedSummary,
    SummaryOutput,
    build_summary_input,
    generate_encounter_summary,
)
from app.ai.extraction import FactView
from app.ai.prompts import PROMPT_DEFAULTS, PromptRegistry


def _fact(fact_id, subject, value):
    return FactView(
        id=fact_id, fact_type="observation", subject=subject, value=value,
        source_type="patient_report", source_class="patient_report",
        verification_status="unverified", encounter_id="enc_1",
    )


def test_prompt_default_registered_and_pre_decision():
    text = PROMPT_DEFAULTS["careloop_encounter_summary"]
    assert "NO plan" in text and "PRE-DECISION" in text.upper()
    assert "not for patient care" in text  # safety preamble


def test_build_summary_input_contains_facts_and_transcript():
    text = build_summary_input(
        [_fact("f1", "Blood pressure", "150/95 mmHg")], "Doctor: hello"
    )
    assert "f1 | observation | Blood pressure: 150/95 mmHg" in text
    assert "Doctor: hello" in text
    assert build_summary_input([], "").count("(no facts on record)") == 1


def test_generate_encounter_summary_mocked(monkeypatch):
    captured: dict = {}

    async def fake_call_model(*, task, instructions, input_text, output_schema,
                              model=None, prompt_version=None, usage_sink=None):
        captured.update(task=task, instructions=instructions,
                        schema=output_schema, prompt_version=prompt_version)
        if usage_sink is not None:
            usage_sink.update({"model": "fake-model", "input_tokens": 10,
                               "output_tokens": 5, "total_tokens": 15})
        return SummaryOutput(
            title="Visit summary",
            body="Patient reported stopping lisinopril; kiosk BP 150/95.",
        )

    monkeypatch.setattr("app.ai.client.call_model", fake_call_model)
    registry = PromptRegistry(client=None)  # in-code fallback prompts

    result = asyncio.run(
        generate_encounter_summary(
            "enc_1", "john-miller",
            [_fact("f1", "Blood pressure", "150/95 mmHg")],
            "Patient: I stopped the lisinopril.",
            trace_id=None,
            registry=registry,
        )
    )
    assert isinstance(result, GeneratedSummary)
    assert result.text.startswith("Visit summary")
    assert result.prompt_version == "careloop_encounter_summary@fallback"
    assert result.model_version == "fake-model"
    assert captured["task"] == encounter_summary.SPAN_NAME == "finalize_encounter_summary"
    assert captured["schema"] is SummaryOutput
    assert "NO plan" in captured["instructions"]


def test_provider_failure_propagates(monkeypatch):
    async def failing_call_model(**kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr("app.ai.client.call_model", failing_call_model)
    with pytest.raises(RuntimeError, match="provider down"):
        asyncio.run(
            generate_encounter_summary(
                "enc_1", "john-miller", [], "hi", trace_id=None,
                registry=PromptRegistry(client=None),
            )
        )
