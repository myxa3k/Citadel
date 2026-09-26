"""Scoring and labelling of search results.

The point of the bot is that *you* pick the release, so nothing here filters
anything out -- it only decides what order to show things in, and what to say
about each one. The regexes were derived from real result titles off the
indexers actually configured (RuTor, NoNaMe Club, MegaPeer, Anime Tosho),
not from guesswork about how releases are usually named.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from clients import Release

# Russian dub markers. On RuTor/NoNaMe a trailing "| D |" means dubbed,
# "| P |" professional multi-voice, "| L |" amateur. MVO/DVO/AVO are the
# same idea spelled out.
RE_RU_DUB = re.compile(
    r"\|\s*(D|P|MVO|DVO|AVO)\s*(\||$)|\b(MVO|DVO|AVO)\b|\bдубляж\b|\bмноголос",
    re.IGNORECASE,
)
# A Cyrillic word in the title is the single most reliable "this is a Russian
# release" signal -- more so than any tag, which indexers apply inconsistently.
RE_CYRILLIC = re.compile(r"[А-Яа-яЁё]{4,}")
RE_RU_SUB = re.compile(r"\b(rus?sub|русские\s+субтитр|ru\s*sub)\b", re.IGNORECASE)
RE_ENG_DUB = re.compile(r"\b(english\s+dub|eng\s*dub)\b", re.IGNORECASE)
RE_DUAL = re.compile(r"\bdual[\s._-]?audio\b|\bmulti\b", re.IGNORECASE)
RE_UKRAINIAN = re.compile(r"\bUKR\b|\bукраїн", re.IGNORECASE)

# Games and phone rips share the indexers with video and match title searches
# constantly ("Naruto ... PC | RePack-FitGirl"). They are never what's wanted.
RE_JUNK = re.compile(
    r"\bRePack\b|\bFitGirl\b|\bxatab\b|\bChovka\b|\bDLCs?\b|\bPC\s*\||КПК"
    r"|\bDeluxe Edition\b",
    re.IGNORECASE,
)

RE_1080 = re.compile(r"\b1080[pi]\b|\bFullHD\b", re.IGNORECASE)
RE_720 = re.compile(r"\b720[pi]\b", re.IGNORECASE)
RE_2160 = re.compile(r"\b(2160[pi]|4K|UHD)\b", re.IGNORECASE)


@dataclass(slots=True)
class Scored:
    release: Release
    score: int
    labels: list[str]


def score(release: Release) -> Scored:
    """Rank one release. Higher is better; junk goes deeply negative so it
    sinks to the bottom of the list instead of vanishing from it."""
    title = release.title
    points = 0
    labels: list[str] = []

    if RE_JUNK.search(title):
        points -= 10_000
        labels.append("⚠️ не видео")

    if RE_RU_DUB.search(title):
        points += 200
        labels.append("🇷🇺 озвучка")
    if RE_CYRILLIC.search(title):
        points += 150
        if "🇷🇺 озвучка" not in labels:
            labels.append("🇷🇺 рус")
    if RE_RU_SUB.search(title):
        points += 120
        labels.append("💬 рус.суб")
    if RE_DUAL.search(title):
        points += 60
        labels.append("🎧 dual")
    if RE_ENG_DUB.search(title):
        points += 20
        labels.append("🇬🇧 eng")
    # Ukrainian releases match the Cyrillic bonus but aren't what's wanted --
    # cancel it out rather than banning them outright.
    if RE_UKRAINIAN.search(title):
        points -= 180
        labels.append("🇺🇦 укр")

    if RE_2160.search(title):
        points += 40
        labels.append("4K")
    elif RE_1080.search(title):
        points += 50
        labels.append("1080p")
    elif RE_720.search(title):
        points += 10
        labels.append("720p")

    # Seeders decide whether a download finishes at all, but shouldn't
    # outrank language: capped so a well-seeded English rip can't bury a
    # Russian one that has fewer peers.
    points += min(release.seeders, 50)

    # Below a couple of seeders the download realistically won't finish --
    # a perfectly-labelled Russian release with nobody sharing it is worse
    # than an English one that actually arrives, so it sinks rather than
    # sitting mid-list looking like a reasonable pick.
    if release.seeders == 0:
        points -= 500
        labels.append("💀 нет сидов")
    elif release.seeders <= 2:
        points -= 100
        labels.append("⚠️ мало сидов")

    return Scored(release=release, score=points, labels=labels)


def rank(releases: list[Release]) -> list[Scored]:
    return sorted(
        (score(r) for r in releases), key=lambda s: (-s.score, -s.release.seeders)
    )
