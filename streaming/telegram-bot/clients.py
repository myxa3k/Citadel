"""HTTP clients for the services the bot drives.

Everything the bot does to the outside world goes through here: searching
Prowlarr, handing a torrent to qBittorrent, asking Jellyfin to rescan. Keeping
it in one place means the bot logic stays about conversation flow, and the
retry/timeout rules live next to the calls they protect.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

log = logging.getLogger(__name__)

# Indexers answer at wildly different speeds -- Anime Tosho replies in
# milliseconds, anything behind FlareSolverr can take most of a minute. The
# timeout has to clear the slowest one or those results are silently lost.
SEARCH_TIMEOUT = httpx.Timeout(90.0, connect=10.0)
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


@dataclass(slots=True)
class Release:
    """One search result, trimmed to what the bot actually shows or needs."""

    title: str
    indexer: str
    size: int
    seeders: int
    leechers: int
    download_url: str
    info_url: str
    categories: list[str]

    @property
    def size_gb(self) -> float:
        return self.size / (1024**3)

    @classmethod
    def from_prowlarr(cls, raw: dict[str, Any]) -> "Release":
        # Prowlarr returns either downloadUrl or magnetUrl depending on the
        # indexer; qBittorrent accepts both, so take whichever is present.
        url = raw.get("downloadUrl") or raw.get("magnetUrl") or ""
        return cls(
            title=raw.get("title", "?"),
            indexer=raw.get("indexer", "?"),
            size=raw.get("size") or 0,
            seeders=raw.get("seeders") or 0,
            leechers=raw.get("leechers") or 0,
            download_url=url,
            info_url=raw.get("infoUrl") or "",
            categories=[c.get("name", "") for c in raw.get("categories") or []],
        )


class ProwlarrClient:
    """Search across every configured indexer at once."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base = base_url.rstrip("/") + "/"
        self._headers = {"X-Api-Key": api_key}

    async def search(self, query: str, limit: int = 60) -> list[Release]:
        params = {"query": query, "type": "search", "limit": limit}
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as client:
            resp = await client.get(
                urljoin(self._base, "api/v1/search"),
                params=params,
                headers=self._headers,
            )
            resp.raise_for_status()
            raw = resp.json()

        releases = [Release.from_prowlarr(item) for item in raw]
        # A release with no usable link can't be acted on, and one with no
        # seeders won't finish downloading -- neither belongs in the list the
        # user picks from.
        return [r for r in releases if r.download_url and r.seeders > 0]

    async def search_many(self, queries: list[str], limit: int = 60) -> list[Release]:
        """Search several titles at once and merge the results.

        Trackers index the same show under different names -- the English or
        romaji one on anime trackers, the Russian one on Russian trackers.
        Searching only what the user typed finds only that half, and the
        Russian half is where Russian audio and subtitles are.
        """
        results = await asyncio.gather(
            *(self.search(q, limit=limit) for q in queries),
            return_exceptions=True,
        )

        merged: dict[str, Release] = {}
        for query, result in zip(queries, results):
            if isinstance(result, Exception):
                # One dead indexer or a timeout on one name shouldn't lose
                # the results the other names found.
                log.warning("search failed for %r: %s", query, result)
                continue
            for release in result:
                # The same release comes back under several names. Its
                # download URL is not stable across queries (Prowlarr signs
                # them per request), so identity is title plus size -- two
                # distinct releases never share both.
                key = f"{release.title}|{release.size}"
                merged.setdefault(key, release)

        return list(merged.values())


