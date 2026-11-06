from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import engine, Base
from app.models import *  # noqa: F401, F403 — ensure all models are registered
from app.routers import auth, resume, interview, dashboard, payment
from app.config import settings
from app.services.report_notifications import ReportNotificationHub, install_report_notifications


async def _prefetch_google_keys():
    """Pre-fetch Google signing keys in background so Firebase auth is fast."""
    import asyncio
    import urllib.request
    try:
        await asyncio.to_thread(
            urllib.request.urlopen,
            "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com",
            None,
            settings.firebase_http_timeout,
        )
    except Exception:
        pass


async def _ensure_schema_columns():
    """Apply small additive schema updates for deployments without migrations."""
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "ALTER TABLE interviews ADD COLUMN IF NOT EXISTS pipecat_session_id VARCHAR"
        )
        # Coding round columns
        await conn.exec_driver_sql(
            "ALTER TABLE interviews ADD COLUMN IF NOT EXISTS interview_mode VARCHAR DEFAULT 'voice'"
        )
        await conn.exec_driver_sql(
            "ALTER TABLE interviews ADD COLUMN IF NOT EXISTS coding_language VARCHAR"
        )
        await conn.exec_driver_sql(
            "ALTER TABLE interviews ADD COLUMN IF NOT EXISTS coding_problem JSONB"
        )
        await conn.exec_driver_sql(
            "ALTER TABLE interviews ADD COLUMN IF NOT EXISTS final_code TEXT"
        )
        await conn.exec_driver_sql(
            "ALTER TABLE interviews ADD COLUMN IF NOT EXISTS code_finalized_at TIMESTAMPTZ"
        )
        await conn.exec_driver_sql(
            "ALTER TABLE interviews ADD COLUMN IF NOT EXISTS incomplete_reason TEXT"
        )
        # Code review in reports
        await conn.exec_driver_sql(
            "ALTER TABLE interview_reports ADD COLUMN IF NOT EXISTS code_review JSONB"
        )
        # User preferences columns
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS resume_text TEXT"
        )
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS resume_file_name VARCHAR"
        )
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS role_type VARCHAR"
        )
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS level VARCHAR"
        )
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS years_of_experience SMALLINT"
        )
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS employment_type VARCHAR"
        )
        # Plan & credits columns
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS plan VARCHAR DEFAULT 'free'"
        )
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS credits_remaining INTEGER DEFAULT 2"
        )
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_super_admin BOOLEAN DEFAULT FALSE"
        )


def _log_llm_config() -> None:
    """Log the resolved LLM provider, and warn loudly if it can't work.

    A silently-misconfigured provider used to surface as a confusing auth error
    deep inside a request, so surface it at boot instead.
    """
    from loguru import logger

    from app.services.llm_client import active_provider_info

    info = active_provider_info()
    if info.get("error"):
        logger.error(f"LLM CONFIG INVALID: {info['error']}")
        return
    logger.info(
        f"LLM provider: {info['provider']} (api={info['api']}, "
        f"base_url={info['base_url']}) models={info['models']}"
    )
    if not info["has_key"]:
        logger.error(
            f"LLM provider '{info['provider']}' has NO API KEY set — every "
            f"AI feature will fail. Set {info['provider'].upper()}_API_KEY in .env."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    _log_llm_config()
    # Create all tables on startup
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _ensure_schema_columns()
    async with engine.begin() as conn:
        await install_report_notifications(conn)
    notifications = ReportNotificationHub(engine)
    await notifications.start()
    app.state.report_notifications = notifications
    # Pre-fetch Google keys in background (don't block startup)
    asyncio.create_task(_prefetch_google_keys())
    try:
        yield
    finally:
        await notifications.close()
        await engine.dispose()


app = FastAPI(title="AI Interview Bot", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(auth.router)
app.include_router(resume.router)
app.include_router(interview.router)
app.include_router(dashboard.router)
app.include_router(payment.router)


@app.get("/health")
async def health_check():
    return {"status": "ok"}
