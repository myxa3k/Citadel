"""Finds every name a show is known by, so one search covers all trackers.

The problem this solves: trackers index the same anime under different
titles. English/anime trackers use the romaji or English name, Russian
trackers use the Russian one -- "Yani Neko" on Anime Tosho is "Табакошка" on
RuTor. Searching one name finds one half of what exists, and the Russian
half is where Russian audio and subtitles live.

Two sources, because neither alone is enough:
  * Shikimori  -- a Russian anime database; the only reliable source of the
                  Russian title.
  * AniList    -- romaji/English/native plus synonyms, better coverage of
                  non-Russian names.

Both are best-effort: if one is down or knows nothing, whatever the other
returns is still used, and the user's original query is always searched.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(15.0, connect=8.0)
USER_AGENT = "CitadelStreamingBot/1.0"

ANILIST_QUERY = """
query ($search: String) {
  Page(perPage: 3) {
    media(search: $search, type: ANIME) {
      title { romaji english native }
      synonyms
    }
  }
}
"""


async def _shikimori(query: str) -> list[str]:
    """Russian title (and the romaji it's filed under)."""
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(
            "https://shikimori.one/api/animes",
            params={"search": query, "limit": 3},
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        found: list[str] = []
        for anime in resp.json():
            found.extend(
                v for v in (anime.get("name"), anime.get("russian")) if v
            )
        return found


async def _anilist(query: str) -> list[str]:
    """Romaji, English, native and whatever synonyms are on record."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            "https://graphql.anilist.co",
            json={"query": ANILIST_QUERY, "variables": {"search": query}},
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        media = resp.json()["data"]["Page"]["media"]
        found: list[str] = []
        for item in media:
            title = item.get("title") or {}
            found.extend(v for v in title.values() if v)
            found.extend(item.get("synonyms") or [])
        return found


def _worth_searching(name: str) -> bool:
    """Drop titles in scripts none of the configured trackers index.

    Japanese, Chinese, Thai and Korean names return nothing from RuTor,
    NoNaMe or Anime Tosho -- searching them only costs time. Latin and
    Cyrillic are the two that pay off.
    """
    if len(name) < 3:
        return False
    return bool(re.search(r"[A-Za-zА-Яа-яЁё]", name)) and not re.search(
        r"[぀-ヿ一-鿿฀-๿가-힯]", name
    )


def _dedupe(names: list[str]) -> list[str]:
    """Collapse names that differ only in punctuation or case."""
    seen: dict[str, str] = {}
    for name in names:
        key = re.sub(r"[^\w]+", "", name.lower())
        if key and key not in seen:
            seen[key] = name.strip()
    return list(seen.values())


async def alternative_titles(query: str, limit: int = 4) -> list[str]:
    """Names to search for, the user's own query always first.

    Capped deliberately: every extra name is another full sweep of five
    indexers, and past three or four the additions are increasingly obscure
    romanisations that match nothing.
    """
    results = await asyncio.gather(
        _shikimori(query), _anilist(query), return_exceptions=True
    )

    names = [query]
    for result in results:
        if isinstance(result, Exception):
            log.warning("title lookup failed: %s", result)
            continue
        names.extend(result)

    usable = [n for n in _dedupe(names) if _worth_searching(n)]
    # Keep the original query even if the filter would have dropped it --
    # the user knows what they typed.
    if query not in usable:
        usable.insert(0, query)
    return usable[:limit]
