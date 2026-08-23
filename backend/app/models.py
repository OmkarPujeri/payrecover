"""ORM models — PayRecover core schema (PRD section 20).

Three tables: recovery_events, recovery_actions, circuit_breaker_events.
Uses portable types (``Uuid``, ``JSON`` with a Postgres ``JSONB`` variant,
Python-side UUID/timestamp defaults) so the schema runs identically on
PostgreSQL and SQLite.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# Portable JSON: JSONB on Postgres, plain JSON elsewhere (e.g. SQLite).
JSONVar = JSON().with_variant(JSONB, "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RecoveryEvent(Base):
    __tablename__ = "recovery_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # Razorpay identifiers
    razorpay_payment_id: Mapped[str] = mapped_column(String(50), nullable=False)
    razorpay_order_id: Mapped[str] = mapped_column(String(50), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # paise
    currency: Mapped[str] = mapped_column(String(3), default="INR")

    # Razorpay error fields (5-part structure)
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_source: Mapped[str | None] = mapped_column(String(20))
    error_step: Mapped[str | None] = mapped_column(String(30))
    error_reason: Mapped[str | None] = mapped_column(String(100))
    error_description: Mapped[str | None] = mapped_column(Text)

    # Customer
    customer_email: Mapped[str | None] = mapped_column(String(255))
    customer_contact: Mapped[str | None] = mapped_column(String(20))
    customer_name: Mapped[str | None] = mapped_column(String(255))
    payment_method: Mapped[str | None] = mapped_column(String(20))
    customer_dnd: Mapped[bool] = mapped_column(Boolean, default=False)

    # Diagnosis (populated by the Diagnostic Agent — later phase)
    failure_category: Mapped[str | None] = mapped_column(String(10))  # soft/hard/terminal
    failure_label: Mapped[str | None] = mapped_column(String(50))
    root_cause_analysis: Mapped[str | None] = mapped_column(Text)
    recoverability_score: Mapped[int | None] = mapped_column(Integer)  # 0-100

    # Recovery state
    recovery_status: Mapped[str] = mapped_column(String(20), default="pending")
    recovery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    recovered_amount: Mapped[int] = mapped_column(Integer, default=0)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recovery_cost_paise: Mapped[int] = mapped_column(Integer, default=0)

    # Circuit-breaker flags
    has_dispute: Mapped[bool] = mapped_column(Boolean, default=False)
    customer_opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    subscription_cancelled: Mapped[bool] = mapped_column(Boolean, default=False)

    # Metadata
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    cascade_group_id: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    actions: Mapped[list["RecoveryAction"]] = relationship(
        back_populates="event", cascade="all, delete-orphan", lazy="selectin"
    )
    circuit_breaker_events: Mapped[list["CircuitBreakerEvent"]] = relationship(
        back_populates="event", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        Index("idx_events_status", "recovery_status"),
        Index("idx_events_order", "razorpay_order_id"),
        Index("idx_events_payment", "razorpay_payment_id"),
        Index("idx_events_created", "created_at"),
        Index("idx_events_cascade", "cascade_group_id"),
    )


class RecoveryAction(Base):
    __tablename__ = "recovery_actions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    recovery_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recovery_events.id", ondelete="CASCADE"), nullable=False
    )

    # Agent info
    agent_name: Mapped[str] = mapped_column(String(30), nullable=False)  # diagnostic/strategy/compliance
    action_type: Mapped[str] = mapped_column(String(30), nullable=False)
    action_params: Mapped[dict] = mapped_column(JSONVar, nullable=False, default=dict)

    # LLM output
    agent_reasoning: Mapped[str | None] = mapped_column(Text)
    confidence_score: Mapped[int | None] = mapped_column(Integer)  # 0-100
    risk_factors: Mapped[list | None] = mapped_column(JSONVar)
    uncertainty_factors: Mapped[list | None] = mapped_column(JSONVar)

    # Compliance (deterministic engine — later phase)
    compliance_decision: Mapped[str | None] = mapped_column(String(10))  # APPROVED/MODIFIED/BLOCKED
    compliance_rule_id: Mapped[str | None] = mapped_column(String(20))
    compliance_rule_name: Mapped[str | None] = mapped_column(String(50))
    compliance_reason: Mapped[str | None] = mapped_column(Text)

    # Execution
    status: Mapped[str] = mapped_column(String(20), default="scheduled")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONVar)
    cost_paise: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    event: Mapped["RecoveryEvent"] = relationship(back_populates="actions")

    __table_args__ = (
        Index("idx_actions_event", "recovery_event_id"),
        Index("idx_actions_status", "status"),
        Index("idx_actions_created", "created_at"),
    )


class CircuitBreakerEvent(Base):
    __tablename__ = "circuit_breaker_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    recovery_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recovery_events.id", ondelete="CASCADE"), nullable=False
    )
    trigger_type: Mapped[str] = mapped_column(String(50), nullable=False)
    trigger_id: Mapped[str] = mapped_column(String(10), nullable=False)  # CB-001..CB-008
    trigger_details: Mapped[dict | None] = mapped_column(JSONVar)
    cancelled_actions: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    event: Mapped["RecoveryEvent"] = relationship(back_populates="circuit_breaker_events")

    __table_args__ = (Index("idx_cb_event", "recovery_event_id"),)
