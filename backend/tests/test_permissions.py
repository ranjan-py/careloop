"""Permission-tier enforcement (spec §13/§15) — the gate must visibly bite."""

from app.tools.registry import check_permission


class TestAutoDemo:
    def test_executes_without_decision(self):
        check = check_permission("auto_demo", "pending")
        assert check.allowed is True

    def test_never_executes_a_rejected_action(self):
        check = check_permission("auto_demo", "rejected")
        assert check.allowed is False


class TestClinicianReview:
    def test_pending_blocked(self):
        check = check_permission("clinician_review", "pending")
        assert check.allowed is False
        assert "BLOCKED" in check.reason

    def test_approved_allowed(self):
        assert check_permission("clinician_review", "approved").allowed is True

    def test_modified_allowed(self):
        assert check_permission("clinician_review", "modified").allowed is True

    def test_rejected_blocked(self):
        assert check_permission("clinician_review", "rejected").allowed is False


class TestRequiredClinicianDecision:
    def test_pending_blocked_hard_stop(self):
        check = check_permission("required_clinician_decision", "pending")
        assert check.allowed is False
        assert "required_clinician_decision" in check.reason

    def test_approved_allowed(self):
        assert check_permission("required_clinician_decision", "approved").allowed is True

    def test_denied_blocked(self):
        assert check_permission("required_clinician_decision", "denied").allowed is False


def test_check_reports_its_tier():
    check = check_permission("required_clinician_decision", "pending")
    assert check.tier == "required_clinician_decision"
