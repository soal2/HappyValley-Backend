"""心晴谷 Backend – FastAPI application entry point."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import expand_cors_origins, get_settings
from app.database import async_session_factory
from app.routes.activities import router as activities_router
from app.routes.auth import router as auth_router
from app.routes.postcards import router as postcards_router
from app.routes.meditation_cards import router as meditation_cards_router
from app.routes.gratitude_cards import router as gratitude_cards_router
from app.routes.tts import router as tts_router
from app.routes.websocket import router as websocket_router
from app.services import character_generation_service
from app.services.maintenance_service import maintain_runtime_state

logger = structlog.get_logger()
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: startup and shutdown hooks."""
    logger.info("happyvalley_backend.startup")
    stop = asyncio.Event()

    async def maintenance_loop() -> None:
        startup = True
        while not stop.is_set():
            async with async_session_factory() as session:
                try:
                    await maintain_runtime_state(session, startup=startup)
                    await session.commit()
                except Exception:
                    await session.rollback()
                    logger.exception("maintenance.failed")
            startup = False
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                pass

    maintenance_task = asyncio.create_task(maintenance_loop())
    yield
    stop.set()
    maintenance_task.cancel()
    with suppress(asyncio.CancelledError):
        await maintenance_task
    await character_generation_service.cancel_all()
    logger.info("happyvalley_backend.shutdown")


app = FastAPI(
    title="心晴谷 Backend",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=expand_cors_origins(settings.CORS_ORIGINS),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(auth_router, tags=["auth"])
app.include_router(activities_router, tags=["activities"])
app.include_router(postcards_router, tags=["postcards"])
app.include_router(meditation_cards_router, tags=["meditation-cards"])
app.include_router(gratitude_cards_router, tags=["gratitude-cards"])
app.include_router(tts_router, tags=["tts"])
app.include_router(websocket_router, tags=["websocket"])


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}
