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
# Specials are exempt from that floor, and need one of their own. A shorts
# series runs three to five minutes an episode -- Re:Zero's "Break Time" and
# "Re Puchi" are around 40 MB each -- so the ordinary threshold discarded
# twenty of twenty-five without a word. They are still real episodes.
MIN_SPECIAL_BYTES = 2 * 1024 * 1024

# Extras that ship inside a season pack and are not episodes: creditless
# openings and endings, adverts, previews, art galleries, menus.
#
# These matter more than their size suggests. "Yuru Yuri TV 2 Art Design 1"
# parses as episode 1 and "CM 2" as episode 2, so without this they overwrite
# the real first and second episodes in the library -- which is precisely how
# one series ended up as three broken entries in Jellyfin. A creditless
# opening is also often a full-size 1080p file, so a size threshold alone
# never catches them.
EXTRA_MARKERS = re.compile(
    r"(?:^|[\s._\-\[(])(?:"
    r"NC(?:OP|ED)|(?:OP|ED)\s*\d*(?:v\d)?|Creditless"
    r"|CM|PV|SPOT|Trailer|Teaser|Promo|Preview"
    r"|Art[\s._\-]?Design|Menu|Logo|Interview|Making|Web[\s._\-]?Preview"
    r"|Clean[\s._\-]?(?:Opening|Ending)"
    r")(?:[\s._\-\])]|\d|$)",
    re.IGNORECASE,
)


def is_extra(name: str) -> bool:
    """Whether a filename is a bonus feature rather than an episode."""
    return EXTRA_MARKERS.search(Path(name).stem) is not None


# A special: a real episode, but one that belongs in Season 00 rather than
# numbered among the season it shipped with. Russian packs write it inline,
# keeping the absolute position in the bracket:
#
#   [07] Yuru Yuri TV 1 Sp 1 серия.mkv
#
# which parses as "episode 1" -- the same slot as the actual first episode.
# Whichever sorts first wins and the other is dropped, so the special was
# silently lost. Recognising it sends it to Season 00 instead, where it has
# its own numbering and collides with nothing.
SPECIAL_MARKERS = re.compile(
    r"(?:^|[\s._\-\[(])(?:"
    r"Sp(?:ecial)?s?|SP|OVA|ONA|OAD|Спешл|Спецвыпуск"
    r")(?:[\s._\-\])]|\d|$)",
    re.IGNORECASE,
)


# Folder names a release uses for its specials. A pack advertised as
# "TV-1 + SP + MV" keeps them in a subfolder, and the files inside are not
# always marked in their own names -- the folder is the only signal.
SPECIAL_DIRS = {
    "sp", "specials", "special", "ova", "ovas", "oad", "extra episodes",
    "сп", "спешлы", "спецвыпуски",
}


def is_special(name: str, path: Path | None = None) -> bool:
    """Whether a file is a special/OVA rather than a numbered episode.

    `path`, when given, is checked for a specials subfolder as well, since a
    release that bundles seasons and specials together often marks only the
    folder.
    """
    stem = Path(name).stem
    if is_extra(stem):
        return False
    if SPECIAL_MARKERS.search(stem) is not None:
        return True
    if path is not None:
        return any(part.strip().lower() in SPECIAL_DIRS for part in path.parts)
    return False


def _relative_to(f: Path, base: Path) -> Path | None:
    """`f` relative to `base`, or None when it isn't underneath it.

    A single-file download has the file itself as the source path, so there
    is no folder structure to read a specials marker from.
    """
    try:
        return f.relative_to(base)
    except ValueError:
        return None


def _same_file(a: Path, b: Path) -> bool:
    """Whether two paths are the same data -- a hardlink of each other.

    Re-running an import must not treat the entry it made last time as a
    rival file needing a new number, or every run would add another copy.
    """
    try:
        sa, sb = a.stat(), b.stat()
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


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
    # "Yuru Yuri TV 2" -- the unbracketed form, which is what the folder name
    # actually looks like once a RuTracker release has been unpacked. Either
    # at the end, or followed by what else the pack contains: a release
    # labelled "TV-1 + SP + MV" is season 1 plus its specials.
    re.compile(
        r"[\s._\-]+(?:TV|ТВ)[\s._\-]*(\d{1,2})(?=$|[\s._\-]*\+)", re.I
    ),
    # A bare trailing number: "Yuru Yuri 2". Last, and deliberately narrow --
    # a number is only a season when nothing else follows it.
    re.compile(r"[\s._\-]+(\d{1,2})$"),
]


