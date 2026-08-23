"""Razorpay API client wrapper — key-optional.

If Razorpay keys are configured, real API calls are made via the official
synchronous SDK, executed off the event loop with ``asyncio.to_thread``.
If keys are absent (or ``FORCE_SIMULATION`` is set), every method returns a
realistic *simulated* response tagged with ``"_simulated": True`` — so the
entire recovery pipeline runs and demos with zero credentials.

Only the endpoints the recovery engine needs are wrapped:
create_order, fetch_payment, create_payment_link, notify_payment_link.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

from app.config import Settings, settings as global_settings


def _gen_id(prefix: str) -> str:
    return f"{prefix}_sim_{secrets.token_hex(7)}"


class RazorpayClient:
    def __init__(self, settings: Settings = global_settings) -> None:
        self.simulation: bool = settings.simulation_mode
        self._client: Any = None
        self._sdk: Any = None

        if not self.simulation:
            try:
                import razorpay as razorpay_sdk  # official SDK (top-level pkg)

                self._sdk = razorpay_sdk
                self._client = razorpay_sdk.Client(
                    auth=(settings.razorpay_key_id, settings.razorpay_key_secret)
                )
            except Exception:  # noqa: BLE001 — any import/auth issue -> simulate
                self.simulation = True

    @property
    def mode(self) -> str:
        return "simulation" if self.simulation else "live"

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #
    async def create_order(
        self,
        amount: int,
        currency: str = "INR",
        receipt: str | None = None,
        notes: dict | None = None,
    ) -> dict:
        if self.simulation:
            return {
                "id": _gen_id("order"),
                "entity": "order",
                "amount": amount,
                "amount_paid": 0,
                "amount_due": amount,
                "currency": currency,
                "receipt": receipt,
                "status": "created",
                "attempts": 0,
                "notes": notes or {},
                "created_at": int(time.time()),
                "_simulated": True,
            }
        payload: dict[str, Any] = {
            "amount": amount,
            "currency": currency,
            "payment_capture": 1,
        }
        if receipt:
            payload["receipt"] = receipt
        if notes:
            payload["notes"] = notes
        return await asyncio.to_thread(self._client.order.create, payload)

    # ------------------------------------------------------------------ #
    # Payments
    # ------------------------------------------------------------------ #
    async def fetch_payment(self, payment_id: str) -> dict:
        if self.simulation:
            return {
                "id": payment_id,
                "entity": "payment",
                "status": "failed",
                "_simulated": True,
            }
        return await asyncio.to_thread(self._client.payment.fetch, payment_id)

    # ------------------------------------------------------------------ #
    # Payment Links (primary recovery tool)
    # ------------------------------------------------------------------ #
    async def create_payment_link(
        self,
        amount: int,
        description: str,
        customer: dict | None = None,
        notify: dict | None = None,
        expire_by: int | None = None,
        reminder_enable: bool = True,
        notes: dict | None = None,
        currency: str = "INR",
    ) -> dict:
        if self.simulation:
            link_id = _gen_id("plink")
            return {
                "id": link_id,
                "entity": "payment_link",
                "status": "created",
                "amount": amount,
                "currency": currency,
                "description": description,
                "short_url": f"https://rzp.io/i/{secrets.token_hex(5)}",
                "customer": customer or {},
                "notify": notify or {"sms": True, "email": True},
                "reminder_enable": reminder_enable,
                "expire_by": expire_by,
                "notes": notes or {},
                "created_at": int(time.time()),
                "_simulated": True,
            }
        payload: dict[str, Any] = {
            "amount": amount,
            "currency": currency,
            "description": description,
            "reminder_enable": reminder_enable,
        }
        if customer:
            payload["customer"] = customer
        if notify:
            payload["notify"] = notify
        if expire_by:
            payload["expire_by"] = expire_by
        if notes:
            payload["notes"] = notes
        return await asyncio.to_thread(self._client.payment_link.create, payload)

    async def notify_payment_link(self, link_id: str, medium: str = "email") -> dict:
        """Resend a payment-link notification (medium: 'sms' | 'email')."""
        if self.simulation:
            return {
                "success": True,
                "payment_link_id": link_id,
                "medium": medium,
                "_simulated": True,
            }
        return await asyncio.to_thread(
            self._client.payment_link.notify_by, link_id, medium
        )


# App-wide singleton (mode fixed at import from current settings).
razorpay_client = RazorpayClient()
