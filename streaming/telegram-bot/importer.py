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

# A season stated in the title rather than in the episode numbering. Every
# tracker writes it differently, and each spelling that survives into the
# folder name splits one show into two -- "Yuru Yuri" and "Yuru Yuri ss2"
# become separate entries in Jellyfin even though they are one series.
#
# Matching these lets the season be lifted out of the title and applied to
# the episodes instead, which is where Jellyfin expects to find it.
SEASON_MARKERS = [
    # "2nd Season", "3rd season"
    re.compile(r"[\s._\-]+(\d{1,2})(?:st|nd|rd|th)[\s._\-]*season\b", re.I),
    # "Season 2", "Сезон 2"
    re.compile(r"[\s._\-]+(?:season|сезон)[\s._\-]*(\d{1,2})\b", re.I),
    # "ss2", "S2" as a standalone token -- not "S02E01", which the episode
    # patterns own, and not a trailing letter of a word.
    re.compile(r"(?![\s._\-]+[Ss]\d{1,2}[Ee]\d)[\s._\-]+(?:ss|s)(\d{1,2})\b", re.I),
    # A bare trailing number: "Yuru Yuri 2". Last, and deliberately narrow --
    # a number is only a season when nothing else follows it.
    re.compile(r"[\s._\-]+(\d{1,2})$"),
]


# "[TV-2]", "[ТВ-3]" -- RuTracker's way of marking a sequel season. Only
# inside brackets, where the tag actually lives; a loose "TV 2" in a title is
# more likely part of the name.
RE_BRACKET_SEASON = re.compile(r"\[\s*(?:TV|ТВ)[\s._\-]*(\d{1,2})\s*\]", re.I)


def split_season(raw: str) -> tuple[str, int | None]:
    """Separate a season stated in the title from the title itself.

    Returns the title with the season marker removed, and the season number
    if one was found. `"Yuru Yuri ss2"` becomes `("Yuru Yuri", 2)`, so both
    it and plain `"Yuru Yuri"` land in one library folder with the episodes
    filed under the right season.
    """
    for pattern in SEASON_MARKERS:
        m = pattern.search(raw)
        if not m:
            continue
        season = int(m.group(1))
        # Season 0 isn't a thing, and a wildly high number is a year or a
        # resolution that slipped through rather than a season.
        if not 1 <= season <= 40:
            continue
        stripped = (raw[: m.start()] + raw[m.end() :]).strip(" -_.")
        # Only if something is left: "S2" alone is not a title.
        if stripped:
            return stripped, season
    return raw, None


def match_key(title: str) -> str:
    """Normalise a title down to what makes two folders the same show.

    Spacing, punctuation and case all vary between releases of one series --
    "YuruYuri", "Yuru Yuri" and "Yuru-Yuri" are the same thing. Reducing to
    letters and digits is what lets the importer notice a show already has a
    folder instead of making a second one beside it.
    """
    return re.sub(r"[^a-z0-9а-яё]", "", title.lower())


@dataclass(slots=True)
class ImportResult:
    title: str
    destination: Path
    linked: int
    skipped: int


def clean_title(raw: str) -> str:
    """Turn a release folder name into something usable as a library folder.

    The season, if the title states one, is *not* part of the result -- see
    `title_and_season` for that. One show is one folder.
    """
    return title_and_season(raw)[0]


def title_and_season(raw: str) -> tuple[str, int | None]:
    """`clean_title`, but also reporting the season the title claimed."""
    # RuTracker states a sequel season as a bracketed "[TV-2]" tag, which sits
    # in the technical section the title is about to be cut at. Read it before
    # that happens -- otherwise the season is simply lost and season 2 files
    # itself on top of season 1.
    bracketed = RE_BRACKET_SEASON.search(raw)
    tagged_season = int(bracketed.group(1)) if bracketed else None

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

    # A season written into the title belongs on the episodes, not on the
    # folder -- otherwise each season of one show becomes its own entry.
    title, season = split_season(title)
    if season is None:
        season = tagged_season

    # A colon reads fine on disk but some players mangle it; a dash keeps the
    # phrase legible where a bare removal would run words together.
    title = title.replace(":", " -")
    title = re.sub(r"\s{2,}", " ", title).strip(" -_.")
    return re.sub(r'[<>"/\\|?*]', "-", title) or "Unknown", season


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


