"""Classify content as Movie, Series, or Anime based on caption + TMDb data."""

import re
import logging

from database.models import ContentType
from utils import tmdb_api
from utils.title_cleaner import extract_episode_info

logger = logging.getLogger(__name__)

# Keywords that strongly suggest anime (in caption or channel)
ANIME_KEYWORDS = re.compile(
    r"\b(anime|ova|ona|especial\s*anime|sub\s*esp|latino\s*anime|animes|donghua)\b",
    re.IGNORECASE,
)

SERIES_KEYWORDS = re.compile(
    r"\b(temporada|season|serie|episode|episodio|cap[ií]tulo|chapter|S\d{1,2}E\d{1,3}|\d{1,2}x\d{1,3})\b",
    re.IGNORECASE,
)


async def classify(caption: str, tmdb_id: int = None) -> ContentType:
    """
    Determine the content type from a caption and optional TMDb ID.

    Priority:
    1. If caption contains anime keywords → ANIME
    2. If TMDb ID given and is_anime() → ANIME
    3. If caption has episode patterns or series keywords → SERIES
    4. Else → MOVIE
    """
    if not caption:
        return ContentType.MOVIE

    # 1. Check caption keywords for anime
    if ANIME_KEYWORDS.search(caption):
        return ContentType.ANIME

    # 2. Check TMDb for anime
    if tmdb_id:
        try:
            if await tmdb_api.is_anime(tmdb_id):
                return ContentType.ANIME
        except Exception:
            pass

    # 3. Check for series patterns
    ep_info = extract_episode_info(caption)
    if ep_info or SERIES_KEYWORDS.search(caption):
        return ContentType.SERIES

    # 4. Default to movie
    return ContentType.MOVIE
