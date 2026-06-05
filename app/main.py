from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import engine, Base
from app.models import *  # noqa: F401, F403 — ensure all models are registered
from app.routers import auth, resume, interview
from app.config import settings


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    # Create all tables on startup
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _ensure_schema_columns()
    # Pre-fetch Google keys in background (don't block startup)
    asyncio.create_task(_prefetch_google_keys())
    yield
    # Clean up DB connections on shutdown
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


@app.get("/health")
async def health_check():
    return {"status": "ok"}
