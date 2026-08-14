"""Application settings — loads every variable declared in ../.env.example.

pydantic-settings reads process env first, then the optional .env file.
Defaults mirror .env.example placeholders so the app can boot (degraded but
honest) with nothing configured; the /api/health endpoint reports what is
actually reachable.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # App
    app_env: str = "development"
    app_secret: str = "change-me-local-only"
    demo_user_email: str = "maya.patel@careloop.demo"
    demo_user_password: str = "demo-only-password"
    frontend_url: str = "http://localhost:3000"
    backend_url: str = "http://localhost:8000"
    # Seed corpus / scenario bundle / audio fixture root: /data in Docker
    # (read-only mount), ../data when running from backend/ on the host.
    data_dir: str = "../data"

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-5-nano"
    openai_eval_model: str = "gpt-5-nano"
    # Care-plan + report generation carry the demo's hard guarantees
    # (fact grounding, modified-wording fidelity, no degeneration). Measured
    # on nano: 2/5 actions ungrounded and clinician wording paraphrased —
    # mini restores the guarantees for ~1-2 cents/encounter more.
    openai_generation_model: str = "gpt-5-mini"
    # GPT-5-family reasoning effort. nano at default effort took ~21 s per
    # structured call (measured); "low" = 4.1 s with clinically sharper output
    # than "minimal" (1.6 s). Empty string disables the parameter.
    openai_reasoning_effort: str = "low"

    # Deepgram
    deepgram_api_key: str = ""
    deepgram_model: str = "nova-3-medical"
    deepgram_diarize_model: str = "latest"

    # App Postgres
    app_database_url: str = (
        "postgresql+asyncpg://careloop:careloop-local@app-postgres:5432/careloop"
    )
    app_postgres_user: str = "careloop"
    app_postgres_password: str = "careloop-local"
    app_postgres_db: str = "careloop"

    # Neo4j
    neo4j_uri: str = "bolt://neo4j:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "careloop-neo4j-local"

    # Langfuse client (must match LANGFUSE_INIT_* — spec §19)
    langfuse_host: str = "http://langfuse-web:3000"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    # Langfuse headless initialization (consumed by the Langfuse containers;
    # loaded here so config validation covers the whole .env.example surface)
    langfuse_init_org_id: str = ""
    langfuse_init_org_name: str = ""
    langfuse_init_project_id: str = ""
    langfuse_init_project_name: str = ""
    langfuse_init_project_public_key: str = ""
    langfuse_init_project_secret_key: str = ""
    langfuse_init_user_email: str = ""
    langfuse_init_user_name: str = ""
    langfuse_init_user_password: str = ""

    # Langfuse infrastructure (compose-only; mirrored for completeness)
    langfuse_postgres_password: str = ""
    clickhouse_password: str = ""
    redis_auth: str = ""
    minio_root_password: str = ""
    langfuse_salt: str = ""
    langfuse_encryption_key: str = ""
    nextauth_secret: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
