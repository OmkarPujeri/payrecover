"""Application configuration.

All external credentials are OPTIONAL. When Razorpay keys are absent the
backend runs in SIMULATION mode (mock API responses); when the Groq key is
absent the agent layer (added later) falls back to a deterministic mock.
This lets the whole system run and demo with zero credentials.
"""
from __future__ import annotations

import json
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- App ----
    app_env: str = "development"
    app_port: int = 8000
    frontend_url: str = "http://localhost:3000"
    cors_origins: list[str] = ["http://localhost:3000"]

    # ---- Database ----
    database_url: str = (
        "postgresql+asyncpg://payrecover:payrecover@localhost:5432/payrecover"
    )

    # ---- Razorpay (optional) ----
    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = None
    razorpay_webhook_secret: str | None = None

    # ---- Groq (optional) ----
    groq_api_key: str | None = None
    # Free-tier chat model (see console.groq.com/docs/rate-limits). Swappable via
    # the GROQ_MODEL env var without a code change; llama-3.3-70b-versatile was
    # decommissioned, so it defaults to a current free-plan model.
    groq_model: str = "openai/gpt-oss-120b"

    # ---- Toggles ----
    force_simulation: bool = False

    # --- Parsing helpers ---------------------------------------------------
    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors(cls, v: object) -> object:
        """Accept a JSON array, a comma-separated string, or a list."""
        if v is None or v == "":
            return ["http://localhost:3000"]
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                try:
                    return json.loads(s)
                except json.JSONDecodeError:
                    pass
            return [item.strip() for item in s.split(",") if item.strip()]
        return v

    @field_validator("razorpay_key_id", "razorpay_key_secret",
                     "razorpay_webhook_secret", "groq_api_key", mode="before")
    @classmethod
    def _empty_to_none(cls, v: object) -> object:
        """Treat empty-string env vars as unset."""
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    # --- Derived flags -----------------------------------------------------
    @property
    def razorpay_configured(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    @property
    def simulation_mode(self) -> bool:
        """True when Razorpay calls should be simulated."""
        return self.force_simulation or not self.razorpay_configured

    @property
    def groq_configured(self) -> bool:
        return bool(self.groq_api_key)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
