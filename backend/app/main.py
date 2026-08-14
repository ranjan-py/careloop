"""CareLoop backend — app factory and wiring only (no business logic here).

Synthetic clinical AI prototype — not for patient care.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import health
from app.ai.client import verify_model
from app.ai.evidence import router as evidence_router
from app.analytics.router import router as analytics_router
from app.auth.router import router as auth_router
from app.care_plan.router import router as care_plan_router
from app.config import get_settings
from app.context.neo4j_client import close_driver
from app.context.router import router as context_router
from app.db.session import dispose_engine, init_db
from app.encounters.router import router as encounters_router
from app.evals.router import router as evals_router
from app.observability.router import router as observability_router
from app.patients.router import router as patients_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SAFETY_BANNER = "Synthetic clinical AI prototype — not for patient care."


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_ready = await init_db()
    model_verification = await verify_model()
    app.state.db_ready = db_ready
    app.state.model_verification = model_verification
    if model_verification["status"] != "ok":
        logger.warning(
            "OpenAI model NOT verified (%s): %s — AI features are BLOCKED until verified.",
            model_verification["status"],
            model_verification["detail"],
        )
    yield
    await close_driver()
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="CareLoop Backend",
        description=SAFETY_BANNER,
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_url],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth_router)
    app.include_router(patients_router)
    app.include_router(encounters_router)
    app.include_router(care_plan_router)
    app.include_router(evidence_router)
    app.include_router(context_router)
    app.include_router(analytics_router)
    app.include_router(evals_router)
    app.include_router(observability_router)
    app.include_router(health.router)

    @app.get("/")
    async def root() -> dict:
        return {"service": "careloop-backend", "banner": SAFETY_BANNER}

    return app


app = create_app()
