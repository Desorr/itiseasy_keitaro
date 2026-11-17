from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import Settings


class Base(DeclarativeBase):
    pass


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    alias: Mapped[str] = mapped_column(String(64), unique=True)
    state: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class Offer(Base):
    __tablename__ = "offers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(1024))


class Stream(Base):
    __tablename__ = "streams"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    name: Mapped[str] = mapped_column(String(1024))
    position: Mapped[int] = mapped_column(Integer)
    schema: Mapped[str] = mapped_column(String(32))
    missing: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed: Mapped[dict[str, Any]] = mapped_column(JSONB)
    draft: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    conflict: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    publication: Mapped[str] = mapped_column(String(32), default="idle")
    pending: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    publishing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StreamOffer(Base):
    __tablename__ = "stream_offers"
    __table_args__ = (UniqueConstraint("stream_id", "offer_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    stream_id: Mapped[int] = mapped_column(ForeignKey("streams.id"), index=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"))
    position: Mapped[int] = mapped_column(Integer)
    share: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32), default="active")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    removed: Mapped[bool] = mapped_column(Boolean, default=False)


def connect_database(settings: Settings):
    engine = create_async_engine(settings.database_url.get_secret_value(), pool_pre_ping=True, hide_parameters=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)
