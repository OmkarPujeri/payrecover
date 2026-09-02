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
import logging
from typing import Any, Callable

from app.agent.tools import GROQ_TOOLS, TOOL_NAMES
from app.config import Settings, settings as global_settings

logger = logging.getLogger(__name__)

# Default Groq chat model. The buildathon originally standardised on
# llama-3.3-70b-versatile, but Groq decommissioned it (every call 404s), so we
# default to a current free-plan model. Overridable via the GROQ_MODEL env var
# (see app.config.Settings.groq_model). Free-tier limits for the gpt-oss / qwen3
# chat models are identical: 30 RPM, 1K RPD, 8K TPM, 200K TPD
# (console.groq.com/docs/rate-limits).
DEFAULT_MODEL = "openai/gpt-oss-120b"

# A fallback takes the user payload dict and returns the diagnosis/decision dict.
JsonFallback = Callable[[dict[str, Any]], dict[str, Any]]

# A tool fallback (the deterministic planner) returns (tool_name, args, meta).
ToolFallback = Callable[[dict[str, Any]], tuple[str, dict[str, Any], dict[str, Any]]]


class LLMClient:
    """Async wrapper around Groq chat-completions with a deterministic fallback."""

    def __init__(self, settings: Settings = global_settings) -> None:
        self.simulation: bool = not settings.groq_configured
        self.model: str = settings.groq_model or DEFAULT_MODEL
        self._client: Any = None

        if not self.simulation:
            try:
                from groq import Groq  # official SDK — imported only when live

                self._client = Groq(api_key=settings.groq_api_key)
            except Exception:  # noqa: BLE001 — any import/auth issue -> mock
                logger.warning("Groq client init failed — falling back to mock", exc_info=True)
                self.simulation = True

    @property
    def mode(self) -> str:
        return "mock" if self.simulation else "live"

    def _model_kwargs(self) -> dict[str, Any]:
        """Model-specific extra kwargs for a chat-completions call.

        The gpt-oss models are *reasoning* models: they spend completion tokens
        thinking before answering, which both eats the token budget (free tier
        is 8K TPM) and risks truncating the JSON/tool answer. Our tasks —
        classify a failure, pick a bounded tool — need no deep reasoning, so we
        pin ``reasoning_effort="low"``. Guarded to gpt-oss so it's never sent to
        a model that would reject it (which would otherwise 400 every call).
        """
        if self.model.startswith("openai/gpt-oss"):
            return {"reasoning_effort": "low"}
        return {}

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_payload: dict[str, Any],
        fallback: JsonFallback,
        temperature: float = 0.1,
        max_tokens: int = 1024,
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
            logger.warning(
                "Live Groq JSON call failed (%s) — using deterministic fallback",
                self.model,
                exc_info=True,
            )
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
            **self._model_kwargs(),
        )
        return resp.choices[0].message.content or "{}"

    async def complete_tool_call(
        self,
        *,
        system_prompt: str,
        user_payload: dict[str, Any],
        fallback: ToolFallback,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> tuple[str, dict[str, Any], dict[str, Any], str]:
        """Return ``(tool_name, tool_args, meta, source)`` for the Strategy Agent.

        The LLM selects *which* bounded tool to call and *how* to parameterise it
        (``tool_choice="required"`` guarantees exactly one call). The accompanying
        ``meta`` — crucially the ``confidence`` that drives the human-review gate —
        is always taken from the deterministic ``fallback`` (the planner), never
        from the model. A model must not be able to talk its way past HITL by
        asserting a high confidence, so action selection may be learned but the
        gate input stays deterministic and auditable.

        In mock mode, or on any live error / unknown tool / malformed arguments,
        the deterministic ``fallback`` supplies the whole decision.
        """
        if self.simulation:
            tool_name, args, meta = fallback(user_payload)
            return tool_name, args, {**meta, "source": "mock"}, "mock"

        try:
            tool_name, args = await asyncio.to_thread(
                self._raw_tool_call,
                system_prompt,
                user_payload,
                temperature,
                max_tokens,
            )
            if tool_name not in TOOL_NAMES:
                raise ValueError(f"LLM chose unknown tool {tool_name!r}")
            # Confidence/risk come from the deterministic brain (gate integrity).
            _, _, meta = fallback(user_payload)
            return tool_name, args, {**meta, "source": "llm"}, "llm"
        except Exception:  # noqa: BLE001 — never let an LLM hiccup break recovery
            logger.warning(
                "Live Groq tool call failed (%s) — using deterministic fallback",
                self.model,
                exc_info=True,
            )
            tool_name, args, meta = fallback(user_payload)
            return tool_name, args, {**meta, "source": "mock"}, "mock"

    def _raw_tool_call(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        temperature: float,
        max_tokens: int,
    ) -> tuple[str, dict[str, Any]]:
        """Synchronous Groq tool-calling request (run via ``asyncio.to_thread``)."""
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, default=str)},
            ],
            tools=GROQ_TOOLS,
            tool_choice="required",
            temperature=temperature,
            max_tokens=max_tokens,
            **self._model_kwargs(),
        )
        message = resp.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []
        if not tool_calls:
            raise ValueError("LLM returned no tool call")
        call = tool_calls[0]
        args = json.loads(call.function.arguments or "{}")
        if not isinstance(args, dict):
            raise ValueError("tool arguments were not a JSON object")
        return call.function.name, args


# App-wide singleton (mode fixed at import from current settings).
llm_client = LLMClient()