class QBittorrentClient:
    """Hand a torrent to qBittorrent and report on what it's doing.

    qBittorrent's WebUI authenticates with a cookie, so a session is kept and
    re-established on expiry rather than logging in per call.
    """

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self._base = base_url.rstrip("/") + "/"
        self._username = username
        self._password = password
        self._client: httpx.AsyncClient | None = None

    async def _session(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
            await self._login(self._client)
        return self._client

    async def _login(self, client: httpx.AsyncClient) -> None:
        resp = await client.post(
            urljoin(self._base, "api/v2/auth/login"),
            data={"username": self._username, "password": self._password},
            headers={"Referer": self._base},
        )
        resp.raise_for_status()
        # qBittorrent 4.x answers "Ok."/"Fails."; 5.x answers 204 with an empty
        # body and only sets the session cookie on success. Checking for the
        # cookie covers both, and 5.x returns 204 even for a bad password --
        # so the status code alone proves nothing.
        if not any(name.startswith("QBT_SID") for name in client.cookies.keys()):
            raise RuntimeError("qBittorrent rejected the credentials")

    async def add(self, url: str, category: str, savepath: str | None = None) -> None:
        client = await self._session()
        data = {"urls": url, "category": category}
        if savepath:
            data["savepath"] = savepath

        resp = await client.post(urljoin(self._base, "api/v2/torrents/add"), data=data)
        # A session that outlived its cookie comes back as 403; log in again
        # and retry once before giving up.
        if resp.status_code == 403:
            await self._login(client)
            resp = await client.post(
                urljoin(self._base, "api/v2/torrents/add"), data=data
            )
        resp.raise_for_status()

    async def delete(self, torrent_hash: str, delete_files: bool = True) -> None:
        """Drop a torrent. `delete_files` is on by default because the only
        reason to cancel is that the download is going nowhere -- leaving a
        part-file behind would just waste the disk."""
        client = await self._session()
        data = {
            "hashes": torrent_hash,
            "deleteFiles": "true" if delete_files else "false",
        }
        resp = await client.post(urljoin(self._base, "api/v2/torrents/delete"), data=data)
        if resp.status_code == 403:
            await self._login(client)
            resp = await client.post(
                urljoin(self._base, "api/v2/torrents/delete"), data=data
            )
        resp.raise_for_status()

    async def torrents(self, category: str | None = None) -> list[dict[str, Any]]:
        client = await self._session()
        params = {"category": category} if category else {}
        resp = await client.get(
            urljoin(self._base, "api/v2/torrents/info"), params=params
        )
        if resp.status_code == 403:
            await self._login(client)
            resp = await client.get(
                urljoin(self._base, "api/v2/torrents/info"), params=params
            )
        resp.raise_for_status()
        return resp.json()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class SonarrClient:
    """Registers a show in Sonarr so Bazarr can see it.

    The bot downloads outside Sonarr (it can only search English titles), so
    nothing it fetches exists as far as Sonarr is concerned -- and Bazarr
    takes its library from Sonarr, not from disk. Adding the series after the
    fact is what makes subtitles possible.
    """

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base = base_url.rstrip("/") + "/"
        self._headers = {"X-Api-Key": api_key}

    async def lookup(self, term: str, limit: int = 5) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.get(
                urljoin(self._base, "api/v3/series/lookup"),
                params={"term": term},
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json()[:limit]

    async def existing_tvdb_ids(self) -> set[int]:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.get(
                urljoin(self._base, "api/v3/series"), headers=self._headers
            )
            resp.raise_for_status()
            return {s.get("tvdbId") for s in resp.json() if s.get("tvdbId")}

    async def add_series(
        self,
        series: dict[str, Any],
        root_folder: str,
        quality_profile_id: int,
        season_folder: bool = True,
    ) -> dict[str, Any]:
        """Add a looked-up series, pointed at files that are already on disk.

        `monitor: existing` plus no search means Sonarr adopts what's there
        and watches for future episodes, without immediately trying to
        re-download the season the bot just fetched.
        """
        payload = {
            **series,
            "rootFolderPath": root_folder,
            "qualityProfileId": quality_profile_id,
            "seasonFolder": season_folder,
            "monitored": True,
            "addOptions": {
                "monitor": "existing",
                "searchForMissingEpisodes": False,
                "searchForCutoffUnmetEpisodes": False,
            },
        }
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(
                urljoin(self._base, "api/v3/series"),
                json=payload,
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def rescan(self, series_id: int) -> None:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(
                urljoin(self._base, "api/v3/command"),
                json={"name": "RescanSeries", "seriesId": series_id},
                headers=self._headers,
            )
            resp.raise_for_status()


class JellyfinClient:
    """Trigger a library rescan so a finished download shows up without waiting
    for Jellyfin's own scheduled scan."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base = base_url.rstrip("/") + "/"
        self._headers = {"X-Emby-Token": api_key}

    async def refresh_library(self) -> None:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(
                urljoin(self._base, "Library/Refresh"), headers=self._headers
            )
            resp.raise_for_status()