# "[TV-2]", "(ТВ-3)" -- how a sequel season is marked on RuTracker, in either
# kind of bracket. The real torrent names use the round form and put it right
# after the Russian title:
#
#   Свободу Лесбиянкам (ТВ-2) / Yuru Yuri / Yuruyuri / ... [TV] [12 из 12]
#
# Matching only the square form meant every season of a show parsed as
# season None, so all of them were filed as Season 01 and each import
# overwrote the last -- 12 episodes where there should have been 36.
#
# Bracketed only, still: a loose "TV 2" mid-title is more likely part of the
# name, and the unbracketed form is handled by SEASON_MARKERS where it is
# anchored to the end.
RE_BRACKET_SEASON = re.compile(
    r"[\[(]\s*(?:TV|ТВ)[\s._\-]*(\d{1,2})\s*[\])]", re.I
)


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
        rest = raw[m.end() :]
        # "TV-1 + SP + MV" lists what else is in the pack, not more title.
        # Everything from the "+" onwards belongs to the season marker.
        rest = re.sub(r"^\s*\+.*$", "", rest)
        stripped = (raw[: m.start()] + rest).strip(" -_.")
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


def title_candidates(raw: str) -> list[str]:
    """Every Latin title a release name offers, best first.

    RuTracker lists a show's names slash-separated, and for a sequel the
    first is the season's own title while a later one is the bare series:

        Свободу Лесбиянкам (ТВ-3) / Yuru Yuri San Hai / Yuruyuri / YRYR

    "Yuru Yuri San Hai" is the better label, but "Yuruyuri" is what matches
    the folder the first two seasons already went into. Handing back both
    lets the importer prefer whichever one the library already knows.
    """
    head = RE_BRACKET_SEASON.sub(" ", raw)

    # A Russian-titled release often carries the Latin name in brackets:
    #
    #   Жизнь в альтернативном мире с нуля [Re Zero kara Hajimeru ...] TV-1
    #
    # That bracket is the only thing tying it to the "Re Zero" folder the
    # other seasons live in, so it is a candidate rather than noise. Taken
    # before the leading-bracket strip below, which would discard it.
    bracketed = [
        b.strip()
        for b in re.findall(r"\[([^\]]{4,80})\]", head)
        if re.search(r"[A-Za-z]{3,}", b)
    ]

    head = re.sub(r"^\s*\[[^\]]{1,30}\]\s*(?=\S)", "", head)

    seen: list[str] = []
    for part in head.split("/") + bracketed:
        part = part.strip()
        if not re.search(r"[A-Za-z]{3,}", part):
            continue
        cut = TITLE_STOP.search(part)
        if cut and cut.start() > 0:
            part = part[: cut.start()]
        cleaned = re.sub(r"[\s._]+", " ", part).strip(" -_.")
        cleaned = re.sub(r'[<>"/\\|?*]', "-", cleaned.replace(":", " -"))
        # Acronyms like "YRYR" are a search alias, never a folder name.
        if len(cleaned) >= 4 and cleaned not in seen and not cleaned.isupper():
            seen.append(cleaned)
    return seen


def title_and_season(raw: str) -> tuple[str, int | None]:
    """`clean_title`, but also reporting the season the title claimed."""
    # RuTracker states a sequel season as a bracketed "[TV-2]" tag, which sits
    # in the technical section the title is about to be cut at. Read it before
    # that happens -- otherwise the season is simply lost and season 2 files
    # itself on top of season 1.
    bracketed = RE_BRACKET_SEASON.search(raw)
    tagged_season = int(bracketed.group(1)) if bracketed else None

    # The unbracketed "TV-1" form sits past the title too, after the Latin
    # name in brackets, so it has to be read from the whole string as well:
    # cutting the title at the first "[" happens below and would hide it.
    if tagged_season is None:
        _, tagged_season = split_season(raw)

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



# The suffix a merge gives a second copy of an episode. Recognised on the way
# back in so re-merging leaves it alone instead of bumping it to alt2.
RE_ALT_VERSION = re.compile(r" - alt\d+$")


@dataclass(slots=True)
class MergeResult:
    moved: int
    #: Files already in place -- the same data, nothing to do.
    already_there: int
    #: Episodes that arrived twice from different releases, kept as "alt"
    #: versions beside the original rather than dropped.
    duplicates: list[str]
    problems: list[str]


