from sqlalchemy import Column, Integer, String, Float, JSON, DateTime, Boolean, ForeignKey, UniqueConstraint, Index, func
from sqlalchemy.orm import relationship
from .database import Base

class AnalysisSession(Base):
    __tablename__ = "analysis_sessions"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True)
    timeframe = Column(String)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    
    # Outcome tracking fields
    outcome = Column(String, default="PENDING")
    entry_price = Column(Float, nullable=True)
    target_price = Column(Float, nullable=True)
    stop_price = Column(Float, nullable=True)
    
    # CIO Final Output
    decision = Column(String)
    confidence = Column(Float)
    cio_explanation = Column(String)
    
    # Sub-agent reports (stored as JSON)
    tech_analyst = Column(JSON)
    order_flow_analyst = Column(JSON)
    macro_analyst = Column(JSON)
    news_analyst = Column(JSON)
    devils_advocate = Column(JSON)
    risk_manager = Column(JSON)
    pre_mortem_analyst = Column(JSON, nullable=True)
    
    # Market Context at the time
    market_conditions = Column(JSON)
    
    # Advanced Memory Fields
    regime = Column(String, nullable=True)
    funding = Column(Float, nullable=True)
    oi = Column(Float, nullable=True)
    liquidations = Column(JSON, nullable=True)
    trend = Column(String, nullable=True)
    rr = Column(Float, nullable=True)

    # Feature Reliability Memory booleans
    mtf_correct = Column(Boolean, nullable=True)
    funding_correct = Column(Boolean, nullable=True)
    liquidation_correct = Column(Boolean, nullable=True)
    orderflow_correct = Column(Boolean, nullable=True)
    premortem_correct = Column(Boolean, nullable=True)



class TradeSignal(Base):
    __tablename__ = "trade_signals"
    __table_args__ = (
        Index("ix_trade_signals_symbol_timeframe_status", "symbol", "timeframe", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True, nullable=False)
    timeframe = Column(String, index=True, nullable=False)
    side = Column(String, nullable=False)
    status = Column(String, index=True, nullable=False)
    decision = Column(String, nullable=False)
    confidence = Column(Float, nullable=False)

    entry_low = Column(Float, nullable=False)
    entry_high = Column(Float, nullable=False)
    entry_reference = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=True)
    stop_initial = Column(Float, nullable=False)
    stop_current = Column(Float, nullable=False)
    target_1 = Column(Float, nullable=False)
    target_2 = Column(Float, nullable=False)
    target_3 = Column(Float, nullable=False)
    target_runner = Column(Float, nullable=False)
    target_stage = Column(Integer, default=0, nullable=False)
    risk_per_unit = Column(Float, nullable=False)
    risk_amount_usd = Column(Float, nullable=False)
    notional_usd = Column(Float, nullable=False)
    recommended_leverage = Column(Integer, nullable=False)

    current_price = Column(Float, nullable=True)
    exit_price = Column(Float, nullable=True)
    exit_reason = Column(String, nullable=True)
    entry_timeout_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    published_at = Column(DateTime(timezone=True), server_default=func.now())
    last_evaluated_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    analysis_session_id = Column(Integer, nullable=True)

    events = Column(JSON, default=list)
    context = Column(JSON, default=dict)
    ai_review = Column(JSON, default=dict)


class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True)
    email = Column(String(320), nullable=False, unique=True, index=True)
    password_hash = Column(String(256), nullable=False)
    role = Column(String(32), nullable=False, default="member", index=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_login_at = Column(DateTime(timezone=True), nullable=True)

    subscriptions = relationship("Subscription", back_populates="user", cascade="all, delete-orphan")


class ResearchSlot(Base):
    """A short-lived, server-enforced concurrent live-research lease."""
    __tablename__ = "research_slots"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    symbol = Column(String(32), nullable=False)
    timeframe = Column(String(16), nullable=False)
    channel = Column(String(16), nullable=False)
    acquired_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    heartbeat_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class UserAIConnection(Base):
    """A user's encrypted, provider-scoped AI connection.

    The plaintext key never belongs in this model or in a response payload.
    """
    __tablename__ = "user_ai_connections"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    provider = Column(String(32), nullable=False, default="openrouter")
    encrypted_api_key = Column(String(4096), nullable=False)
    key_suffix = Column(String(12), nullable=False)
    model = Column(String(200), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    plan_code = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False, default="pending", index=True)
    starts_at = Column(DateTime(timezone=True), nullable=True)
    ends_at = Column(DateTime(timezone=True), nullable=True, index=True)
    grace_ends_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="subscriptions")
    payments = relationship("Payment", back_populates="subscription")


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("provider", "provider_payment_id", name="uq_payment_provider_id"),
        UniqueConstraint("order_id", name="uq_payment_order_id"),
    )

    id = Column(String(36), primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    subscription_id = Column(String(36), ForeignKey("subscriptions.id", ondelete="RESTRICT"), nullable=False, index=True)
    provider = Column(String(32), nullable=False)
    provider_invoice_id = Column(String(128), nullable=True, index=True)
    provider_payment_id = Column(String(128), nullable=True)
    order_id = Column(String(128), nullable=False)
    status = Column(String(32), nullable=False, default="waiting", index=True)
    amount = Column(Float, nullable=False)
    currency = Column(String(16), nullable=False)
    pay_currency = Column(String(32), nullable=True)
    pay_amount = Column(String(64), nullable=True)
    payment_address = Column(String(256), nullable=True)
    transaction_hash = Column(String(256), nullable=True, unique=True)
    confirmations = Column(Integer, nullable=False, default=0)
    raw_payload = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    subscription = relationship("Subscription", back_populates="payments")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    event_type = Column(String(64), nullable=False, index=True)
    ip_address = Column(String(64), nullable=True)
    metadata_json = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ScannerConfiguration(Base):
    """The single durable configuration for platform-wide autonomous scans."""
    __tablename__ = "scanner_configuration"

    id = Column(Integer, primary_key=True, default=1)
    enabled = Column(Boolean, nullable=False, default=False)
    discovery = Column(Boolean, nullable=False, default=False)
    watchlist = Column(JSON, nullable=False, default=list)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class RadarSnapshot(Base):
    """One shared, durable Radar snapshot per supported timeframe pair."""
    __tablename__ = "radar_snapshots"

    key = Column(String(32), primary_key=True)
    ltf = Column(String(16), nullable=False)
    htf = Column(String(16), nullable=False)
    payload = Column(JSON, nullable=True)
    captured_at = Column(DateTime(timezone=True), nullable=True, index=True)
    last_requested_at = Column(DateTime(timezone=True), nullable=True, index=True)
    demand_count = Column(Integer, nullable=False, default=0)
    refreshing_until = Column(DateTime(timezone=True), nullable=True, index=True)
    last_error = Column(String(500), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PlatformAIUsage(Base):
    """Conservative, persistent reservation ledger for platform AI spend."""
    __tablename__ = "platform_ai_usage"

    period_key = Column(String(7), primary_key=True)
    reserved_calls = Column(Integer, nullable=False, default=0)
    reserved_cost_usd = Column(Float, nullable=False, default=0.0)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
