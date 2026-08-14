"""Launch-criteria table math (spec §22.1) — pure, over row-shaped fixtures."""

from __future__ import annotations

from dataclasses import dataclass

from app.evals.criteria import (
    CRITERIA_SPEC,
    aggregate_launch_criteria,
    build_launch_criteria,
)


@dataclass
class Row:
    evaluator: str
    score: float
    passed: bool


def full_rows(**overrides) -> list[Row]:
    base = {
        "schema_validity": Row("schema_validity", 1.0, True),
        "unsupported_fact_detector": Row("unsupported_fact_detector", 1.0, True),
        "rejected_action_leakage": Row("rejected_action_leakage", 0.0, True),
        "modified_action_fidelity": Row("modified_action_fidelity", 1.0, True),
        "latency": Row("latency", 3.2, True),
        "cost_per_encounter": Row("cost_per_encounter", 0.21, True),
    }
    base.update(overrides)
    return list(base.values())


class TestBuildLaunchCriteria:
    def test_names_targets_and_order_match_spec(self):
        rows = build_launch_criteria(full_rows())
        assert [(r.name, r.target) for r in rows] == [
            ("Schema validity", "100%"),
            ("Unsupported-fact rate", "0%"),
            ("Rejected-action leakage", "0"),
            ("Modified-action fidelity", "100%"),
            ("p95 next-best-action latency", "< 6 s"),
            ("Cost per encounter", "< $0.50"),
        ]

    def test_all_green_actuals(self):
        rows = {r.name: r for r in build_launch_criteria(full_rows())}
        assert rows["Schema validity"].actual == "100%"
        assert rows["Schema validity"].passed
        assert rows["Unsupported-fact rate"].actual == "0%"
        assert rows["Rejected-action leakage"].actual == "0"
        assert rows["Modified-action fidelity"].actual == "100%"
        assert rows["p95 next-best-action latency"].actual == "3.2 s"
        assert "0.21" in rows["Cost per encounter"].actual
        assert "estimate" in rows["Cost per encounter"].actual
        assert all(r.passed for r in rows.values())

    def test_missing_rows_are_honestly_unmeasured(self):
        rows = build_launch_criteria([])
        assert all(r.actual == "not yet measured" for r in rows)
        assert not any(r.passed for r in rows)

    def test_failures_propagate(self):
        rows = {
            r.name: r
            for r in build_launch_criteria(
                full_rows(
                    unsupported_fact_detector=Row("unsupported_fact_detector", 0.75, False),
                    rejected_action_leakage=Row("rejected_action_leakage", 2.0, False),
                    latency=Row("latency", 7.4, False),
                )
            )
        }
        assert rows["Unsupported-fact rate"].actual == "25%"
        assert not rows["Unsupported-fact rate"].passed
        assert rows["Rejected-action leakage"].actual == "2"
        assert not rows["Rejected-action leakage"].passed
        assert rows["p95 next-best-action latency"].actual == "7.4 s"
        assert not rows["p95 next-best-action latency"].passed
        # untouched criteria stay green
        assert rows["Schema validity"].passed and rows["Cost per encounter"].passed

    def test_unmeasurable_latency(self):
        rows = {r.name: r for r in build_launch_criteria(full_rows(
            latency=Row("latency", -1.0, False)))}
        assert rows["p95 next-best-action latency"].actual == "not measurable"

    def test_accepts_dict_rows(self):
        dict_rows = [
            {"evaluator": r.evaluator, "score": r.score, "passed": r.passed}
            for r in full_rows()
        ]
        assert all(r.passed for r in build_launch_criteria(dict_rows))

    def test_last_row_per_evaluator_wins(self):
        rows = full_rows() + [Row("rejected_action_leakage", 3.0, False)]
        built = {r.name: r for r in build_launch_criteria(rows)}
        assert built["Rejected-action leakage"].actual == "3"


class TestAggregateLaunchCriteria:
    def test_aggregates_over_encounters(self):
        results = {
            "enc_a": full_rows(),
            "enc_b": full_rows(
                latency=Row("latency", 5.0, True),
                cost_per_encounter=Row("cost_per_encounter", 0.31, True),
                rejected_action_leakage=Row("rejected_action_leakage", 1.0, False),
            ),
        }
        rows = {r.name: r for r in aggregate_launch_criteria(results)}
        # leakage SUMS and any red encounter makes the criterion red
        assert rows["Rejected-action leakage"].actual.startswith("1")
        assert not rows["Rejected-action leakage"].passed
        # p95 over per-encounter p95 values -> worst of {3.2, 5.0}
        assert rows["p95 next-best-action latency"].actual.startswith("5.0 s")
        # cost is the MEAN estimate
        assert "0.26" in rows["Cost per encounter"].actual
        assert rows["Schema validity"].passed

    def test_missing_measurement_in_one_encounter_is_red(self):
        results = {
            "enc_a": full_rows(),
            "enc_b": [r for r in full_rows() if r.evaluator != "latency"],
        }
        rows = {r.name: r for r in aggregate_launch_criteria(results)}
        assert "unmeasured in 1/2" in rows["p95 next-best-action latency"].actual
        assert not rows["p95 next-best-action latency"].passed

    def test_empty_is_unmeasured(self):
        rows = aggregate_launch_criteria({})
        assert all(r.actual == "not yet measured" and not r.passed for r in rows)


def test_criteria_spec_covers_six_criteria():
    assert len(CRITERIA_SPEC) == 6
