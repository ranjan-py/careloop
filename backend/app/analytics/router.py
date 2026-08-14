"""Feedback analytics — GET /api/analytics/feedback (spec §23).

Aggregates the seeded synthetic cohort (FeedbackAnalyticsEvent rows) MERGED
with the current session's live clinician decisions (Decision rows): the
decision mix and rejection reasons include both (labeled in the detail), a
``current_session`` sub-object carries the live counts, and the high-friction
table stays synthetic-cohort-only (its pinned display comes from spec §23).
Reason labels come from the SINGLE shared RejectionCategory enum — display
labels exactly, no variants. Everything is labeled "Synthetic demo cohort".
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.session import require_clinician
from app.config import get_settings
from app.db import models as m
from app.db.session import get_session
from app.schemas.core import REJECTION_CATEGORY_LABELS, RejectionCategory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

COHORT_LABEL = "Synthetic demo cohort"

#: Decision.decision_type -> the shared mix vocabulary.
_DECISION_TYPE_MAP = {"approve": "approved", "modify": "modified", "reject": "rejected"}


# ---------------------------------------------------------------------------
# Pure merge math (unit-tested)
# ---------------------------------------------------------------------------


def merge_decision_mix(
    synthetic_counts: dict[str, int], live_counts: dict[str, int]
) -> dict:
    """Combined decision mix over synthetic cohort + current-session decisions.

    Frontend DecisionMix reads percentage keys ``accepted``/``modified``/
    ``rejected`` (see frontend/src/lib/types.ts).
    """
    combined = {
        key: synthetic_counts.get(key, 0) + live_counts.get(key, 0)
        for key in ("approved", "modified", "rejected")
    }
    total = sum(combined.values())
    synthetic_total = sum(
        synthetic_counts.get(k, 0) for k in ("approved", "modified", "rejected")
    )
    live_total = sum(live_counts.get(k, 0) for k in ("approved", "modified", "rejected"))

    def pct(n: int) -> float:
        return round(100.0 * n / total, 1) if total else 0.0

    return {
        "accepted": pct(combined["approved"]),
        "modified": pct(combined["modified"]),
        "rejected": pct(combined["rejected"]),
        "total_events": total,
        "detail": (
            f"{synthetic_total} synthetic-cohort events + {live_total} "
            f"current-session decision(s)"
        ),
    }


def merge_reasons(
    synthetic_reasons: dict[str, int], live_reasons: dict[str, int]
) -> list[dict]:
    """Per-category counts (shared enum, exact display labels), synthetic +
    current-session combined, with the live share broken out."""
    return [
        {
            "category": category.value,
            "label": REJECTION_CATEGORY_LABELS[category],
            "count": synthetic_reasons.get(category.value, 0)
            + live_reasons.get(category.value, 0),
            "current_session": live_reasons.get(category.value, 0),
        }
        for category in RejectionCategory
    ]


# ---------------------------------------------------------------------------
# High-friction enrichment from the pinned synthetic aggregates (spec §23)
# ---------------------------------------------------------------------------


@lru_cache
def _pinned_high_friction() -> dict[str, dict]:
    """title -> {trend, top_reason_detail, organizations_observed} from
    data/historical_feedback.json (the seed's source of truth). Missing file
    degrades to plain DB-derived rows."""
    path = Path(get_settings().data_dir) / "historical_feedback.json"
    try:
        src = json.loads(path.read_text(encoding="utf-8"))
        return {row["action_title"]: row for row in src.get("high_friction", [])}
    except Exception as exc:  # noqa: BLE001 — enrichment only, degrade quietly
        logger.warning("historical_feedback.json unavailable for enrichment: %s", exc)
        return {}


@router.get("/feedback")
async def feedback_analytics(
    session: AsyncSession = Depends(get_session),
    _=Depends(require_clinician),
) -> dict:
    try:
        synthetic_decisions = (
            await session.execute(
                select(m.FeedbackAnalyticsEvent.decision, func.count())
                .group_by(m.FeedbackAnalyticsEvent.decision)
            )
        ).all()
        synthetic_reason_rows = (
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
        # Current-session live decisions (spec §23 merge): latest decision per
        # action is the operative one (a re-decided action counts once).
        latest_per_action = (
            select(
                m.Decision.action_id,
                func.max(m.Decision.decided_at).label("last_at"),
            ).group_by(m.Decision.action_id)
        ).subquery()
        live_rows = (
            await session.execute(
                select(m.Decision.decision_type, m.Decision.rejection_category)
                .join(
                    latest_per_action,
                    (m.Decision.action_id == latest_per_action.c.action_id)
                    & (m.Decision.decided_at == latest_per_action.c.last_at),
                )
            )
        ).all()
    except Exception as exc:  # noqa: BLE001 — report degraded, don't fabricate analytics
        logger.warning("Analytics query failed: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Analytics unavailable — app database unreachable.",
        )

    synthetic_counts = {decision: count for decision, count in synthetic_decisions}
    live_counts: dict[str, int] = {}
    live_reasons: dict[str, int] = {}
    for decision_type, rejection_category in live_rows:
        key = _DECISION_TYPE_MAP.get(decision_type)
        if key is None:
            continue
        live_counts[key] = live_counts.get(key, 0) + 1
        if key == "rejected" and rejection_category:
            live_reasons[rejection_category] = live_reasons.get(rejection_category, 0) + 1

    decision_mix = merge_decision_mix(synthetic_counts, live_counts)
    reasons = merge_reasons(
        {value: count for value, count in synthetic_reason_rows}, live_reasons
    )

    pinned = _pinned_high_friction()
    high_friction = []
    for title, row_total, friction_count in friction_rows:
        friction = int(friction_count or 0)
        if not row_total:
            continue
        item = {
            "title": title,
            "total": row_total,
            "modify_reject_pct": round(100.0 * friction / row_total, 1),
        }
        extra = pinned.get(title)
        if extra:
            item["trend"] = extra.get("trend")
            item["top_reason"] = extra.get("top_reason_detail")
            item["organizations"] = extra.get("organizations_observed")
        high_friction.append(item)
    high_friction.sort(key=lambda r: r["modify_reject_pct"], reverse=True)

    return {
        "decision_mix": decision_mix,
        "reasons": reasons,
        "high_friction": high_friction[:10],  # synthetic cohort only (spec §23)
        "current_session": {
            "approved": live_counts.get("approved", 0),
            "modified": live_counts.get("modified", 0),
            "rejected": live_counts.get("rejected", 0),
        },
        "cohort_label": COHORT_LABEL,
    }
