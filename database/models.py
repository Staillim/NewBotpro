"""SQLAlchemy models for CineStelar Premium Bot."""

from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Enum,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func

Base = declarative_base()


# ── Enums ─────────────────────────────────────────────────────────────────────


class ContentType(str, PyEnum):
    MOVIE = "movie"
    SERIES = "series"
    ANIME = "anime"


class PlanType(str, PyEnum):
    NONE = "none"
    LITE = "lite"  # $3 – streaming only
    PRO = "pro"    # $5 – streaming + download


class SubStatus(str, PyEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


# ── Users ─────────────────────────────────────────────────────────────────────


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)
    language_code = Column(String(10), nullable=True)
    verified = Column(Boolean, default=False)
    banned = Column(Boolean, default=False)

    # Subscription
    plan = Column(Enum(PlanType), default=PlanType.NONE)
    plan_expires_at = Column(DateTime, nullable=True)
    plan_status = Column(Enum(SubStatus), default=SubStatus.EXPIRED)

    # Referral
    referred_by = Column(BigInteger, nullable=True)

    joined_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_active = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationships
    subscriptions = relationship("Subscription", back_populates="user", lazy="selectin")
    activities = relationship("UserActivity", back_populates="user", lazy="selectin")
    favorites = relationship("Favorite", back_populates="user", lazy="selectin")
    navigation = relationship("UserNavigationState", back_populates="user", uselist=False, lazy="selectin")


# ── Subscriptions ─────────────────────────────────────────────────────────────


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False, index=True)
    plan = Column(Enum(PlanType), nullable=False)
    status = Column(Enum(SubStatus), default=SubStatus.ACTIVE)
    payment_ref = Column(String(255), nullable=True)  # external payment reference
    amount_usd = Column(Float, nullable=True)
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="subscriptions")


# ── Content: Movies ───────────────────────────────────────────────────────────


class Movie(Base):
    __tablename__ = "movies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    file_id = Column(String(255), nullable=False)
    message_id = Column(BigInteger, nullable=True)   # ID in intake channel
    channel_message_id = Column(BigInteger, nullable=True)  # ID in movies channel

    title = Column(String(500), nullable=False, index=True)
    original_title = Column(String(500), nullable=True)
    year = Column(String(10), nullable=True)
    overview = Column(Text, nullable=True)
    poster_url = Column(String(500), nullable=True)
    backdrop_url = Column(String(500), nullable=True)
    vote_average = Column(Float, nullable=True)
    runtime = Column(Integer, nullable=True)
    genres = Column(String(500), nullable=True)
    tmdb_id = Column(Integer, nullable=True, unique=True)

    raw_caption = Column(Text, nullable=True)
    indexed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_movies_indexed_at", "indexed_at"),
        Index("ix_movies_vote_average", "vote_average"),
    )


# ── Content: TV Shows (Series & Anime share this) ────────────────────────────


class TvShow(Base):
    __tablename__ = "tv_shows"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(500), nullable=False, index=True)
    original_name = Column(String(500), nullable=True)
    content_type = Column(Enum(ContentType), default=ContentType.SERIES)  # series | anime
    tmdb_id = Column(Integer, nullable=True, unique=True)
    year = Column(String(20), nullable=True)
    overview = Column(Text, nullable=True)
    poster_url = Column(String(500), nullable=True)
    backdrop_url = Column(String(500), nullable=True)
    vote_average = Column(Float, nullable=True)
    genres = Column(String(500), nullable=True)
    number_of_seasons = Column(Integer, nullable=True)
    status = Column(String(50), nullable=True)
    detected_pattern = Column(String(100), nullable=True)

    indexed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_shows_content_type", "content_type"),
        Index("ix_shows_content_type_indexed", "content_type", "indexed_at"),
        Index("ix_shows_vote_average", "vote_average"),
    )

    episodes = relationship("Episode", back_populates="tv_show", lazy="selectin")


class Episode(Base):
    __tablename__ = "episodes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tv_show_id = Column(Integer, ForeignKey("tv_shows.id"), nullable=False, index=True)
    file_id = Column(String(255), nullable=False)
    message_id = Column(BigInteger, nullable=True)
    channel_message_id = Column(BigInteger, nullable=True)

    season_number = Column(Integer, nullable=False)
    episode_number = Column(Integer, nullable=False)
    title = Column(String(500), nullable=True)
    overview = Column(Text, nullable=True)
    air_date = Column(String(20), nullable=True)
    runtime = Column(Integer, nullable=True)
    still_path = Column(String(500), nullable=True)

    raw_caption = Column(Text, nullable=True)
    indexed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    tv_show = relationship("TvShow", back_populates="episodes")


# ── User Activity ─────────────────────────────────────────────────────────────


class UserActivity(Base):
    __tablename__ = "user_activity"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False, index=True)
    action_type = Column(String(50), nullable=False)  # search, watch_movie, watch_episode, subscribe
    content_id = Column(Integer, nullable=True)
    content_type = Column(String(20), nullable=True)  # movie, series, anime
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="activities")


# ── Favorites ─────────────────────────────────────────────────────────────────


class Favorite(Base):
    __tablename__ = "favorites"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False, index=True)
    content_type = Column(Enum(ContentType), nullable=False)
    content_id = Column(Integer, nullable=False)  # movie.id or tv_show.id
    added_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("user_id", "content_type", "content_id", name="uq_user_fav"),
    )

    user = relationship("User", back_populates="favorites")


# ── Search Log ────────────────────────────────────────────────────────────────


class SearchLog(Base):
    __tablename__ = "search_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    query = Column(String(500), nullable=False)
    results_count = Column(Integer, default=0)
    searched_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# ── Navigation State ─────────────────────────────────────────────────────────


class UserNavigationState(Base):
    __tablename__ = "user_navigation_state"

    user_id = Column(BigInteger, ForeignKey("users.user_id"), primary_key=True)
    current_menu = Column(String(100), nullable=True)
    selected_show_id = Column(Integer, ForeignKey("tv_shows.id"), nullable=True)
    search_query = Column(String(500), nullable=True)
    page = Column(Integer, default=0)
    last_interaction = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="navigation")


# ── Bot Config ────────────────────────────────────────────────────────────────


class BotConfig(Base):
    __tablename__ = "bot_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
