"""Clean raw captions/filenames to extract usable titles."""

import re


# Patterns to remove from filenames/captions
NOISE_PATTERNS = [
    r"\b(1080p|720p|480p|2160p|4k|uhd|hdr|hdr10)\b",
    r"\b(bluray|blu-ray|bdrip|brrip|webrip|web-dl|webdl|hdtv|dvdrip|hdrip)\b",
    r"\b(x264|x265|h264|h265|hevc|avc|10bit)\b",
    r"\b(aac|dts|ac3|atmos|truehd|dd5\.1|flac)\b",
    r"\b(latino|castellano|dual|multi|sub|subs|subtitulado|español|ingles|english)\b",
    r"\b(remux|repack|proper|extended|directors\.cut|unrated|theatrical)\b",
    r"\[.*?\]",      # [anything in brackets]
    r"\(.*?\)",       # (anything in parens) – careful, may remove year
    r"[-_.]",         # separators → spaces
    r"\s{2,}",        # collapse multiple spaces
]

YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

# Episode patterns
EPISODE_PATTERNS = [
    re.compile(r"S(\d{1,2})E(\d{1,3})", re.IGNORECASE),
    re.compile(r"(\d{1,2})x(\d{1,3})", re.IGNORECASE),
    re.compile(r"[Tt]emporada\s*(\d{1,2})\s*[-–]\s*[Cc]ap[ií]tulo\s*(\d{1,3})"),
    re.compile(r"[Ss]eason\s*(\d{1,2})\s*[-–]\s*[Ee]pisode\s*(\d{1,3})"),
    re.compile(r"[Tt](\d{1,2})\s*[Cc]ap\s*(\d{1,3})"),
    re.compile(r"[Ee]p(?:isode|isodio)?\s*(\d{1,3})", re.IGNORECASE),
]


def clean_title(raw: str) -> str:
    """Remove noise from a filename/caption and return a clean title."""
    if not raw:
        return ""
    text = raw.strip()

    # Remove episode info before cleaning
    for pat in EPISODE_PATTERNS:
        text = pat.sub("", text)

    # Remove noise
    for pattern in NOISE_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)

    # Preserve year if present
    year_match = YEAR_RE.search(raw)
    year = year_match.group(0) if year_match else ""

    text = re.sub(r"\s+", " ", text).strip()

    # Remove trailing year from title text (we'll add it back if needed)
    if year and text.endswith(year):
        text = text[: -len(year)].strip()

    return text


def extract_year(raw: str) -> str | None:
    if not raw:
        return None
    m = YEAR_RE.search(raw)
    return m.group(0) if m else None


def extract_episode_info(raw: str) -> dict | None:
    """Extract season/episode from a caption. Returns {season, episode} or None."""
    if not raw:
        return None
    for pat in EPISODE_PATTERNS:
        m = pat.search(raw)
        if m:
            groups = m.groups()
            if len(groups) == 2:
                return {"season": int(groups[0]), "episode": int(groups[1])}
            elif len(groups) == 1:
                return {"season": 1, "episode": int(groups[0])}
    return None