def merge_shows(
    media_root: Path,
    parent: str,
    folders: list[str],
    roles: dict[str, tuple[str, int]],
) -> MergeResult:
    """Fold several show folders into one, each in the role it was given.

    `roles` maps a folder to ("season", N), ("specials", 0) or
    ("extras", 0) -- the three places Jellyfin reads differently. Specials
    become Season 00, where OVAs and shorts belong; extras go to `extras/`,
    which Jellyfin shows as bonus features and never numbers as episodes.

    Files are *moved*, not copied -- they are hardlinks into the download, so
    moving one keeps the same inode and the torrent keeps seeding from it
    untouched. Nothing is deleted except the emptied folders.

    Returns (files moved, problems).
    """
    target_root = media_root / parent
    target_root.mkdir(parents=True, exist_ok=True)
    moved = 0
    skipped_same = 0
    duplicates: list[str] = []
    problems: list[str] = []

    for folder in folders:
        source = media_root / folder
        role, number = roles.get(folder, ("season", 1))
        # The show being merged *into* keeps what it already has. Walking its
        # own files and refiling them under the role it was given moved every
        # episode of Season 01 into Season 02 -- the same inodes appearing
        # twice, one series looking like it had fifty episodes.
        if source == target_root:
            continue
        if not source.is_dir():
            continue

        for f in sorted(source.rglob("*")):
            if not f.is_file():
                continue

            # An alternate version placed by an earlier merge is already
            # where it belongs. Re-processing it would renumber it to alt2,
            # then alt3, one step further on every run.
            if RE_ALT_VERSION.search(f.stem):
                skipped_same += 1
                continue

            # A bonus feature keeps its own name wherever it lands: numbering
            # it as an episode is exactly the mistake that made one series
            # look like three.
            if role == "extras" or is_extra(f.name):
                destination = target_root / "extras"
                destination.mkdir(parents=True, exist_ok=True)
                target = destination / f.name
            else:
                # A specials/OVA file inside a season folder is still a
                # special. The role describes the folder, but a release that
                # bundles "TV-1 + SP" puts both in one -- taking the role at
                # face value filed five-minute shorts as episodes of the
                # season, numbered over the real ones.
                is_sp = is_special(f.name, f.relative_to(source))
                season = 0 if role == "specials" or is_sp else number
                parsed = episode_number(f.name) or episode_number(f.parent.name)
                episode = parsed[1] if parsed else None

                destination = target_root / f"Season {season:02d}"
                destination.mkdir(parents=True, exist_ok=True)

                if episode is None:
                    # No number to file it under; keep the original name so
                    # nothing is silently lost.
                    target = destination / f.name
                else:
                    # Subtitles carry a language part before the extension
                    # that has to survive the rename, or Jellyfin stops
                    # matching them to the video.
                    suffix = f.suffix
                    if suffix.lower() in SUBTITLE_SUFFIXES:
                        lang = f.stem.rsplit(".", 1)
                        if len(lang) == 2 and 1 <= len(lang[1]) <= 8:
                            suffix = f".{lang[1]}{suffix}"
                    target = (
                        destination
                        / f"{parent} - S{season:02d}E{episode:02d}{suffix}"
                    )
                    if target.exists() and not _same_file(target, f):
                        if season == 0:
                            # Two OVA batches both numbered from 1. Specials
                            # have no canonical order anyway, so the next free
                            # slot is as good a place as any.
                            probe = episode
                            while target.exists() and probe < 200:
                                probe += 1
                                target = (
                                    destination
                                    / f"{parent} - S00E{probe:02d}{suffix}"
                                )
                        else:
                            # Two different files claiming one episode: a
                            # second release of the same season, with its own
                            # encode or dub. Renumbering would lie about which
                            # episode it is, so it keeps its number and gains
                            # a suffix -- Jellyfin shows it as an alternate
                            # version of that episode rather than a new one.
                            stem = (
                                f"{parent} - S{season:02d}E{episode:02d}"
                                f" - alt{{n}}{suffix}"
                            )
                            probe = 0
                            while target.exists() and probe < 20:
                                probe += 1
                                target = destination / stem.format(n=probe)
                            duplicates.append(
                                f"S{season:02d}E{episode:02d} ({folder})"
                            )

            if target.exists():
                # Same data already in place -- the merge has nothing to do.
                skipped_same += 1
                continue
            try:
                f.rename(target)
                moved += 1
            except OSError as exc:
                problems.append(f"{f.name}: {exc}")

        # Only the now-empty shell goes. Anything left behind means a file
        # didn't move, and deleting the folder then would lose it.
        leftover = [p for p in source.rglob("*") if p.is_file()]
        if leftover:
            problems.append(
                f"{folder}: {len(leftover)} files could not be placed, folder kept"
            )
            continue
        try:
            shutil.rmtree(source)
        except OSError as exc:
            problems.append(f"{folder}: {exc}")

    return MergeResult(moved, skipped_same, duplicates, problems)


