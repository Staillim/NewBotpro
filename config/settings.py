import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # Bot
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    BOT_USERNAME: str = os.getenv("BOT_USERNAME", "")

    # Admin IDs (comma-separated)
    ADMIN_IDS: list[int] = [
        int(i) for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip()
    ]

    # ── Channels ──
    # Single channel where admin sends ALL content (movies, series, anime)
    INTAKE_CHANNEL_ID: int = int(os.getenv("INTAKE_CHANNEL_ID", "0"))

    # Distribution channels (bot auto-distributes indexed content here)
    MOVIES_CHANNEL_ID: int = int(os.getenv("MOVIES_CHANNEL_ID", "0"))
    SERIES_CHANNEL_ID: int = int(os.getenv("SERIES_CHANNEL_ID", "0"))
    ANIME_CHANNEL_ID: int = int(os.getenv("ANIME_CHANNEL_ID", "0"))

    # Public channel for verification (users must join)
    VERIFICATION_CHANNEL_ID: int = int(os.getenv("VERIFICATION_CHANNEL_ID", "0"))
    VERIFICATION_CHANNEL_USERNAME: str = os.getenv("VERIFICATION_CHANNEL_USERNAME", "")

    # Notification groups
    NOTIFICATION_GROUPS: list[int] = [
        int(i) for i in os.getenv("NOTIFICATION_GROUPS", "").split(",") if i.strip()
    ]

    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///bot.db")

    # Catalog WebApp URL (set to your Render URL after deploy, e.g. https://cinestelar.onrender.com)
    WEBAPP_URL: str = os.getenv("WEBAPP_URL", "")

    # TMDb
    TMDB_API_KEY: str = os.getenv("TMDB_API_KEY", "")

    # Ad system
    LIBTL_ZONE_ID: str = os.getenv("LIBTL_ZONE_ID", "")

    # Subscription prices (reference only – payment handled externally or via Telegram Stars)
    PLAN_LITE_PRICE: float = 3.0  # USD – streaming only
    PLAN_PRO_PRICE: float = 5.0   # USD – streaming + download

    # Telegram Stars prices (XTR) — 1 star ≈ $0.02 USD
    # ── Lite plans ──
    PLAN_LITE_STARS: int = int(os.getenv("PLAN_LITE_STARS", "150"))       # 30d ~$3
    PLAN_LITE_15D_STARS: int = int(os.getenv("PLAN_LITE_15D_STARS", "80")) # 15d ~$1.60
    # ── Pro plans ──
    PLAN_PRO_STARS: int  = int(os.getenv("PLAN_PRO_STARS",  "250"))        # 30d ~$5
    PLAN_PRO_6M_STARS: int  = int(os.getenv("PLAN_PRO_6M_STARS",  "1150")) # 6×250=1500 → ahorra 350⭐  (~23%)
    PLAN_PRO_1Y_STARS: int  = int(os.getenv("PLAN_PRO_1Y_STARS",  "1800")) # 12×250=3000 → ahorra 1200⭐ (~40%)

    # Subscription duration in days per payment
    PLAN_DURATION_DAYS: int = 30

    # Pagination
    CATALOG_PAGE_SIZE: int = 8
    SEARCH_RESULTS_LIMIT: int = 10

    @classmethod
    def is_admin(cls, user_id: int) -> bool:
        return user_id in cls.ADMIN_IDS


settings = Settings()
