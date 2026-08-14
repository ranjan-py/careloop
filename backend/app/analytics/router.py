"""Feedback analytics — GET /api/analytics/feedback (spec §23).

Aggregates the seeded synthetic historical events. Reason labels come from the
SINGLE shared RejectionCategory enum — display labels exactly, no variants.
Everything is labeled "Synthetic demo cohort".
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.session import require_clinician
from app.db import models as m
from app.db.session import get_session
from app.schemas.core import REJECTION_CATEGORY_LABELS, RejectionCategory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

COHORT_LABEL = "Synthetic demo cohort"


@router.get("/feedback")
async def feedback_analytics(
    session: AsyncSession = Depends(get_session),
    _=Depends(require_clinician),
) -> dict:
    try:
        decision_rows = (
            await session.execute(
                select(m.FeedbackAnalyticsEvent.decision, func.count())
                .group_by(m.FeedbackAnalyticsEvent.decision)
            )
        ).all()
        reason_rows = (
            await session.execute(
                select(m.FeedbackAnalyticsEvent.rejection_category, func.count())
                .where(m.FeedbackAnalyticsEvent.rejection_category.is_not(None))
                .group_by(m.FeedbackAnalyticsEvent.rejection_category)
            )
        ).all()
        friction_rows = (
            await session.execute(
                select(
                    m.FeedbackAnalyticsEvent.recommendation_title,
                    func.count().label("total"),
                    func.sum(
                        case(
                            (m.FeedbackAnalyticsEvent.decision.in_(["modified", "rejected"]), 1),
                            else_=0,
                        )
                    ),
                ).group_by(m.FeedbackAnalyticsEvent.recommendation_title)
            )
        ).all()
    except Exception as exc:  # noqa: BLE001 — report degraded, don't fabricate analytics
        logger.warning("Analytics query failed: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Analytics unavailable — app database unreachable.",
        )

    counts = {decision: count for decision, count in decision_rows}
    total = sum(counts.values())

    def pct(n: int) -> float:
        return round(100.0 * n / total, 1) if total else 0.0

    decision_mix = {
        "accepted_pct": pct(counts.get("approved", 0)),
        "modified_pct": pct(counts.get("modified", 0)),
        "rejected_pct": pct(counts.get("rejected", 0)),
        "total_events": total,
    }

    reason_counts = {value: count for value, count in reason_rows}
    reasons = [
        {
            "category": category.value,
            "label": REJECTION_CATEGORY_LABELS[category],
            "count": reason_counts.get(category.value, 0),
        }
        for category in RejectionCategory
    ]

    high_friction = []
    for title, row_total, friction_count in friction_rows:
        friction = int(friction_count or 0)
        if row_total:
            high_friction.append(
                {
                    "title": title,
                    "total": row_total,
                    "modify_reject_pct": round(100.0 * friction / row_total, 1),
                }
            )
    high_friction.sort(key=lambda r: r["modify_reject_pct"], reverse=True)

    return {
        "decision_mix": decision_mix,
        "reasons": reasons,
        "high_friction": high_friction[:10],
        "cohort_label": COHORT_LABEL,
    }
