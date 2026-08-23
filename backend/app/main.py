"""PayRecover FastAPI application entrypoint.

Wires CORS, DB table creation on startup, the SSE stream, and all routers.
Run locally:  uvicorn app.main:app --reload --port 8000  (from the backend/ dir)
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.api.dashboard import router as dashboard_router
from app.api.simulator_routes import router as simulator_router
from app.config import settings
from app.database import init_db
from app.razorpay.client import razorpay_client
from app.sse import sse_manager
from app.webhooks.router import router as webhook_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("payrecover")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info(
        "PayRecover backend ready | razorpay=%s | groq=%s | db=%s",
        razorpay_client.mode,
        "live" if settings.groq_configured else "mock",
        "sqlite" if settings.is_sqlite else "postgres",
    )
    yield
    logger.info("PayRecover backend shutting down")


app = FastAPI(title="PayRecover API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhook_router)
app.include_router(simulator_router)
app.include_router(dashboard_router)


@app.get("/", tags=["meta"])
async def root():
    return {
        "service": "PayRecover",
        "status": "ok",
        "version": "0.1.0",
        "razorpay_mode": razorpay_client.mode,
        "groq": "live" if settings.groq_configured else "mock",
        "docs": "/docs",
    }


@app.get("/health", tags=["meta"])
async def health():
    return {
        "status": "healthy",
        "razorpay_mode": razorpay_client.mode,
        "groq_configured": settings.groq_configured,
        "simulation_mode": settings.simulation_mode,
        "sse_connections": sse_manager.connection_count,
    }


@app.get("/api/stream", tags=["stream"])
async def stream_events(request: Request):
    """Server-Sent Events stream of live recovery activity."""
    queue = await sse_manager.connect()

    async def event_generator():
        try:
            yield sse_manager.format({"type": "connected"})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield sse_manager.format(data)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"  # comment frame keeps the connection warm
        finally:
            sse_manager.disconnect(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
