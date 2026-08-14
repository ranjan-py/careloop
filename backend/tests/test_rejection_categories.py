"""RejectionCategory — single shared enum, exact wire values and display labels
(spec §14, CONTRACTS.md). These strings are load-bearing across the rejection
modal, persistence, analytics seed, and evaluators — do not 'fix' them."""

from app.schemas.core import REJECTION_CATEGORY_LABELS, RejectionCategory

EXPECTED = {
    "missing_patient_information": "Missing patient information",
    "clinical_disagreement": "Clinical disagreement",
    "patient_limitation": "Patient limitation",
    "operational_workflow_limitation": "Operational/workflow limitation",
    "organization_protocol_constraint": "Organization/protocol constraint",
    "requires_supervision_escalation": "Requires supervision/escalation",
    "other": "Other",
}


def test_exactly_seven_categories():
    assert len(RejectionCategory) == 7


def test_wire_values_exact():
    assert {c.value for c in RejectionCategory} == set(EXPECTED.keys())


def test_display_labels_exact():
    for category in RejectionCategory:
        assert category.display_label == EXPECTED[category.value]


def test_labels_mapping_covers_every_member():
    assert set(REJECTION_CATEGORY_LABELS.keys()) == set(RejectionCategory)


def test_feedback_module_reexports_same_object():
    from app.feedback import RejectionCategory as ReExported

    assert ReExported is RejectionCategory
