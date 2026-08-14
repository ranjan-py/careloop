"""Feedback module (spec §26).

The rejection taxonomy is a SINGLE shared enum (spec §14) defined once in
app.schemas.core and re-exported here for discoverability — never redefine it.
Decision persistence lives in app.care_plan.router; aggregate analytics in
app.analytics.router.
"""

from app.schemas.core import REJECTION_CATEGORY_LABELS, RejectionCategory

__all__ = ["RejectionCategory", "REJECTION_CATEGORY_LABELS"]
