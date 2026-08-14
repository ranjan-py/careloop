"""Analytics current-session merge math (spec §23) — pure helpers only."""

from __future__ import annotations

from app.analytics.router import merge_decision_mix, merge_reasons
from app.schemas.core import REJECTION_CATEGORY_LABELS, RejectionCategory


class TestMergeDecisionMix:
    def test_synthetic_only(self):
        mix = merge_decision_mix({"approved": 80, "modified": 15, "rejected": 5}, {})
        assert mix["accepted"] == 80.0
        assert mix["modified"] == 15.0
        assert mix["rejected"] == 5.0
        assert mix["total_events"] == 100
        assert "0 current-session" in mix["detail"]

    def test_live_decisions_shift_the_mix(self):
        mix = merge_decision_mix(
            {"approved": 96, "modified": 0, "rejected": 0},
            {"approved": 2, "modified": 1, "rejected": 1},
        )
        assert mix["total_events"] == 100
        assert mix["accepted"] == 98.0
        assert mix["modified"] == 1.0
        assert mix["rejected"] == 1.0
        assert "96 synthetic-cohort events + 4 current-session" in mix["detail"]

    def test_empty_everything(self):
        mix = merge_decision_mix({}, {})
        assert mix["accepted"] == 0.0 and mix["total_events"] == 0

    def test_percentages_round_to_one_decimal(self):
        mix = merge_decision_mix({"approved": 1, "modified": 1, "rejected": 1}, {})
        assert mix["accepted"] == 33.3


class TestMergeReasons:
    def test_exact_shared_enum_labels_and_order(self):
        reasons = merge_reasons({}, {})
        assert [r["category"] for r in reasons] == [c.value for c in RejectionCategory]
        assert [r["label"] for r in reasons] == [
            REJECTION_CATEGORY_LABELS[c] for c in RejectionCategory
        ]

    def test_live_counts_merge_and_break_out(self):
        synthetic = {"patient_limitation": 31, "clinical_disagreement": 20}
        live = {"patient_limitation": 1}
        by_cat = {r["category"]: r for r in merge_reasons(synthetic, live)}
        assert by_cat["patient_limitation"]["count"] == 32
        assert by_cat["patient_limitation"]["current_session"] == 1
        assert by_cat["clinical_disagreement"]["count"] == 20
        assert by_cat["clinical_disagreement"]["current_session"] == 0
        assert by_cat["other"]["count"] == 0

    def test_labels_never_variant(self):
        for r in merge_reasons({}, {}):
            assert r["label"] == REJECTION_CATEGORY_LABELS[RejectionCategory(r["category"])]
