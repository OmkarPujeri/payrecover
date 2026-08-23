"""Groq LLM client wrapper — key-optional.

Mirrors the Razorpay client pattern (see ``app/razorpay/client.py``): if
``GROQ_API_KEY`` is present *and* the ``groq`` SDK imports, real
chat-completions are made off the event loop; otherwise — or on ANY live
error/parse failure — the call falls back to a deterministic function supplied
by the caller.

Two consequences that matter for this project:

* **Zero-credential runs.** With no Groq key the agent layer still produces the
  exact JSON shape the LLM would, because the "mock brain" is the same
  deterministic logic used for classification. Demos and tests are fully
  reproducible offline.
* **Graceful degradation.** Even in live mode, a timeout / rate-limit /
  malformed-JSON response never stalls the recovery pipeline — we silently drop
  to the deterministic fallback and tag the result so the source is auditable.

The ``groq`` package is imported ONLY inside the live branch, so mock mode does
not require it to be installed.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from app.config import Settings, settings as global_settings

# The model the buildathon standardises on (see PRD section 13).
DEFAULT_MODEL = "llama-3.3-70b-versatile"

# A fallback takes the user payload dict and returns the diagnosis/decision dict.
JsonFallback = Callable[[dict[str, Any]], dict[str, Any]]


class LLMClient:
    """Async wrapper around Groq chat-completions with a deterministic fallback."""

    def __init__(self, settings: Settings = global_settings) -> None:
        self.simulation: bool = not settings.groq_configured
        self.model: str = DEFAULT_MODEL
        self._client: Any = None

        if not self.simulation:
            try:
                from groq import Groq  # official SDK — imported only when live

                self._client = Groq(api_key=settings.groq_api_key)
            except Exception:  # noqa: BLE001 — any import/auth issue -> mock
                self.simulation = True

    @property
    def mode(self) -> str:
        return "mock" if self.simulation else "live"

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_payload: dict[str, Any],
        fallback: JsonFallback,
        temperature: float = 0.1,
        max_tokens: int = 500,
    ) -> tuple[dict[str, Any], str]:
        """Return ``(parsed_json, source)`` where ``source`` is ``"llm"`` or ``"mock"``.

        In mock mode, or if the live call fails / returns unparseable or
        non-object JSON, the deterministic ``fallback`` is used so the pipeline
        never stalls.
        """
        if self.simulation:
            return fallback(user_payload), "mock"

        try:
            content = await asyncio.to_thread(
                self._raw_completion,
                system_prompt,
                user_payload,
                temperature,
                max_tokens,
            )
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError("LLM returned non-object JSON")
            return data, "llm"
        except Exception:  # noqa: BLE001 — never let an LLM hiccup break recovery
            return fallback(user_payload), "mock"

    def _raw_completion(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Synchronous Groq call (run via ``asyncio.to_thread``)."""
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, default=str)},
            ],
            response_format={"type": "json_object"},
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or "{}"


# App-wide singleton (mode fixed at import from current settings).
llm_client = LLMClient()