def _video_files(source: Path) -> tuple[list[Path], list[Path]]:
    """Split a download's videos into (episodes, extras)."""
    if source.is_file():
        if source.suffix.lower() not in VIDEO_SUFFIXES:
            return [], []
        return ([], [source]) if is_extra(source.name) else ([source], [])

    episodes: list[Path] = []
    extras: list[Path] = []
    for p in sorted(source.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        floor = (
            MIN_SPECIAL_BYTES
            if is_special(p.name, _relative_to(p, source))
            else MIN_VIDEO_BYTES
        )
        if p.stat().st_size < floor:
            continue
        (extras if is_extra(p.name) else episodes).append(p)
    return episodes, extras


def existing_folder(media_root: Path, title: str) -> str | None:
    """The library folder already holding this show, if there is one.

    Matching ignores spacing and punctuation, so a release that cleans up as
    "YuruYuri" joins the "Yuru Yuri" folder rather than starting a rival one
    next to it.

    A release also names the show at full length where the library uses the
    short form -- "Re Zero kara Hajimeru Isekai Seikatsu" against a folder
    called "Re Zero". A folder whose key is a prefix of the title's counts,
    so the long name joins the short folder instead of forking off. The
    longest such folder wins, so "Re Zero" doesn't swallow a release that
    matches "Re Zero Shorts" exactly.
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

    # Nothing exact. Fall back to a prefix match in either direction: the
    # release may name the show longer than the folder does ("Re Zero kara
    # Hajimeru Isekai Seikatsu" vs "Re Zero") or shorter, depending on which
    # season arrived first. Only reasonably long keys, since a four-letter
    # prefix would match half the library.
    prefixes = [
        name
        for name in entries
        if len(match_key(name)) >= 6
        and (
            key.startswith(match_key(name)) or match_key(name).startswith(key)
        )
    ]
    if prefixes:
        # Longest wins, so "Re Zero" doesn't swallow a release that matches
        # "Re Zero Shorts" exactly.
        return max(prefixes, key=lambda n: len(match_key(n)))
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

    Creditless openings, adverts and art galleries are filed under `extras/`
    rather than as episodes -- they parse as episode numbers ("Art Design 1")
    and would otherwise overwrite the real ones.
    """
    title, stated_season = title_and_season(release_name)
    if kind != "movies":
        candidates = [title, *title_candidates(release_name)]

        # A sequel is posted under its own name ("Yuru Yuri San Hai") while
        # the same listing also carries the bare series name ("Yuruyuri").
        # If the library already has a folder under any of the names this
        # release offers, that folder is the show -- put the season there
        # rather than starting a second entry for the same series.
        for candidate in candidates:
            match = existing_folder(media_root, candidate)
            if match:
                title = match
                break
        else:
            # Nothing on disk yet, so this release names the folder. Prefer a
            # Latin name over a Cyrillic one: the other seasons will arrive
            # under their Latin titles and have to find this folder, and
            # metadata providers only know the show by that name anyway.
            if not re.search(r"[A-Za-z]{3,}", title):
                latin = next(
                    (c for c in candidates if re.search(r"[A-Za-z]{3,}", c)), None
                )
                if latin:
                    title = latin
    files, extras = _video_files(source)
    if not files and not extras:
        return ImportResult(title, media_root, 0, 0)

    subtitles = _subtitle_files(source)
    linked = skipped = 0

    if kind == "movies":
        if not files:
            return ImportResult(title, media_root, 0, 0)
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
        # A special shipped inline with the season ("... Sp 1 серия") numbers
        # itself from 1 and would land on top of the real first episode.
        # Season 00 is where it belongs and where nothing competes with it.
        if is_special(f.name, _relative_to(f, source)):
            parsed = (0, parsed[1] if parsed else 1)
        # "Yuru Yuri ss2 - 05.mkv" numbers the episode but not the season;
        # the season came from the release title, so put it back.
        elif parsed is not None and stated_season is not None and parsed[0] == 1:
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
            # Two files claiming one slot: take the next free number rather
            # than dropping one of them. Silently skipping is how the season 1
            # special disappeared without ever appearing in the library.
            if target.exists() and not _same_file(target, f):
                probe = episode
                while target.exists() and probe < 200:
                    probe += 1
                    target = destination / (
                        f"{title} - S{season:02d}E{probe:02d}{f.suffix}"
                    )

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

    # "extras" is the folder name Jellyfin recognises for bonus features: it
    # shows them on the series page without ever treating one as an episode.
    if extras:
        bonus = show_root / "extras"
        bonus.mkdir(parents=True, exist_ok=True)
        for f in extras:
            target = bonus / f.name
            if target.exists():
                skipped += 1
                continue
            try:
                target.hardlink_to(f)
                linked += 1
            except OSError:
                log.exception("could not hardlink extra %s", f)
                skipped += 1

    return ImportResult(title, show_root, linked, skipped)
