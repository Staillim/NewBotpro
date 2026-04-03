"""Database manager – async CRUD operations."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from database.models import (
    Base,
    BotConfig,
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

engine = create_async_engine(settings.DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ── helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


# ── Users ─────────────────────────────────────────────────────────────────────

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


# ── Subscriptions ─────────────────────────────────────────────────────────────

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
    if user.plan_expires_at and user.plan_expires_at.replace(tzinfo=timezone.utc) < _now():
        # Expired – update
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


# ── Movies ────────────────────────────────────────────────────────────────────

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


# ── TV Shows / Anime ─────────────────────────────────────────────────────────

async def add_tv_show(**kwargs) -> TvShow:
    async with async_session() as s:
        show = TvShow(**kwargs)
        s.add(show)
        await s.commit()
        await s.refresh(show)
        return show


async def search_shows(query: str, content_type: ContentType = None, limit: int = 10) -> list[TvShow]:
    async with async_session() as s:
        q = query.lower()
        stmt = select(TvShow).where(func.lower(TvShow.name).contains(q))
        if content_type:
            stmt = stmt.where(TvShow.content_type == content_type)
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
            select(func.count(TvShow.id)).where(TvShow.content_type == content_type)
        )
        total = total_r.scalar() or 0
        result = await s.execute(
            select(TvShow)
            .where(TvShow.content_type == content_type)
            .order_by(TvShow.indexed_at.desc())
            .offset(page * page_size)
            .limit(page_size)
        )
        return list(result.scalars().all()), total


async def get_total_shows(content_type: ContentType) -> int:
    async with async_session() as s:
        r = await s.execute(
            select(func.count(TvShow.id)).where(TvShow.content_type == content_type)
        )
        return r.scalar() or 0


# ── Episodes ──────────────────────────────────────────────────────────────────

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


# ── Favorites ─────────────────────────────────────────────────────────────────

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


# ── Activity ──────────────────────────────────────────────────────────────────

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


# ── Search Log ────────────────────────────────────────────────────────────────

async def log_search(user_id: int, query: str, results_count: int):
    async with async_session() as s:
        s.add(SearchLog(user_id=user_id, query=query, results_count=results_count))
        await s.commit()


# ── Bot Config ────────────────────────────────────────────────────────────────

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
