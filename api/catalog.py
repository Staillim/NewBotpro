"""FastAPI catalog API – serves JSON endpoints and the static WebApp HTML."""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from config.settings import settings
from database import db_manager as db
from database.models import ContentType

app = FastAPI(title="CineStelar Catalog", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

WEBAPP_DIR = Path(__file__).parent.parent / "webapp"
_PAGE_SIZE = 12


# ── Config ────────────────────────────────────────────────────────────────────

@app.get("/api/config")
async def get_config():
    """Frontend uses this to get the bot username for deep-links."""
    return {"bot_username": settings.BOT_USERNAME}


# ── Movies ────────────────────────────────────────────────────────────────────

@app.get("/api/movies")
async def get_movies(
    page: int = Query(0, ge=0),
    search: str = Query("", max_length=100),
):
    if search.strip():
        items = await db.search_movies(search.strip(), limit=20)
        return {"items": [_movie(m) for m in items], "total": len(items), "pages": 1, "page": 0}

    movies, total = await db.get_movies_page(page, page_size=_PAGE_SIZE)
    return {
        "items": [_movie(m) for m in movies],
        "total": total,
        "pages": max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE),
        "page": page,
    }


# ── Series ────────────────────────────────────────────────────────────────────

@app.get("/api/series")
async def get_series(
    page: int = Query(0, ge=0),
    search: str = Query("", max_length=100),
):
    if search.strip():
        items = await db.search_shows(search.strip(), content_type=ContentType.SERIES, limit=20)
        return {"items": [_show(s) for s in items], "total": len(items), "pages": 1, "page": 0}

    shows, total = await db.get_shows_page(ContentType.SERIES, page, page_size=_PAGE_SIZE)
    return {
        "items": [_show(s) for s in shows],
        "total": total,
        "pages": max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE),
        "page": page,
    }


# ── Anime ─────────────────────────────────────────────────────────────────────

@app.get("/api/anime")
async def get_anime(
    page: int = Query(0, ge=0),
    search: str = Query("", max_length=100),
):
    if search.strip():
        items = await db.search_shows(search.strip(), content_type=ContentType.ANIME, limit=20)
        return {"items": [_show(s) for s in items], "total": len(items), "pages": 1, "page": 0}

    shows, total = await db.get_shows_page(ContentType.ANIME, page, page_size=_PAGE_SIZE)
    return {
        "items": [_show(s) for s in shows],
        "total": total,
        "pages": max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE),
        "page": page,
    }


# ── Detail ────────────────────────────────────────────────────────────────────

@app.get("/api/movie/{movie_id}")
async def movie_detail(movie_id: int):
    m = await db.get_movie(movie_id)
    if not m:
        raise HTTPException(status_code=404, detail="Not found")
    return _movie(m, detail=True)


@app.get("/api/show/{show_id}")
async def show_detail(show_id: int):
    s = await db.get_show(show_id)
    if not s:
        raise HTTPException(status_code=404, detail="Not found")
    return _show(s, detail=True)


# ── Serializers ───────────────────────────────────────────────────────────────

def _movie(m, detail: bool = False) -> dict:
    d = {
        "id": m.id,
        "type": "movie",
        "title": m.title,
        "year": m.year,
        "vote_average": m.vote_average,
        "poster_url": m.poster_url,
        "genres": m.genres,
    }
    if detail:
        d.update({
            "overview": m.overview,
            "backdrop_url": m.backdrop_url,
            "runtime": m.runtime,
            "original_title": m.original_title,
        })
    return d


def _show(s, detail: bool = False) -> dict:
    d = {
        "id": s.id,
        "type": s.content_type.value if s.content_type else "series",
        "title": s.name,
        "year": s.year,
        "vote_average": s.vote_average,
        "poster_url": s.poster_url,
        "genres": s.genres,
        "seasons": s.number_of_seasons,
    }
    if detail:
        d.update({
            "overview": s.overview,
            "backdrop_url": s.backdrop_url,
            "original_title": s.original_name,
        })
    return d


# ── Serve static WebApp – must be the last route ──────────────────────────────

@app.get("/")
async def index():
    index_file = WEBAPP_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="WebApp not built")
    return FileResponse(str(index_file))
