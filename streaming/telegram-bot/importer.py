"""Moves finished downloads into the library Jellyfin actually reads.

qBittorrent drops files in /data/downloads/<kind>/, Jellyfin only looks at
/data/media/<kind>/. Sonarr normally bridges that gap, but it never sees these
downloads -- the bot hands torrents straight to qBittorrent, because Sonarr
can only search the English title and Russian trackers index the Russian one.

So the bridge is here instead. Files are **hardlinked**, not copied: same
filesystem, so the library entry appears instantly, costs no extra space, and
the original stays in place for seeding.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

VIDEO_SUFFIXES = {".mkv", ".mp4", ".avi", ".m4v", ".ts"}
# Russian releases very often ship subtitles as separate files next to the
# video rather than muxed in. Leaving them behind is how a download that
# *did* come with Russian subs ends up looking like one that didn't.
SUBTITLE_SUFFIXES = {".ass", ".srt", ".ssa", ".sub", ".vtt"}
# Below this, a file is a sample, a trailer, or a stray extra rather than an
# episode worth putting in the library.
MIN_VIDEO_BYTES = 50 * 1024 * 1024

# Episode number as release groups actually write it, most specific first.
# "S01E05" and "- 05 -" are unambiguous; a bare leading "05." is the common
# fallback in Russian anime packs.
EPISODE_PATTERNS = [
    re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})"),
    re.compile(r"(?:^|[\s._\-\[])(\d{1,3})[\s._\-]*(?:of|из|/)[\s._\-]*\d{1,3}", re.I),
    re.compile(r"[\s._\-]\-[\s._]*(\d{1,3})[\s._\-]"),
    # "Show.E07", no season given.
    re.compile(r"(?:^|[\s._\-])[Ee](\d{1,3})(?:[\s._\-]|$)"),
    # "Show 12 серия" / "12 серия" -- how Russian fansub packs are named.
    re.compile(r"(\d{1,3})\s*(?:серия|серии|эпизод)", re.I),
    re.compile(r"^(\d{1,3})[.\s_\-]"),
    # Number just before the extension: "[Group] Show 02.srt". Last resort,
    # since any number in the name could match -- but subtitle files are
    # usually named exactly this way.
    re.compile(r"[\s._\-](\d{1,3})\.[A-Za-z0-9]{2,4}$"),
]

# Noise that sits between the title and the technical tags. Cutting at the
# first of these keeps "Youjo Senki II" out of
# "Youjo Senki II - ZaLmanVsk (WEB-DL 1080p)".
TITLE_STOP = re.compile(
    r"[\s._\-]*(?:\(|\[|\d{3,4}[pi]\b|WEB[\s._-]?DL|WEBRip|BDRip|BluRay|HDTV"
    r"|x26[45]|HEVC|AVC|S\d{2}E\d{2}|S\d{2}\b)",
    re.IGNORECASE,
)

# Release groups are appended as " - Group" after the title. There's no
# general rule that separates a group name from a subtitle ("Blue Exorcist -
# Kyoto Saga" is part of the title), so only a trailing single token that
# looks like a handle -- no spaces, mixed case or all caps -- is dropped.
RE_TRAILING_GROUP = re.compile(r"[\s._]*-[\s._]*([A-Za-z][A-Za-z0-9]{2,})$")
COMMON_WORDS = {
    "the", "movie", "season", "part", "saga", "arc", "final",
    "special", "tv", "ova", "ona", "film",
}


@dataclass(slots=True)
class ImportResult:
    title: str
    destination: Path
    linked: int
    skipped: int


def clean_title(raw: str) -> str:
    """Turn a release folder name into something usable as a library folder."""
    # Fansub groups prefix their name in brackets ("[smol] YuruYuri ..."), and
    # leaving it in means Jellyfin and Bazarr both fail to identify the show.
    # Only square brackets, and only when something follows: "(500) Days of
    # Summer" starts with a parenthesised number that is part of the title.
    raw = re.sub(r"^\s*\[[^\]]{1,30}\]\s*(?=\S)", "", raw)

    # Russian releases are often "Русское / English / Original (year) tags" --
    # the Latin part matches metadata providers far better than the Cyrillic.
    parts = [p.strip() for p in raw.split("/") if p.strip()]
    if len(parts) > 1:
        latin = [p for p in parts if re.search(r"[A-Za-z]{3,}", p)]
        raw = (latin or parts)[0]

    cut = TITLE_STOP.search(raw)
    if cut and cut.start() > 0:
        raw = raw[: cut.start()]

    title = re.sub(r"[\s._]+", " ", raw).strip(" -_.")

    group = RE_TRAILING_GROUP.search(title)
    if group:
        word = group.group(1)
        # A real subtitle word ("- Final", "- Movie") stays; a handle like
        # "ZaLmanVsk" or "VARYG" goes. Mixed case or all caps distinguishes
        # them well enough in practice.
        looks_like_handle = word.lower() not in COMMON_WORDS and (
            word.isupper() or (word != word.lower() and word != word.capitalize())
        )
        if looks_like_handle:
            title = title[: group.start()].strip(" -_.")

    # A colon reads fine on disk but some players mangle it; a dash keeps the
    # phrase legible where a bare removal would run words together.
    title = title.replace(":", " -")
    title = re.sub(r"\s{2,}", " ", title).strip(" -_.")
    return re.sub(r'[<>"/\\|?*]', "-", title) or "Unknown"


def episode_number(name: str) -> tuple[int, int] | None:
    """Return (season, episode) if the filename states one."""
    for pattern in EPISODE_PATTERNS:
        m = pattern.search(name)
        if not m:
            continue
        groups = m.groups()
        if len(groups) == 2:
            return int(groups[0]), int(groups[1])
        return 1, int(groups[0])
    return None


def _subtitle_files(source: Path) -> list[Path]:
    if source.is_file():
        return []
    return sorted(
        p
        for p in source.rglob("*")
        if p.is_file() and p.suffix.lower() in SUBTITLE_SUFFIXES
    )


def _link_subtitles(
    subtitles: list[Path], video_target: Path, video_source: Path
) -> int:
    """Link subtitles belonging to one episode next to its video file.

    Jellyfin matches a subtitle to a video by filename stem, with the
    language as the suffix before the extension -- `Show - S01E01.ru.ass`
    attaches to `Show - S01E01.mkv` and shows up as Russian.
    """
    linked = 0
    for subtitle in subtitles:
        # Same stem as the video it shipped with, ignoring any language part
        # the release already added.
        if subtitle.stem.split(".")[0] != video_source.stem.split(".")[0]:
            continue

        lang = _subtitle_language(subtitle)
        suffix = f".{lang}{subtitle.suffix}" if lang else subtitle.suffix
        target = video_target.with_suffix("")
        target = target.with_name(target.name + suffix)
        if target.exists():
            continue
        try:
            target.hardlink_to(subtitle)
            linked += 1
        except OSError:
            log.exception("could not link subtitle %s", subtitle)
    return linked


def _subtitle_language(path: Path) -> str | None:
    """Guess the language tag for a subtitle file.

    A tag already in the name wins. Otherwise the file is sampled: Cyrillic
    in the dialogue means Russian, which is the case worth getting right --
    releases routinely ship a Russian .ass with no language in its name, and
    Jellyfin would otherwise label it "Unknown".
    """
    parts = path.stem.lower().split(".")
    for part in parts[1:]:
        if part in {"ru", "rus", "russian"}:
            return "ru"
        if part in {"en", "eng", "english"}:
            return "en"

    try:
        text = path.read_bytes().decode("utf-8", "ignore")
    except OSError:
        return None

    # Sample the actual dialogue, not the head of the file. An .ass can open
    # with megabytes of styling and karaoke timing before the first line of
    # speech -- in one real release the first Cyrillic character sat at byte
    # 2.8M of a 2.9M file, so reading a prefix reports the wrong language.
    spoken = "\n".join(
        line.split(",", 9)[-1]
        for line in text.splitlines()
        if line.startswith(("Dialogue:", "Comment:"))
    )
    sample = spoken or text

    cyrillic = len(re.findall(r"[А-Яа-яЁё]", sample))
    latin = len(re.findall(r"[A-Za-z]", sample))
    # Russian subtitles still carry Latin characters in typesetting tags and
    # signs, so compare counts instead of taking whichever appears first.
    if cyrillic > 20 and cyrillic > latin * 0.2:
        return "ru"
    if latin > 20:
        return "en"
    return None


def attach_subtitles(
    subtitles: list[Path], show_dir: Path, language: str | None = None
) -> tuple[int, int]:
    """Place loose subtitle files next to the episodes they belong to.

    For subtitles fetched by hand -- the case where no Russian release of a
    show exists and the video came from an English one. Each file is matched
    to an episode by the number in its name, then linked beside that episode
    as `<Episode>.<lang>.ass`, the shape Jellyfin reads as a selectable
    track.

    Returns (attached, unmatched).
    """
    episodes: dict[tuple[int, int], Path] = {}
    for video in show_dir.rglob("*"):
        if not video.is_file() or video.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        parsed = episode_number(video.name)
        if parsed:
            episodes[parsed] = video

    attached = unmatched = 0
    for subtitle in subtitles:
        parsed = episode_number(subtitle.name)
        # A season pack of subtitles usually numbers episodes without a
        # season, while the video may sit in "Season 01" -- try both.
        video = episodes.get(parsed) if parsed else None
        if video is None and parsed:
            video = next(
                (v for (_, ep), v in episodes.items() if ep == parsed[1]), None
            )
        if video is None:
            unmatched += 1
            continue

        lang = language or _subtitle_language(subtitle) or "ru"
        target = video.with_suffix("")
        target = target.with_name(f"{target.name}.{lang}{subtitle.suffix}")
        try:
            if target.exists():
                target.unlink()
            shutil.copy2(subtitle, target)
            attached += 1
        except OSError:
            log.exception("could not place subtitle %s", subtitle)
            unmatched += 1

    return attached, unmatched


def _video_files(source: Path) -> list[Path]:
    if source.is_file():
        return [source] if source.suffix.lower() in VIDEO_SUFFIXES else []
    return sorted(
        p
        for p in source.rglob("*")
        if p.is_file()
        and p.suffix.lower() in VIDEO_SUFFIXES
        and p.stat().st_size >= MIN_VIDEO_BYTES
    )


def import_download(
    source: Path, media_root: Path, kind: str, release_name: str
) -> ImportResult:
    """Hardlink one finished download into the library.

    Films go in as `<Title>/<Title>.mkv`; anything with episode numbering gets
    `<Title>/Season NN/<Title> - SNNENN.mkv`, which is the layout Jellyfin
    parses without help.
    """
    title = clean_title(release_name)
    files = _video_files(source)
    if not files:
        return ImportResult(title, media_root, 0, 0)

    subtitles = _subtitle_files(source)
    linked = skipped = 0

    if kind == "movies":
        destination = media_root / title
        destination.mkdir(parents=True, exist_ok=True)
        # A film release is one feature plus extras; the largest file is it.
        main = max(files, key=lambda f: f.stat().st_size)
        target = destination / f"{title}{main.suffix}"
        if target.exists():
            skipped += 1
        else:
            target.hardlink_to(main)
            linked += 1
        linked += _link_subtitles(subtitles, target, main)
        return ImportResult(title, destination, linked, skipped)

    show_root = media_root / title
    seasons: set[int] = set()

    for f in files:
        parsed = episode_number(f.name) or episode_number(f.parent.name)
        if parsed is None:
            # Unnumbered: keep it rather than drop it, and let Jellyfin sort
            # out what it is.
            destination = show_root
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / f.name
        else:
            season, episode = parsed
            seasons.add(season)
            destination = show_root / f"Season {season:02d}"
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / f"{title} - S{season:02d}E{episode:02d}{f.suffix}"

        if target.exists():
            skipped += 1
        else:
            try:
                target.hardlink_to(f)
                linked += 1
            except OSError:
                # Different filesystem (or a permissions problem) -- a copy
                # would silently double disk use, so report instead of hiding it.
                log.exception("could not hardlink %s -> %s", f, target)
                skipped += 1
                continue

        # Subtitles are linked even when the video was already there, so an
        # episode imported before this feature existed still picks them up.
        linked += _link_subtitles(subtitles, target, f)

    return ImportResult(title, show_root, linked, skipped)
