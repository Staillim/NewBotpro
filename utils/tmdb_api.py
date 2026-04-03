"""TMDb API wrapper for movies and TV shows."""

import logging
from typing import Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.themoviedb.org/3"
IMG_BASE = "https://image.tmdb.org/t/p"


async def _get(endpoint: str, params: dict = None) -> Optional[dict]:
    params = params or {}
    params["api_key"] = settings.TMDB_API_KEY
    params.setdefault("language", "es-MX")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{BASE_URL}{endpoint}", params=params)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.error("TMDb request failed: %s", e)
        return None


# ── Movies ────────────────────────────────────────────────────────────────────

async def search_movie(query: str, year: str = None) -> list[dict]:
    params = {"query": query}
    if year:
        params["year"] = year
    data = await _get("/search/movie", params)
    if not data:
        return []
    results = []
    for item in data.get("results", [])[:5]:
        results.append(_parse_movie(item))
    return results


async def get_movie_details(tmdb_id: int) -> Optional[dict]:
    data = await _get(f"/movie/{tmdb_id}")
    return _parse_movie(data) if data else None


def _parse_movie(item: dict) -> dict:
    return {
        "tmdb_id": item.get("id"),
        "title": item.get("title", ""),
        "original_title": item.get("original_title", ""),
        "year": (item.get("release_date") or "")[:4],
        "overview": item.get("overview", ""),
        "poster_url": f"{IMG_BASE}/w500{item['poster_path']}" if item.get("poster_path") else None,
        "backdrop_url": f"{IMG_BASE}/w1280{item['backdrop_path']}" if item.get("backdrop_path") else None,
        "vote_average": item.get("vote_average", 0),
        "runtime": item.get("runtime"),
        "genres": ", ".join(g["name"] for g in item.get("genres", [])) if "genres" in item else "",
    }


# ── TV Shows / Anime ─────────────────────────────────────────────────────────

async def search_tv(query: str) -> list[dict]:
    data = await _get("/search/tv", {"query": query})
    if not data:
        return []
    results = []
    for item in data.get("results", [])[:5]:
        results.append(_parse_tv(item))
    return results


async def get_tv_details(tmdb_id: int) -> Optional[dict]:
    data = await _get(f"/tv/{tmdb_id}")
    return _parse_tv(data) if data else None


async def get_episode_details(tmdb_id: int, season: int, episode: int) -> Optional[dict]:
    data = await _get(f"/tv/{tmdb_id}/season/{season}/episode/{episode}")
    if not data:
        return None
    return {
        "title": data.get("name", ""),
        "overview": data.get("overview", ""),
        "air_date": data.get("air_date", ""),
        "runtime": data.get("runtime"),
        "still_path": f"{IMG_BASE}/w500{data['still_path']}" if data.get("still_path") else None,
    }


def _parse_tv(item: dict) -> dict:
    first_air = (item.get("first_air_date") or "")[:4]
    last_air = (item.get("last_air_date") or "")[:4]
    year = f"{first_air}-{last_air}" if last_air and last_air != first_air else first_air
    return {
        "tmdb_id": item.get("id"),
        "name": item.get("name", ""),
        "original_name": item.get("original_name", ""),
        "year": year,
        "overview": item.get("overview", ""),
        "poster_url": f"{IMG_BASE}/w500{item['poster_path']}" if item.get("poster_path") else None,
        "backdrop_url": f"{IMG_BASE}/w1280{item['backdrop_path']}" if item.get("backdrop_path") else None,
        "vote_average": item.get("vote_average", 0),
        "genres": ", ".join(g["name"] for g in item.get("genres", [])) if "genres" in item else "",
        "number_of_seasons": item.get("number_of_seasons"),
        "status": item.get("status", ""),
    }


# ── Detect if anime ──────────────────────────────────────────────────────────

ANIME_GENRE_IDS = {16}  # Animation
ANIME_ORIGIN = {"JP", "ja"}

async def is_anime(tmdb_id: int) -> bool:
    """Heuristic: Japanese origin + Animation genre = Anime."""
    data = await _get(f"/tv/{tmdb_id}")
    if not data:
        return False
    genre_ids = {g.get("id") for g in data.get("genres", [])}
    origin_countries = set(data.get("origin_country", []))
    original_language = data.get("original_language", "")
    has_animation = bool(genre_ids & ANIME_GENRE_IDS)
    is_japanese = bool(origin_countries & {"JP"}) or original_language == "ja"
    return has_animation and is_japanese
