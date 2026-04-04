"""Database manager — async CRUD operations."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select, update, delete, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

logger = logging.getLogger(__name__)

from database.models import (
    Base,
    BotConfig,
    BotGroup,
    ContentType,
    Episode,
    Favorite,
    Movie,
    PlanType,
    SearchLog,
    SubStatus,
    Subscription,
    TvShow,
    User,
    UserActivity,
    UserNavigationState,
)
from config.settings import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_size=10,          # mÃ¡ximo 10 conexiones abiertas
    max_overflow=20,       # hasta 20 extras en picos (total 30 max)
    pool_timeout=30,       # espera 30s antes de dar error
    pool_recycle=1800,     # recicla conexiones cada 30 min
    pool_pre_ping=True,    # verifica conexiÃ³n antes de usarla
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Migrations: add columns that may not exist in older databases
    # Uses IF NOT EXISTS for PostgreSQL (avoids silent failures)
    is_pg = "postgresql" in settings.DATABASE_URL or "postgres" in settings.DATABASE_URL
    if is_pg:
        migrations = [
            "ALTER TABLE tv_shows ADD COLUMN IF NOT EXISTS published BOOLEAN NOT NULL DEFAULT FALSE",
            "UPDATE tv_shows SET published = TRUE WHERE published IS NULL OR published = FALSE",
            "ALTER TABLE tv_shows ADD COLUMN IF NOT EXISTS detected_pattern VARCHAR(100)",
            "ALTER TABLE tv_shows ADD COLUMN IF NOT EXISTS indexed_at TIMESTAMP DEFAULT NOW()",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS joined_at TIMESTAMP DEFAULT NOW()",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMP DEFAULT NOW()",
            "ALTER TABLE movies ADD COLUMN IF NOT EXISTS indexed_at TIMESTAMP DEFAULT NOW()",
            "ALTER TABLE episodes ADD COLUMN IF NOT EXISTS indexed_at TIMESTAMP DEFAULT NOW()",
        ]
    else:
        migrations = [
            "ALTER TABLE tv_shows ADD COLUMN published BOOLEAN NOT NULL DEFAULT 0",
            "UPDATE tv_shows SET published = 1",
            "ALTER TABLE tv_shows ADD COLUMN detected_pattern VARCHAR(100)",
            "ALTER TABLE tv_shows ADD COLUMN indexed_at TIMESTAMP",
            "ALTER TABLE users ADD COLUMN joined_at TIMESTAMP",
            "ALTER TABLE users ADD COLUMN last_active TIMESTAMP",
            "ALTER TABLE movies ADD COLUMN indexed_at TIMESTAMP",
            "ALTER TABLE episodes ADD COLUMN indexed_at TIMESTAMP",
        ]
    for sql in migrations:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
            logger.info("Migration OK: %s", sql[:60])
        except Exception as e:
            logger.debug("Migration skipped (%s): %s", type(e).__name__, sql[:60])


# â”€â”€ helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _now():
    return datetime.utcnow()


# â”€â”€ Users â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def get_or_create_user(user_id: int, username: str = None,
                              first_name: str = None, last_name: str = None,
                              language_code: str = None, referred_by: int = None) -> User:
    async with async_session() as s:
        result = await s.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one_or_none()
        if user:
            user.last_active = _now()
            if username:
                user.username = username
            if first_name:
                user.first_name = first_name
            await s.commit()
            return user
        user = User(
            user_id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            language_code=language_code,
            referred_by=referred_by,
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return user


async def get_user(user_id: int) -> Optional[User]:
    async with async_session() as s:
        result = await s.execute(select(User).where(User.user_id == user_id))
        return result.scalar_one_or_none()


async def set_user_verified(user_id: int, verified: bool = True):
    async with async_session() as s:
        await s.execute(
            update(User).where(User.user_id == user_id).values(verified=verified)
        )
        await s.commit()


async def set_user_banned(user_id: int, banned: bool = True):
    async with async_session() as s:
        await s.execute(
            update(User).where(User.user_id == user_id).values(banned=banned)
        )
        await s.commit()


async def get_all_user_ids() -> list[int]:
    async with async_session() as s:
        result = await s.execute(select(User.user_id).where(User.banned == False))
        return [r[0] for r in result.all()]


async def get_total_users() -> int:
    async with async_session() as s:
        result = await s.execute(select(func.count(User.id)))
        return result.scalar() or 0


async def get_active_subscribers() -> int:
    async with async_session() as s:
        result = await s.execute(
            select(func.count(User.id)).where(
                User.plan != PlanType.NONE,
                User.plan_status == SubStatus.ACTIVE,
            )
        )
        return result.scalar() or 0


async def get_new_users_count(days: int = 7) -> int:
    async with async_session() as s:
        since = datetime.utcnow() - timedelta(days=days)
        result = await s.execute(
            select(func.count(User.id)).where(User.joined_at >= since)
        )
        return result.scalar() or 0


async def get_subscribers_by_plan() -> dict:
    async with async_session() as s:
        result = await s.execute(
            select(User.plan, func.count(User.id))
            .where(User.plan != PlanType.NONE, User.plan_status == SubStatus.ACTIVE)
            .group_by(User.plan)
        )
        counts = {str(row[0].value): row[1] for row in result.all()}
        return counts


async def get_new_content_count(days: int = 7) -> dict:
    async with async_session() as s:
        since = datetime.utcnow() - timedelta(days=days)
        movies_r = await s.execute(
            select(func.count(Movie.id)).where(Movie.indexed_at >= since)
        )
        shows_r = await s.execute(
            select(func.count(TvShow.id)).where(TvShow.indexed_at >= since)
        )
        return {
            "movies": movies_r.scalar() or 0,
            "shows": shows_r.scalar() or 0,
        }


# â”€â”€ Subscriptions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def activate_plan(user_id: int, plan: PlanType, days: int = 30,
                         payment_ref: str = None) -> Subscription:
    """Activate a subscription plan for a user."""
    async with async_session() as s:
        now = _now()
        expires = now + timedelta(days=days)

        # Update user record
        await s.execute(
            update(User).where(User.user_id == user_id).values(
                plan=plan,
                plan_expires_at=expires,
                plan_status=SubStatus.ACTIVE,
            )
        )

        amount = settings.PLAN_LITE_PRICE if plan == PlanType.LITE else settings.PLAN_PRO_PRICE
        sub = Subscription(
            user_id=user_id,
            plan=plan,
            status=SubStatus.ACTIVE,
            payment_ref=payment_ref,
            amount_usd=amount,
            started_at=now,
            expires_at=expires,
        )
        s.add(sub)
        await s.commit()
        await s.refresh(sub)
        return sub


async def check_subscription(user_id: int) -> tuple[bool, PlanType]:
    """Return (is_active, plan_type)."""
    user = await get_user(user_id)
    if not user or user.plan == PlanType.NONE:
        return False, PlanType.NONE
    if user.plan_expires_at and user.plan_expires_at < _now():
        # Expired â€“ update
        async with async_session() as s:
            await s.execute(
                update(User).where(User.user_id == user_id).values(
                    plan_status=SubStatus.EXPIRED
                )
            )
            await s.commit()
        return False, PlanType.NONE
    return True, user.plan


async def cancel_plan(user_id: int):
    async with async_session() as s:
        await s.execute(
            update(User).where(User.user_id == user_id).values(
                plan=PlanType.NONE,
                plan_status=SubStatus.CANCELLED,
            )
        )
        await s.commit()


# â”€â”€ Movies â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def add_movie(**kwargs) -> Movie:
    async with async_session() as s:
        movie = Movie(**kwargs)
        s.add(movie)
        await s.commit()
        await s.refresh(movie)
        return movie


async def search_movies(query: str, limit: int = 10) -> list[Movie]:
    async with async_session() as s:
        q = query.lower()
        result = await s.execute(
            select(Movie)
            .where(func.lower(Movie.title).contains(q))
            .order_by(Movie.vote_average.desc().nullslast())
            .limit(limit)
        )
        return list(result.scalars().all())


async def get_movie(movie_id: int) -> Optional[Movie]:
    async with async_session() as s:
        result = await s.execute(select(Movie).where(Movie.id == movie_id))
        return result.scalar_one_or_none()


async def get_movies_page(page: int, page_size: int = 8) -> tuple[list[Movie], int]:
    async with async_session() as s:
        total_r = await s.execute(select(func.count(Movie.id)))
        total = total_r.scalar() or 0
        result = await s.execute(
            select(Movie)
            .order_by(Movie.indexed_at.desc())
            .offset(page * page_size)
            .limit(page_size)
        )
        return list(result.scalars().all()), total


async def get_total_movies() -> int:
    async with async_session() as s:
        r = await s.execute(select(func.count(Movie.id)))
        return r.scalar() or 0


async def delete_movie(movie_id: int) -> bool:
    """Delete a movie by ID. Returns True if it existed."""
    async with async_session() as s:
        result = await s.execute(delete(Movie).where(Movie.id == movie_id))
        await s.commit()
        return result.rowcount > 0


# â”€â”€ TV Shows / Anime â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def add_tv_show(**kwargs) -> TvShow:
    from sqlalchemy.exc import IntegrityError
    async with async_session() as s:
        show = TvShow(**kwargs)
        s.add(show)
        try:
            await s.commit()
            await s.refresh(show)
            return show
        except IntegrityError:
            await s.rollback()
            # tmdb_id unique conflict — fetch the existing row
            tmdb_id = kwargs.get("tmdb_id")
            if tmdb_id:
                result = await s.execute(select(TvShow).where(TvShow.tmdb_id == tmdb_id))
                existing = result.scalar_one_or_none()
                if existing:
                    return existing
            # fallback: insert without tmdb_id
            kwargs["tmdb_id"] = None
            show2 = TvShow(**kwargs)
            s.add(show2)
            await s.commit()
            await s.refresh(show2)
            return show2


async def publish_show(show_id: int) -> None:
    async with async_session() as s:
        await s.execute(
            update(TvShow).where(TvShow.id == show_id).values(published=True)
        )
        await s.commit()


async def search_shows(query: str, content_type: ContentType = None, limit: int = 10, published_only: bool = True) -> list[TvShow]:
    async with async_session() as s:
        q = query.lower()
        stmt = select(TvShow).where(func.lower(TvShow.name).contains(q))
        if content_type:
            stmt = stmt.where(TvShow.content_type == content_type)
        if published_only:
            stmt = stmt.where(TvShow.published == True)  # noqa: E712
        stmt = stmt.order_by(TvShow.vote_average.desc().nullslast()).limit(limit)
        result = await s.execute(stmt)
        return list(result.scalars().all())


async def get_show(show_id: int) -> Optional[TvShow]:
    async with async_session() as s:
        result = await s.execute(select(TvShow).where(TvShow.id == show_id))
        return result.scalar_one_or_none()


async def get_shows_page(content_type: ContentType, page: int, page_size: int = 8) -> tuple[list[TvShow], int]:
    async with async_session() as s:
        total_r = await s.execute(
            select(func.count(TvShow.id)).where(
                TvShow.content_type == content_type, TvShow.published == True  # noqa: E712
            )
        )
        total = total_r.scalar() or 0
        result = await s.execute(
            select(TvShow)
            .where(TvShow.content_type == content_type, TvShow.published == True)  # noqa: E712
            .order_by(TvShow.indexed_at.desc())
            .offset(page * page_size)
            .limit(page_size)
        )
        return list(result.scalars().all()), total


async def get_total_shows(content_type: ContentType) -> int:
    async with async_session() as s:
        r = await s.execute(
            select(func.count(TvShow.id)).where(
                TvShow.content_type == content_type, TvShow.published == True  # noqa: E712
            )
        )
        return r.scalar() or 0


async def delete_show(show_id: int) -> bool:
    """Delete a show and all its episodes. Returns True if the show existed."""
    async with async_session() as s:
        await s.execute(delete(Episode).where(Episode.tv_show_id == show_id))
        result = await s.execute(delete(TvShow).where(TvShow.id == show_id))
        await s.commit()
        return result.rowcount > 0


async def clear_all_content() -> dict:
    """Delete ALL movies, episodes, shows and favorites. Returns counts."""
    async with async_session() as s:
        fav = await s.execute(delete(Favorite))
        ep = await s.execute(delete(Episode))
        sh = await s.execute(delete(TvShow))
        mv = await s.execute(delete(Movie))
        sl = await s.execute(delete(SearchLog))
        await s.commit()
        return {
            "movies": mv.rowcount,
            "episodes": ep.rowcount,
            "shows": sh.rowcount,
            "favorites": fav.rowcount,
            "search_logs": sl.rowcount,
        }


async def search_movies(query: str, limit: int = 10) -> list[Movie]:
    """Search movies by title (case-insensitive)."""
    async with async_session() as s:
        result = await s.execute(
            select(Movie)
            .where(Movie.title.ilike(f"%{query}%"))
            .order_by(Movie.title)
            .limit(limit)
        )
        return list(result.scalars().all())


# â”€â”€ Episodes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def add_episode(**kwargs) -> Episode:
    async with async_session() as s:
        ep = Episode(**kwargs)
        s.add(ep)
        await s.commit()
        await s.refresh(ep)
        return ep


async def get_seasons(show_id: int) -> list[int]:
    async with async_session() as s:
        result = await s.execute(
            select(Episode.season_number)
            .where(Episode.tv_show_id == show_id)
            .distinct()
            .order_by(Episode.season_number)
        )
        return [r[0] for r in result.all()]


async def get_episodes(show_id: int, season: int) -> list[Episode]:
    async with async_session() as s:
        result = await s.execute(
            select(Episode)
            .where(Episode.tv_show_id == show_id, Episode.season_number == season)
            .order_by(Episode.episode_number)
        )
        return list(result.scalars().all())


async def get_episode(episode_id: int) -> Optional[Episode]:
    async with async_session() as s:
        result = await s.execute(select(Episode).where(Episode.id == episode_id))
        return result.scalar_one_or_none()


async def get_last_episode_number(show_id: int, season: int) -> int:
    """Return the highest episode_number for a given show+season, or 0 if none."""
    async with async_session() as s:
        result = await s.execute(
            select(func.max(Episode.episode_number))
            .where(Episode.tv_show_id == show_id, Episode.season_number == season)
        )
        return result.scalar() or 0


# â”€â”€ Favorites â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def add_favorite(user_id: int, content_type: ContentType, content_id: int):
    async with async_session() as s:
        fav = Favorite(user_id=user_id, content_type=content_type, content_id=content_id)
        s.add(fav)
        try:
            await s.commit()
        except Exception:
            await s.rollback()  # duplicate


async def remove_favorite(user_id: int, content_type: ContentType, content_id: int):
    async with async_session() as s:
        await s.execute(
            delete(Favorite).where(
                Favorite.user_id == user_id,
                Favorite.content_type == content_type,
                Favorite.content_id == content_id,
            )
        )
        await s.commit()


async def get_favorites(user_id: int) -> list[Favorite]:
    async with async_session() as s:
        result = await s.execute(
            select(Favorite).where(Favorite.user_id == user_id).order_by(Favorite.added_at.desc())
        )
        return list(result.scalars().all())


# â”€â”€ Activity â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def log_activity(user_id: int, action_type: str, content_id: int = None,
                        content_type: str = None):
    async with async_session() as s:
        act = UserActivity(
            user_id=user_id,
            action_type=action_type,
            content_id=content_id,
            content_type=content_type,
        )
        s.add(act)
        await s.commit()


# â”€â”€ Search Log â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def log_search(user_id: int, query: str, results_count: int):
    async with async_session() as s:
        s.add(SearchLog(user_id=user_id, query=query, results_count=results_count))
        await s.commit()


# â”€â”€ Bot Config â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def get_config(key: str, default: str = None) -> Optional[str]:
    async with async_session() as s:
        result = await s.execute(select(BotConfig).where(BotConfig.key == key))
        cfg = result.scalar_one_or_none()
        return cfg.value if cfg else default


async def set_config(key: str, value: str):
    async with async_session() as s:
        result = await s.execute(select(BotConfig).where(BotConfig.key == key))
        cfg = result.scalar_one_or_none()
        if cfg:
            cfg.value = value
            cfg.updated_at = _now()
        else:
            s.add(BotConfig(key=key, value=value))
        await s.commit()


# ── Bot Groups ────────────────────────────────────────────────────────────────

async def register_group(chat_id: int, title: str = None) -> None:
    """Upsert a group where the bot is active."""
    async with async_session() as s:
        result = await s.execute(select(BotGroup).where(BotGroup.chat_id == chat_id))
        group = result.scalar_one_or_none()
        if group:
            group.active = True
            group.updated_at = _now()
            if title:
                group.title = title
        else:
            s.add(BotGroup(chat_id=chat_id, title=title))
        await s.commit()


async def remove_group(chat_id: int) -> None:
    """Mark a group as inactive (bot was removed)."""
    async with async_session() as s:
        result = await s.execute(select(BotGroup).where(BotGroup.chat_id == chat_id))
        group = result.scalar_one_or_none()
        if group:
            group.active = False
            group.updated_at = _now()
            await s.commit()


async def get_active_groups() -> list[int]:
    """Return chat_ids of all active groups."""
    async with async_session() as s:
        result = await s.execute(
            select(BotGroup.chat_id).where(BotGroup.active == True)  # noqa: E712
        )
        return [row[0] for row in result.all()]


# ── Genres ────────────────────────────────────────────────────────────────────

async def get_all_genres() -> list[str]:
    """Return distinct genre names from movies + shows, sorted."""
    async with async_session() as s:
        mov_r = await s.execute(select(Movie.genres).where(Movie.genres.isnot(None)))
        show_r = await s.execute(
            select(TvShow.genres).where(
                TvShow.genres.isnot(None), TvShow.published == True  # noqa: E712
            )
        )
        genre_set: set[str] = set()
        for row in mov_r.all():
            for g in row[0].split(","):
                g = g.strip()
                if g:
                    genre_set.add(g)
        for row in show_r.all():
            for g in row[0].split(","):
                g = g.strip()
                if g:
                    genre_set.add(g)
        return sorted(genre_set)


async def get_items_by_genre(genre: str, page: int = 0, page_size: int = 20) -> tuple[list, int]:
    """Return movies + shows matching a genre, newest first."""
    pattern = f"%{genre}%"
    async with async_session() as s:
        # Movies matching genre
        mov_r = await s.execute(
            select(Movie).where(Movie.genres.ilike(pattern))
            .order_by(Movie.indexed_at.desc())
        )
        movies = list(mov_r.scalars().all())

        # Published shows matching genre
        show_r = await s.execute(
            select(TvShow).where(
                TvShow.genres.ilike(pattern),
                TvShow.published == True,  # noqa: E712
            )
            .order_by(TvShow.indexed_at.desc())
        )
        shows = list(show_r.scalars().all())

        combined = sorted(
            [("movie", m) for m in movies] + [("show", s_) for s_ in shows],
            key=lambda x: (x[1].indexed_at or _now()),
            reverse=True,
        )
        total = len(combined)
        page_items = combined[page * page_size : (page + 1) * page_size]
        return page_items, total