def suggest_merges(media_root: Path) -> list[tuple[str, list[str]]]:
    """Folders in one library that look like seasons of the same show.

    Buying each season as its own tracker release is the normal way to get an
    anime that ran over several years, and it leaves the library with one
    folder per release. This finds the sets worth collapsing: folders whose
    names agree on a prefix long enough to be a title rather than a
    coincidence.

    Returns (suggested parent title, folder names) for each group, so the
    caller can confirm before anything moves.
    """
    try:
        raw = [p.name for p in media_root.iterdir() if p.is_dir()]
    except OSError:
        return []

    # Sorted by the normalised key, not the raw name, so the bare title always
    # comes before the titles built on top of it. Sorting by raw name puts
    # "YuruYuri" *after* "Yuru Yuri San Hai" -- a space sorts before a letter
    # -- and the shortest name is then never the one that gets to collect the
    # others, which is how a real group silently went unfound.
    names = sorted(raw, key=lambda n: (match_key(n), n))

    groups: list[tuple[str, list[str]]] = []
    used: set[str] = set()

    for i, name in enumerate(names):
        if name in used:
            continue
        base = match_key(name)
        if len(base) < 4:
            # Too short to distinguish a real shared title from two unrelated
            # shows that happen to start alike.
            continue

        members = [name]
        for other in names[i + 1 :]:
            if other in used:
                continue
            key = match_key(other)
            # One is a prefix of the other: "yuruyuri" against
            # "yuruyurisanhai". That's the shape a sequel's title takes --
            # the original plus a subtitle.
            if key.startswith(base) and len(key) > len(base):
                members.append(other)

        if len(members) > 1:
            used.update(members)
            # The shortest *key* is the bare series title; the others are it
            # plus a season subtitle. Measured on the key so that spacing
            # doesn't decide which folder name the merged show ends up under.
            groups.append((min(members, key=lambda n: len(match_key(n))), members))

    return groups


def merge_shows(
    media_root: Path, parent: str, folders: list[str], seasons: dict[str, int]
) -> tuple[int, list[str]]:
    """Fold several show folders into one, each as its own season.

    Files are *moved*, not copied -- they are hardlinks into the download, so
    moving one keeps the same inode and the torrent keeps seeding from it
    untouched. Nothing is deleted except the emptied folders.

    Returns (files moved, problems).
    """
    target_root = media_root / parent
    target_root.mkdir(parents=True, exist_ok=True)
    moved = 0
    problems: list[str] = []

    for folder in folders:
        source = media_root / folder
        season = seasons.get(folder, 1)
        if not source.is_dir():
            continue

        for f in sorted(source.rglob("*")):
            if not f.is_file():
                continue

            parsed = episode_number(f.name) or episode_number(f.parent.name)
            episode = parsed[1] if parsed else None

            destination = target_root / f"Season {season:02d}"
            destination.mkdir(parents=True, exist_ok=True)

            if episode is None:
                # No number to file it under; keep the original name so
                # nothing is silently lost.
                target = destination / f.name
            else:
                # Subtitles carry a language part before the extension that
                # has to survive the rename, or Jellyfin stops matching them.
                suffix = f.suffix
                if suffix.lower() in SUBTITLE_SUFFIXES:
                    lang = f.stem.rsplit(".", 1)
                    if len(lang) == 2 and 1 <= len(lang[1]) <= 8:
                        suffix = f".{lang[1]}{suffix}"
                target = destination / f"{parent} - S{season:02d}E{episode:02d}{suffix}"

            if target.exists():
                continue
            try:
                f.rename(target)
                moved += 1
            except OSError as exc:
                problems.append(f"{f.name}: {exc}")

        if source == target_root:
            continue
        # Only the now-empty shell goes. Anything left behind means a file
        # didn't move, and deleting the folder then would lose it.
        if any(p.is_file() for p in source.rglob("*")):
            problems.append(f"{folder}: files left behind, folder kept")
            continue
        try:
            shutil.rmtree(source)
        except OSError as exc:
            problems.append(f"{folder}: {exc}")

    return moved, problems


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


def existing_folder(media_root: Path, title: str) -> str | None:
    """The library folder already holding this show, if there is one.

    Matching ignores spacing and punctuation, so a release that cleans up as
    "YuruYuri" joins the "Yuru Yuri" folder rather than starting a rival one
    next to it.
    """
    key = match_key(title)
    if not key:
        return None
    try:
        entries = sorted(p.name for p in media_root.iterdir() if p.is_dir())
    except OSError:
        return None
    for name in entries:
        if match_key(name) == key:
            return name
    return None


def import_download(
    source: Path, media_root: Path, kind: str, release_name: str
) -> ImportResult:
    """Hardlink one finished download into the library.

    Films go in as `<Title>/<Title>.mkv`; anything with episode numbering gets
    `<Title>/Season NN/<Title> - SNNENN.mkv`, which is the layout Jellyfin
    parses without help.

    A season named in the release title ("... ss2") is applied to the
    episodes, and an existing folder for the same show is reused, so seasons
    downloaded separately still end up as one series.
    """
    title, stated_season = title_and_season(release_name)
    if kind != "movies":
        # Reuse the spelling already on disk, so the folder doesn't fork over
        # a difference in spacing.
        title = existing_folder(media_root, title) or title
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
        # "Yuru Yuri ss2 - 05.mkv" numbers the episode but not the season;
        # the season came from the release title, so put it back.
        if parsed is not None and stated_season is not None and parsed[0] == 1:
            parsed = (stated_season, parsed[1])
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
