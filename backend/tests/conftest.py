"""Pytest fixtures — run the app against a throwaway SQLite database.

Environment is set BEFORE importing the app so the cached settings/engine
pick up SQLite. Tables are recreated per test for isolation.
"""
import os

# Must be set before importing anything under app.*
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_payrecover.db"
os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", "")
os.environ.setdefault("RAZORPAY_KEY_ID", "")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "")
os.environ.setdefault("GROQ_API_KEY", "")

import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app import models  # noqa: E402,F401  (register tables on Base.metadata)
from app.database import AsyncSessionLocal, Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def _fresh_schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def session():
    """A direct DB session, for tests that drive services below the HTTP layer."""
    async with AsyncSessionLocal() as s:
        yield s
