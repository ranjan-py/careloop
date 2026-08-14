"""Mocked tool functions (spec §15) — structured results, demo labeling,
and the single deterministic failure simulation."""

from app.tools.registry import (
    CATEGORY_TOOL_MAP,
    DEMO_EXECUTION_LABEL,
    TOOLS,
    create_demo_lab_order,
)


class TestCreateDemoLabOrder:
    def test_returns_structured_result(self):
        result = create_demo_lab_order(patient_id="pat_miller", tests=["potassium"])
        assert result.tool_name == "create_demo_lab_order"
        assert result.status == "executed"
        assert result.record["patient_id"] == "pat_miller"
        assert result.record["tests"] == ["potassium"]
        assert result.executed_at is not None

    def test_carries_demo_label(self):
        result = create_demo_lab_order(patient_id="pat_miller")
        assert result.demo_label == DEMO_EXECUTION_LABEL
        assert "no real clinical order" in result.demo_label

    def test_deterministic_failure_simulation(self):
        result = create_demo_lab_order(patient_id="pat_miller", simulate_failure=True)
        assert result.status == "failed"
        assert result.record["failure_mode"] == "simulated"
        # Deterministic: same input, same outcome.
        again = create_demo_lab_order(patient_id="pat_miller", simulate_failure=True)
        assert again.status == "failed"


class TestRegistry:
    def test_exactly_six_tools(self):
        assert len(TOOLS) == 6
        assert set(TOOLS) == {
            "create_demo_lab_order",
            "schedule_demo_followup",
            "save_demo_medication_review",
            "generate_patient_instructions",
            "create_demo_referral",
            "send_demo_outreach_task",
        }

    def test_every_action_category_maps_to_a_registered_tool(self):
        for category, tool_name in CATEGORY_TOOL_MAP.items():
            assert tool_name in TOOLS, f"category {category} maps to unknown tool"

    def test_only_one_tool_supports_failure_simulation(self):
        import inspect

        with_flag = [
            name
            for name, fn in TOOLS.items()
            if "simulate_failure" in inspect.signature(fn).parameters
        ]
        assert with_flag == ["create_demo_lab_order"]
