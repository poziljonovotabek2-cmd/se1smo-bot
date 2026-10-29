from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, Text, func, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from app.config import settings

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    language: Mapped[str] = mapped_column(String(2), default="ru")
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    place: Mapped[str | None] = mapped_column(String(160), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    subscribed: Mapped[bool] = mapped_column(Boolean, default=False)
    has_started: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    min_magnitude: Mapped[float] = mapped_column(Float, default=3.5)
    max_distance_km: Mapped[int] = mapped_column(Integer, default=500)
    quiet_hours: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FeedCache(Base):
    __tablename__ = "feed_cache"
    cache_key: Mapped[str] = mapped_column(String(180), primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SeenEvent(Base):
    __tablename__ = "seen_events"
    event_key: Mapped[str] = mapped_column(String(220), primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivered: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", index=True)


class PendingCheck(Base):
    __tablename__ = "pending_checks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    since: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    notified_event: Mapped[str | None] = mapped_column(String(220), nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all does not alter tables already created by earlier bot versions.
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS min_magnitude DOUBLE PRECISION NOT NULL DEFAULT 3.5"))
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS max_distance_km INTEGER NOT NULL DEFAULT 500"))
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS quiet_hours BOOLEAN NOT NULL DEFAULT FALSE"))
        # Existing users have already used the bot; only future first-time users should see onboarding.
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS has_started BOOLEAN NOT NULL DEFAULT TRUE"))
        await conn.execute(text("ALTER TABLE feed_cache ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"))
        # Old rows were inserted only after deciding to suppress duplicate alerts, so treat them as delivered.
        await conn.execute(text("ALTER TABLE seen_events ADD COLUMN IF NOT EXISTS delivered BOOLEAN NOT NULL DEFAULT TRUE"))
