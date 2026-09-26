"""Telegram front end for searching and downloading.

The flow it exists to support:

    you type a name (in any language)
      -> the bot searches every indexer through Prowlarr
      -> it shows what came back, best matches first, labelled by language
      -> you press the one you want
      -> it goes to qBittorrent, and the bot reports progress

Nothing is chosen automatically. Sonarr/Radarr only ever search the English or
romanised title they get from TheTVDB, which is why Russian releases were
invisible to them -- this searches whatever you actually typed.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from clients import (
    JellyfinClient,
    ProwlarrClient,
    QBittorrentClient,
    Release,
    TorrentNotAdded,
)
from following import Followed, FollowStore
from importer import clean_title, import_download
from ranking import Scored, rank
from titles import alternative_titles

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("streaming-bot")

# How many results one page of buttons holds. Telegram allows more, but past
# roughly this many the titles stop being readable on a phone.
PAGE_SIZE = 8
# Category qBittorrent files these under, which is also how the bot finds its
# own downloads again when reporting status.
CATEGORY = "telegram"


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        sys.exit(f"missing required environment variable: {name}")
    return value


@dataclass
class Config:
    token: str
    chat_id: int
    thread_id: int | None
    allowed_users: set[int]
    prowlarr_url: str
    prowlarr_key: str
    qbit_url: str
    qbit_user: str
    qbit_pass: str
    jellyfin_url: str | None
    jellyfin_key: str | None
    follow_state: str
    save_paths: dict[str, str] = field(default_factory=dict)
    media_paths: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Config":
        raw_users = os.environ.get("ALLOWED_USER_IDS", "").strip()
        allowed = {int(u) for u in raw_users.split(",") if u.strip()}

        thread = os.environ.get("TELEGRAM_TOPIC_STREAMING", "").strip()
        jellyfin_url = os.environ.get("JELLYFIN_URL", "").strip() or None
        jellyfin_key = os.environ.get("JELLYFIN_API_KEY", "").strip() or None

        return cls(
            token=_env("TELEGRAM_BOT_TOKEN"),
            chat_id=int(_env("TELEGRAM_CHAT_ID")),
            thread_id=int(thread) if thread else None,
            allowed_users=allowed,
            prowlarr_url=_env("PROWLARR_URL"),
            prowlarr_key=_env("PROWLARR_API_KEY"),
            qbit_url=_env("QBITTORRENT_URL"),
            qbit_user=_env("QBITTORRENT_USER"),
            qbit_pass=_env("QBITTORRENT_PASS"),
            jellyfin_url=jellyfin_url,
            jellyfin_key=jellyfin_key,
            follow_state=os.environ.get("FOLLOW_STATE", "/state/following.json"),
            save_paths={
                "anime": os.environ.get("PATH_ANIME", "/data/downloads/anime"),
                "series": os.environ.get("PATH_SERIES", "/data/downloads/series"),
                "movies": os.environ.get("PATH_MOVIES", "/data/downloads/movies"),
            },
            media_paths={
                "anime": os.environ.get("MEDIA_ANIME", "/data/media/anime"),
                "series": os.environ.get("MEDIA_SERIES", "/data/media/series"),
                "movies": os.environ.get("MEDIA_MOVIES", "/data/media/movies"),
            },
        )


# Search results live only in memory, keyed by the message they belong to.
# A restart loses them, which is fine -- searching again is one message, and
# persisting them would mean a database for data that's stale within minutes.
SEARCHES: dict[str, list[Scored]] = {}


def _authorised(config: Config, update: Update) -> bool:
    user = update.effective_user
    if user is None:
        return False
    # An empty allow-list means "anyone in the configured chat", which is the
    # sane default for a private family group.
    if not config.allowed_users:
        return True
    return user.id in config.allowed_users


def _format_release(item: Scored, index: int) -> str:
    r = item.release
    labels = " ".join(item.labels)
    return (
        f"<b>{index}.</b> {html.escape(r.title[:110])}\n"
        f"    {labels}  •  {r.size_gb:.1f} GB  •  🌱 {r.seeders}  •  <i>{html.escape(r.indexer)}</i>"
    )


def _results_keyboard(key: str, items: list[Scored], page: int) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    chunk = items[start : start + PAGE_SIZE]

    rows = []
    for offset, item in enumerate(chunk):
        index = start + offset
        label = f"{index + 1}. {item.release.title[:40]}"
        rows.append(
            [InlineKeyboardButton(label, callback_data=f"pick:{key}:{index}")]
        )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ back", callback_data=f"page:{key}:{page - 1}"))
    if start + PAGE_SIZE < len(items):
        nav.append(InlineKeyboardButton("next ▶️", callback_data=f"page:{key}:{page + 1}"))
    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(rows)


def _results_text(query: str, items: list[Scored], page: int) -> str:
    start = page * PAGE_SIZE
    chunk = items[start : start + PAGE_SIZE]
    total_pages = (len(items) + PAGE_SIZE - 1) // PAGE_SIZE

    header = (
        f"🔍 <b>{html.escape(query)}</b> — {len(items)} found"
        f"  (page {page + 1}/{total_pages})\n\n"
    )
    body = "\n\n".join(
        _format_release(item, start + i + 1) for i, item in enumerate(chunk)
    )
    return header + body


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _authorised(config, update):
        return
    await update.effective_message.reply_text(
        "I search every tracker at once — type the name in Russian or English,\n"
        "whichever you know it by.\n\n"
        "<b>/search Blue Exorcist</b>\n"
        "<b>/search Синий экзорцист</b>\n\n"
        "Pick a release, say where it goes, and it downloads. When it's done "
        "it appears in Jellyfin on its own.\n\n"
        "<b>Downloading</b>\n"
        "/search &lt;name&gt; — find something\n"
        "/status — what's downloading\n"
        "/cancel — stop a download\n\n"
        "<b>Following</b>\n"
        "/follow &lt;name&gt; — watch for new episodes\n"
        "/new — what came out\n"
        "/following — what I'm watching\n"
        "/unfollow — stop watching\n\n"
        "<b>Library</b>\n"
        "/library — what's on the server\n"
        "/delete — remove a show and its torrent\n"
        "/disk — free space\n"
        "/cleanup — delete downloads no torrent owns\n\n"
        "🌱 is how many people are sharing. Zero means it will never "
        "download, however long you wait.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _authorised(config, update):
        return

    qbit: QBittorrentClient = context.bot_data["qbit"]
    try:
        torrents = await qbit.torrents(category=CATEGORY)
    except Exception as exc:  # noqa: BLE001 - surface any failure to the user
        log.exception("status failed")
        await update.effective_message.reply_text(f"qBittorrent is not responding: {exc}")
        return

    if not torrents:
        await update.effective_message.reply_text("Nothing is downloading right now.")
        return

    lines = []
    stuck = False
    for t in torrents[:15]:
        pct = (t.get("progress") or 0) * 100
        speed = (t.get("dlspeed") or 0) / (1024**2)
        seeds = t.get("num_seeds") or 0
        swarm_seeds = t.get("num_complete") or 0
        eta = t.get("eta") or 0
        name = html.escape((t.get("name") or "?")[:60])
        state, detail = _describe(t, seeds, swarm_seeds, speed, eta)
        if "💀" in state:
            stuck = True
        lines.append(f"• {name}\n   {pct:.1f}%  •  {state}{detail}")

    text = "📥 <b>Downloads</b>\n\n" + "\n".join(lines)
    if stuck:
        # Otherwise a dead torrent just sits at 0% forever with no explanation
        # of why, which looks identical to "still starting up".
        text += (
            "\n\n💀 — nobody is seeding this, it cannot be downloaded.\n"
            "Cancel it with /cancel and pick a release that has seeders."
        )

    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


def _describe(
    torrent: dict[str, Any], seeds: int, swarm_seeds: int, speed: float, eta: int
) -> tuple[str, str]:
    """Turn qBittorrent's state name into something worth reading."""
    state = torrent.get("state", "")
    progress = torrent.get("progress") or 0

    if progress >= 1:
        return "✅ done", " • seeding" if state.endswith("UP") else ""
    if state in {"pausedDL", "stoppedDL"}:
        return "⏸ paused", ""
    if state in {"metaDL", "checkingDL", "allocating"}:
        return "⏳ preparing", ""
    if speed > 0:
        eta_txt = f" • ⏱ {eta // 60} min" if 0 < eta < 8640000 else ""
        return f"⬇️ {speed:.1f} MB/s", f" • 🌱 {seeds}{eta_txt}"
    # Not moving. Whether that's fatal depends on the swarm, not on us:
    # no seeders anywhere means it can never finish, while seeders that
    # exist but aren't connected yet usually resolve on their own.
    if swarm_seeds == 0:
        return "💀 no seeders", ""
    return "⏳ finding peers", f" • 🌱 {swarm_seeds} in swarm"


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/search <name>` -- the explicit form, needed because the bot keeps
    Telegram's group privacy mode on and so never sees plain group messages."""
    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.effective_message.reply_text(
            "Tell me what to look for: <code>/search Blue Exorcist</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    await _do_search(update, context, query)


async def _offer_follow(
    context: ContextTypes.DEFAULT_TYPE, config: Config, title: str
) -> None:
    """Offer to watch this show for new episodes."""
    store: FollowStore = context.bot_data["follows"]
    if store.key(title) in {store.key(s.title) for s in store.all()}:
        return

    key = _remember_pending(context, title)
    await context.bot.send_message(
        chat_id=config.chat_id,
        message_thread_id=config.thread_id,
        text=(
            f"Follow <b>{html.escape(title)}</b>?\n"
            "I will check once a day for new episodes."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔔 Follow", callback_data=f"follow:{key}"),
                    InlineKeyboardButton("✖️ No thanks", callback_data=f"follow:x"),
                ]
            ]
        ),
    )


def _remember_pending(context: ContextTypes.DEFAULT_TYPE, title: str) -> str:
    pending = context.bot_data.setdefault("pending_follow", {})
    key = str(len(pending))
    pending[key] = title
    return key


async def on_follow_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query_cb = update.callback_query
    await query_cb.answer()

    _, key = query_cb.data.split(":", 1)
    if key == "x":
        await query_cb.edit_message_text("Fine, not following.")
        return

    title = context.bot_data.get("pending_follow", {}).get(key)
    if not title:
        await query_cb.edit_message_text("This offer has expired — use /follow &lt;name&gt;")
        return

    store: FollowStore = context.bot_data["follows"]
    store.add(title, "anime")
    await query_cb.edit_message_text(
        f"🔔 Following <b>{html.escape(title)}</b>.\n"
        "List — /following, stop — /unfollow",
        parse_mode=ParseMode.HTML,
    )


async def cmd_follow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _authorised(config, update):
        return

    title = " ".join(context.args).strip() if context.args else ""
    if not title:
        await update.effective_message.reply_text(
            "What should I follow: <code>/follow Chainsmoker Cat</code>", parse_mode=ParseMode.HTML
        )
        return

    store: FollowStore = context.bot_data["follows"]
    added = store.add(title, "anime")
    await update.effective_message.reply_text(
        f"{'🔔 Now following' if added else 'Already following'} <b>{html.escape(title)}</b>.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_unfollow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _authorised(config, update):
        return

    store: FollowStore = context.bot_data["follows"]
    title = " ".join(context.args).strip() if context.args else ""
    if not title:
        shows = store.all()
        if not shows:
            await update.effective_message.reply_text("Not following anything.")
            return
        rows = [
            [InlineKeyboardButton(f"🔕 {s.title[:45]}", callback_data=f"unfollow:{i}")]
            for i, s in enumerate(shows[:10])
        ]
        context.bot_data["unfollow_list"] = [s.title for s in shows[:10]]
        await update.effective_message.reply_text(
            "Unfollow which one?", reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    removed = store.remove(title)
    await update.effective_message.reply_text(
        f"{'🔕 No longer following' if removed else 'Was not following'} "
        f"<b>{html.escape(title)}</b>.",
        parse_mode=ParseMode.HTML,
    )


async def on_unfollow_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query_cb = update.callback_query
    await query_cb.answer()

    _, index = query_cb.data.split(":", 1)
    titles = context.bot_data.get("unfollow_list", [])
    try:
        title = titles[int(index)]
    except (ValueError, IndexError):
        await query_cb.edit_message_text("List has expired — run /unfollow again.")
        return

    store: FollowStore = context.bot_data["follows"]
    store.remove(title)
    await query_cb.edit_message_text(
        f"🔕 No longer following <b>{html.escape(title)}</b>.", parse_mode=ParseMode.HTML
    )


async def cmd_following(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not _authorised(config, update):
        return

    store: FollowStore = context.bot_data["follows"]
    shows = store.all()
    if not shows:
        await update.effective_message.reply_text(
            "Not following anything. Add one: <code>/follow Name</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [
        f"• {html.escape(s.title)}"
        + (f"  <i>(checked {s.last_checked[:10]})</i>" if s.last_checked else "")
        for s in shows
    ]
    await update.effective_message.reply_text(
        "🔔 <b>Following</b>\n\n" + "\n".join(lines), parse_mode=ParseMode.HTML
    )


async def check_new_episodes(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Look for episodes of followed shows that haven't been offered yet.

    Only ever proposes -- downloading automatically would mean picking a
    release without you seeing it, which is the thing this whole setup exists
    to avoid.
    """
    config: Config = context.bot_data["config"]
    store: FollowStore = context.bot_data["follows"]
    prowlarr: ProwlarrClient = context.bot_data["prowlarr"]
    updated: list[Followed] = []

    for show in store.all():
        try:
            names = await alternative_titles(show.title)
            releases = await prowlarr.search_many(names)
        except Exception:  # noqa: BLE001 - one bad show shouldn't stop the rest
            log.exception("follow check failed for %s", show.title)
            continue

        fresh = [r for r in releases if r.title not in show.seen]
        show.last_checked = datetime.now(timezone.utc).isoformat()

        # Everything looks new the first time round, which would announce a
        # finished show's entire back catalogue. Record it silently and only
        # report what turns up after that.
        first_run = not show.seen
        for release in releases:
            show.remember(release.title)

        if not first_run and fresh:
            # Store the titles, not the releases: /new re-searches when you
            # actually ask, so the download links are fresh rather than
            # hours old and possibly expired.
            for release in fresh:
                if release.title not in show.pending:
                    show.pending.append(release.title)
            updated.append(show)

        store.update(show)

    if not updated:
        return

    # One short message naming the shows, not a wall of releases. Which
    # release to take is decided later, in /new, when you're ready to choose.
    lines = [f"• <b>{html.escape(s.title)}</b> — {len(s.pending)}" for s in updated]
    await context.bot.send_message(
        chat_id=config.chat_id,
        message_thread_id=config.thread_id,
        text=(
            "🆕 <b>New episodes</b>\n\n"
            + "\n".join(lines)
            + "\n\nSee them with /new"
        ),
        parse_mode=ParseMode.HTML,
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows with episodes found since you last looked."""
    config: Config = context.bot_data["config"]
    if not _authorised(config, update):
        return

    store: FollowStore = context.bot_data["follows"]
    waiting = [s for s in store.all() if s.has_news]
    if not waiting:
        await update.effective_message.reply_text(
            "Nothing new. Following: /following"
        )
        return

    context.bot_data["new_list"] = [s.title for s in waiting]
    rows = [
        [
            InlineKeyboardButton(
                f"{s.title[:38]} ({len(s.pending)})", callback_data=f"new:{i}"
            )
        ]
        for i, s in enumerate(waiting[:10])
    ]
    await update.effective_message.reply_text(
        "🆕 <b>Something new</b>\n\nPick one:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_new_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query_cb = update.callback_query
    await query_cb.answer()

    _, index = query_cb.data.split(":", 1)
    titles = context.bot_data.get("new_list", [])
    try:
        title = titles[int(index)]
    except (ValueError, IndexError):
        await query_cb.edit_message_text("List has expired — run /new again.")
        return

    store: FollowStore = context.bot_data["follows"]
    show = next((s for s in store.all() if s.title == title), None)
    if show is None or not show.pending:
        await query_cb.edit_message_text("Nothing new for this one any more.")
        return

    await query_cb.edit_message_text(f"🔍 Looking for new “{html.escape(title)}”…",
                                     parse_mode=ParseMode.HTML)

    # Search again rather than replaying what the daily check found: those
    # results are hours old, and Prowlarr's download links are signed per
    # request, so a stored one may no longer work.
    prowlarr: ProwlarrClient = context.bot_data["prowlarr"]
    try:
        releases = await prowlarr.search_many(await alternative_titles(title))
    except Exception as exc:  # noqa: BLE001
        log.exception("re-search failed for %s", title)
        await query_cb.edit_message_text(f"Search failed: {exc}")
        return

    wanted = {t for t in show.pending}
    fresh = [r for r in releases if r.title in wanted]
    if not fresh:
        # The releases vanished between the daily check and now -- rare, but
        # possible on a tracker that prunes. Don't leave them queued forever.
        show.pending.clear()
        store.update(show)
        await query_cb.edit_message_text(
            "Those releases are gone now. Try /search."
        )
        return

    ranked = rank(fresh)
    key = str(query_cb.message.message_id)
    SEARCHES[key] = ranked
    context.bot_data.setdefault("queries", {})[key] = title

    # Clearing now means /new won't keep offering the same episodes. They're
    # already in `seen`, so a later check won't re-announce them either.
    show.pending.clear()
    store.update(show)

    await query_cb.edit_message_text(
        _results_text(title, ranked, 0),
        parse_mode=ParseMode.HTML,
        reply_markup=_results_keyboard(key, ranked, 0),
        disable_web_page_preview=True,
    )


def _library_entries(config: Config) -> list[tuple[str, str, int, int]]:
    """Every show in the library: (kind, title, file count, bytes)."""
    entries: list[tuple[str, str, int, int]] = []
    for kind, root in config.media_paths.items():
        base = Path(root)
        if not base.is_dir():
            continue
        for item in sorted(base.iterdir()):
            if not item.is_dir():
                continue
            files = [f for f in item.rglob("*") if f.is_file()]
            # Hardlinked files are counted once each here even though they
            # share blocks with the download -- what matters to the reader is
            # how big the show is, not how the filesystem stores it.
            size = sum(f.stat().st_size for f in files)
            entries.append((kind, item.name, len(files), size))
    return entries


def _human(size: int) -> str:
    gb = size / (1024**3)
    return f"{gb:.1f} GB" if gb >= 1 else f"{size / (1024**2):.0f} MB"


async def cmd_library(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """What's on the server, grouped by kind."""
    config: Config = context.bot_data["config"]
    if not _authorised(config, update):
        return

    entries = await asyncio.to_thread(_library_entries, config)
    if not entries:
        await update.effective_message.reply_text("The library is empty.")
        return

    lines: list[str] = []
    for kind in ("anime", "series", "movies"):
        group = [e for e in entries if e[0] == kind]
        if not group:
            continue
        total = sum(e[3] for e in group)
        lines.append(f"\n<b>{kind.title()}</b> — {len(group)}, {_human(total)}")
        lines.extend(
            f"  • {html.escape(title)} ({files})" for _, title, files, _ in group
        )

    grand = sum(e[3] for e in entries)
    await update.effective_message.reply_text(
        f"📚 <b>Library</b> — {_human(grand)} total\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def cmd_disk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Free space, and what the downloads are costing."""
    config: Config = context.bot_data["config"]
    if not _authorised(config, update):
        return

    usage = await asyncio.to_thread(shutil.disk_usage, "/data")
    used_pct = usage.used / usage.total * 100

    qbit: QBittorrentClient = context.bot_data["qbit"]
    try:
        torrents = await qbit.torrents()
        seeding = sum(t.get("size") or 0 for t in torrents if (t.get("progress") or 0) >= 1)
        count = len(torrents)
    except Exception:  # noqa: BLE001 - disk figures are still worth showing
        seeding, count = 0, 0

    bar_filled = int(used_pct / 5)
    bar = "█" * bar_filled + "░" * (20 - bar_filled)

    await update.effective_message.reply_text(
        f"💾 <b>Disk</b>\n\n"
        f"<code>{bar}</code> {used_pct:.0f}%\n\n"
        f"Free: <b>{_human(usage.free)}</b> of {_human(usage.total)}\n"
        f"Torrents: {count}, {_human(seeding)} seeding\n\n"
        "<i>Seeding files are shared with the library — deleting a show "
        "frees space only once its torrent goes too.</i>",
        parse_mode=ParseMode.HTML,
    )


def _orphans(download_roots: list[str], managed: set[str]) -> list[tuple[Path, int]]:
    """Files under the download folders that no torrent accounts for.

    These appear when a torrent is removed without its data -- from the
    qBittorrent UI, or because it predates this bot entirely. Nothing tracks
    them afterwards, so they sit there consuming the disk while /disk reports
    only what is actually seeding.
    """
    # A torrent's content_path is the file or folder it owns. Reduce each to
    # its top-level entry under the download root, since that is the unit
    # that would be deleted.
    protected: set[str] = set()
    for path in managed:
        for root in download_roots:
            if path.startswith(root.rstrip("/") + "/"):
                rest = path[len(root.rstrip("/")) + 1 :]
                protected.add(rest.split("/", 1)[0])

    found: list[tuple[Path, int]] = []
    for root in download_roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            if entry.name in protected:
                continue
            # The per-category folders are ours and are meant to be empty.
            if entry.is_dir() and not any(entry.iterdir()):
                continue
            size = (
                sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
                if entry.is_dir()
                else entry.stat().st_size
            )
            found.append((entry, size))

    return sorted(found, key=lambda item: -item[1])


async def cmd_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Find downloads no torrent is responsible for any more."""
    config: Config = context.bot_data["config"]
    if not _authorised(config, update):
        return

    qbit: QBittorrentClient = context.bot_data["qbit"]
    try:
        managed = await qbit.managed_paths()
    except Exception as exc:  # noqa: BLE001
        log.exception("cleanup listing failed")
        await update.effective_message.reply_text(
            f"qBittorrent is not responding: {exc}"
        )
        return

    roots = list(dict.fromkeys(config.save_paths.values()))
    # Also scan the parent, since anything downloaded before the per-category
    # folders existed sits directly in downloads/.
    roots += [str(Path(r).parent) for r in roots]
    roots = list(dict.fromkeys(roots))

    orphans = await asyncio.to_thread(_orphans, roots, managed)
    if not orphans:
        await update.effective_message.reply_text(
            "Nothing to clean up — every download belongs to a torrent."
        )
        return

    total = sum(size for _, size in orphans)
    context.bot_data["cleanup_list"] = orphans

    preview = "\n".join(
        f"  • {html.escape(path.name[:46])} — {_human(size)}"
        for path, size in orphans[:8]
    )
    more = f"\n  …and {len(orphans) - 8} more" if len(orphans) > 8 else ""

    await update.effective_message.reply_text(
        f"🧹 <b>{len(orphans)} orphaned downloads</b> — {_human(total)}\n\n"
        "No torrent is seeding these; they are left over from torrents "
        "removed without their files.\n\n"
        f"{preview}{more}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"🗑 Delete all ({_human(total)})", callback_data="cleanup:yes"
                    )
                ],
                [InlineKeyboardButton("✖️ Keep", callback_data="cleanup:no")],
            ]
        ),
    )


async def on_cleanup_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query_cb = update.callback_query
    await query_cb.answer()

    if query_cb.data == "cleanup:no":
        await query_cb.edit_message_text("Kept.")
        return

    orphans = context.bot_data.pop("cleanup_list", None)
    if not orphans:
        await query_cb.edit_message_text("That list has expired — run /cleanup again.")
        return

    await query_cb.edit_message_text("🧹 Deleting…")

    freed = 0
    failed = 0
    for path, size in orphans:
        try:
            if path.is_dir():
                await asyncio.to_thread(shutil.rmtree, path)
            else:
                await asyncio.to_thread(path.unlink)
            freed += size
        except OSError:
            log.exception("could not remove %s", path)
            failed += 1

    note = f"\n{failed} could not be removed — see the log." if failed else ""
    await query_cb.edit_message_text(
        f"🧹 Freed <b>{_human(freed)}</b>.{note}", parse_mode=ParseMode.HTML
    )


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a show: library files, the torrent, and the download."""
    config: Config = context.bot_data["config"]
    if not _authorised(config, update):
        return

    entries = await asyncio.to_thread(_library_entries, config)
    if not entries:
        await update.effective_message.reply_text("The library is empty.")
        return

    wanted = " ".join(context.args).strip().lower() if context.args else ""
    if wanted:
        entries = [e for e in entries if wanted in e[1].lower()]
        if not entries:
            await update.effective_message.reply_text(
                f"Nothing in the library matches “{html.escape(wanted)}”.",
                parse_mode=ParseMode.HTML,
            )
            return

    context.bot_data["delete_list"] = entries[:10]
    rows = [
        [
            InlineKeyboardButton(
                f"🗑 {title[:34]} · {_human(size)}", callback_data=f"del_show:{i}"
            )
        ]
        for i, (_, title, _, size) in enumerate(entries[:10])
    ]
    await update.effective_message.reply_text(
        "Delete which show?\n<i>Files and the torrent both go.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_delete_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query_cb = update.callback_query
    await query_cb.answer()

    _, index = query_cb.data.split(":", 1)
    entries = context.bot_data.get("delete_list", [])
    try:
        kind, title, files, size = entries[int(index)]
    except (ValueError, IndexError):
        await query_cb.edit_message_text("That list has expired — /delete again.")
        return

    # Deleting media is the one irreversible thing here, so it takes a second
    # press rather than happening on the first tap.
    context.bot_data["delete_confirm"] = (kind, title)
    await query_cb.edit_message_text(
        f"Delete <b>{html.escape(title)}</b>?\n\n"
        f"{files} files, {_human(size)}\n"
        "Library files and the matching torrent will both be removed.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🗑 Delete", callback_data="del_yes"),
                    InlineKeyboardButton("✖️ Keep", callback_data="del_no"),
                ]
            ]
        ),
    )


async def on_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query_cb = update.callback_query
    await query_cb.answer()

    if query_cb.data == "del_no":
        await query_cb.edit_message_text("Kept.")
        return

    target = context.bot_data.pop("delete_confirm", None)
    if target is None:
        await query_cb.edit_message_text("That confirmation has expired.")
        return

    kind, title = target
    config: Config = context.bot_data["config"]
    qbit: QBittorrentClient = context.bot_data["qbit"]

    removed_torrents = 0
    try:
        # Match torrents by the title the importer derived from them, so the
        # seeding copy goes too -- otherwise the disk space is never actually
        # reclaimed, since the library files are hardlinks to it.
        for torrent in await qbit.torrents():
            name = torrent.get("name") or ""
            if clean_title(name).lower() == title.lower():
                await qbit.delete(torrent.get("hash", ""), delete_files=True)
                removed_torrents += 1
    except Exception:  # noqa: BLE001 - carry on and still remove the library copy
        log.exception("could not remove torrents for %s", title)

    path = Path(config.media_paths[kind]) / title
    try:
        await asyncio.to_thread(shutil.rmtree, path)
    except OSError as exc:
        log.exception("could not delete %s", path)
        await query_cb.edit_message_text(f"Could not delete the files: {exc}")
        return

    store: FollowStore = context.bot_data["follows"]
    store.remove(title)

    jellyfin: JellyfinClient | None = context.bot_data.get("jellyfin")
    if jellyfin is not None:
        try:
            await jellyfin.refresh_library()
        except Exception:  # noqa: BLE001
            log.exception("jellyfin refresh failed after delete")

    await query_cb.edit_message_text(
        f"🗑 <b>{html.escape(title)}</b> deleted.\n"
        f"Torrents removed: {removed_torrents}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show what's downloading with a button to drop each one."""
    config: Config = context.bot_data["config"]
    if not _authorised(config, update):
        return

    qbit: QBittorrentClient = context.bot_data["qbit"]
    try:
        torrents = await qbit.torrents(category=CATEGORY)
    except Exception as exc:  # noqa: BLE001
        log.exception("cancel listing failed")
        await update.effective_message.reply_text(f"qBittorrent is not responding: {exc}")
        return

    # A finished torrent is seeding, not downloading -- cancelling it would
    # mean deleting something already watchable, which isn't what /cancel is.
    pending = [t for t in torrents if (t.get("progress") or 0) < 1]
    if not pending:
        await update.effective_message.reply_text("Nothing to cancel — everything is downloaded.")
        return

    rows = [
        [
            InlineKeyboardButton(
                f"❌ {(t.get('name') or '?')[:45]}",
                callback_data=f"del:{t.get('hash')}",
            )
        ]
        for t in pending[:8]
    ]
    await update.effective_message.reply_text(
        "Cancel which one?", reply_markup=InlineKeyboardMarkup(rows)
    )


async def on_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query_cb = update.callback_query
    await query_cb.answer()

    _, torrent_hash = query_cb.data.split(":", 1)
    qbit: QBittorrentClient = context.bot_data["qbit"]
    try:
        await qbit.delete(torrent_hash, delete_files=True)
    except Exception as exc:  # noqa: BLE001
        log.exception("delete failed")
        await query_cb.edit_message_text(f"Could not cancel: {exc}")
        return

    await query_cb.edit_message_text("❌ Cancelled and removed.")


async def on_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain text in a private chat, where privacy mode doesn't apply."""
    query = (update.effective_message.text or "").strip()
    if not query:
        return
    await _do_search(update, context, query)


async def _do_search(
    update: Update, context: ContextTypes.DEFAULT_TYPE, query: str
) -> None:
    config: Config = context.bot_data["config"]
    if not _authorised(config, update):
        return

    status = await update.effective_message.reply_text(f"🔍 Searching for “{query}”…")

    # Russian trackers index the same show under its Russian name, so
    # searching only what was typed finds half of what exists -- and the
    # Russian half is the one carrying Russian audio and subtitles.
    try:
        names = await alternative_titles(query)
    except Exception:  # noqa: BLE001 - never let this block the search itself
        log.exception("alternative title lookup failed")
        names = [query]

    if len(names) > 1:
        await status.edit_text(
            f"🔍 Searching for “{query}”…\n<i>also: {html.escape(', '.join(names[1:]))}</i>",
            parse_mode=ParseMode.HTML,
        )

    prowlarr: ProwlarrClient = context.bot_data["prowlarr"]
    try:
        releases = await prowlarr.search_many(names)
    except Exception as exc:  # noqa: BLE001
        log.exception("search failed")
        await status.edit_text(f"Search failed: {exc}")
        return

    if not releases:
        await status.edit_text(
            f"Nothing found for “{html.escape(query)}”.\n"
            "Try a different spelling — the Russian title instead of the English one, say.",
            parse_mode=ParseMode.HTML,
        )
        return

    ranked = rank(releases)
    key = str(status.message_id)
    SEARCHES[key] = ranked
    context.bot_data.setdefault("queries", {})[key] = query

    await status.edit_text(
        _results_text(query, ranked, 0),
        parse_mode=ParseMode.HTML,
        reply_markup=_results_keyboard(key, ranked, 0),
        disable_web_page_preview=True,
    )


async def on_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query_cb = update.callback_query
    await query_cb.answer()

    _, key, page_raw = query_cb.data.split(":", 2)
    items = SEARCHES.get(key)
    if not items:
        await query_cb.edit_message_text("This search has expired — search again.")
        return

    page = int(page_raw)
    search_text = context.bot_data.get("queries", {}).get(key, "")
    await query_cb.edit_message_text(
        _results_text(search_text, items, page),
        parse_mode=ParseMode.HTML,
        reply_markup=_results_keyboard(key, items, page),
        disable_web_page_preview=True,
    )


def _category_keyboard(key: str, index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎌 Anime", callback_data=f"go:{key}:{index}:anime"),
                InlineKeyboardButton("📺 TV", callback_data=f"go:{key}:{index}:series"),
                InlineKeyboardButton("🎬 Film", callback_data=f"go:{key}:{index}:movies"),
            ]
        ]
    )


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query_cb = update.callback_query
    await query_cb.answer()

    _, key, index_raw = query_cb.data.split(":", 2)
    items = SEARCHES.get(key)
    if not items:
        await query_cb.edit_message_text("This search has expired — search again.")
        return

    index = int(index_raw)
    item = items[index]
    r = item.release

    await query_cb.edit_message_text(
        f"Selected:\n\n<b>{html.escape(r.title[:200])}</b>\n\n"
        f"{' '.join(item.labels)}  •  {r.size_gb:.1f} GB  •  🌱 {r.seeders}\n\n"
        "Where should it go?",
        parse_mode=ParseMode.HTML,
        reply_markup=_category_keyboard(key, index),
        disable_web_page_preview=True,
    )


async def on_go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query_cb = update.callback_query
    await query_cb.answer()

    _, key, index_raw, kind = query_cb.data.split(":", 3)
    items = SEARCHES.get(key)
    if not items:
        await query_cb.edit_message_text("This search has expired — search again.")
        return

    config: Config = context.bot_data["config"]
    qbit: QBittorrentClient = context.bot_data["qbit"]
    release: Release = items[int(index_raw)].release
    savepath = config.save_paths.get(kind)

    try:
        await qbit.add(release.download_url, category=CATEGORY, savepath=savepath)
    except TorrentNotAdded:
        # Not a failure so much as "you already have this" -- the same
        # release is indexed under both its English and Russian titles, so
        # picking the other one looks like a new download but isn't.
        log.info("torrent already present: %s", release.title)
        await query_cb.edit_message_text(
            f"ℹ️ <b>{html.escape(release.title[:200])}</b>\n\n"
            "Already downloaded — this release is in your downloads under another name.\n"
            "Watch it in Jellyfin, or check /status.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("failed to queue torrent")
        await query_cb.edit_message_text(f"Could not queue it: {exc}")
        return

    await query_cb.edit_message_text(
        f"✅ Queued\n\n<b>{html.escape(release.title[:200])}</b>\n"
        f"📁 {html.escape(savepath or '?')}\n\n"
        "Progress — /status",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


# Torrents already imported, so a completed download isn't linked again every
# time the job runs. Lost on restart, which is harmless: import_download skips
# files that already exist in the library.
IMPORTED: set[str] = set()

# qBittorrent states that mean the data is fully downloaded. It keeps seeding
# afterwards, so waiting for the torrent to stop would mean waiting forever.
DONE_STATES = {
    "uploading",
    "stalledUP",
    "queuedUP",
    "forcedUP",
    "pausedUP",
    "stoppedUP",
    "checkingUP",
}


async def check_finished(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Link finished downloads into the library and say so in the chat.

    Runs on a timer rather than off a qBittorrent webhook: qBittorrent can
    only call an external program, not post to a URL, so polling is the
    simpler mechanism that doesn't need anything installed in its container.
    """
    config: Config = context.bot_data["config"]
    qbit: QBittorrentClient = context.bot_data["qbit"]

    try:
        torrents = await qbit.torrents(category=CATEGORY)
    except Exception:  # noqa: BLE001 - a transient failure shouldn't kill the job
        log.exception("could not list torrents")
        return

    for torrent in torrents:
        key = torrent.get("hash") or ""
        if not key or key in IMPORTED:
            continue
        if torrent.get("state") not in DONE_STATES:
            continue
        if (torrent.get("progress") or 0) < 1:
            continue

        kind = _kind_for(torrent.get("save_path") or "", config)
        media_root = config.media_paths.get(kind)
        content = torrent.get("content_path") or ""
        if not media_root or not content:
            continue

        try:
            result = await asyncio.to_thread(
                import_download,
                Path(content),
                Path(media_root),
                kind,
                torrent.get("name") or Path(content).name,
            )
        except Exception:  # noqa: BLE001
            log.exception("import failed for %s", torrent.get("name"))
            IMPORTED.add(key)  # don't retry a broken one every minute
            continue

        IMPORTED.add(key)
        if result.linked == 0:
            log.info("nothing new to link for %s", result.title)
            continue

        log.info("imported %s (%d files)", result.title, result.linked)

        jellyfin: JellyfinClient | None = context.bot_data.get("jellyfin")
        if jellyfin is not None:
            try:
                await jellyfin.refresh_library()
            except Exception:  # noqa: BLE001
                log.exception("jellyfin refresh failed")

        await context.bot.send_message(
            chat_id=config.chat_id,
            message_thread_id=config.thread_id,
            text=(
                f"🎬 <b>{html.escape(result.title)}</b> is ready\n"
                f"{result.linked} files added to the library — watch in Jellyfin."
            ),
            parse_mode=ParseMode.HTML,
        )

        if kind != "movies":
            await _offer_follow(context, config, result.title)


def _kind_for(save_path: str, config: Config) -> str:
    for kind, path in config.save_paths.items():
        if save_path.rstrip("/") == path.rstrip("/"):
            return kind
    # Fall back on the folder name, which is how the save paths are built
    # anyway -- covers a torrent moved by hand in qBittorrent.
    tail = Path(save_path).name
    return tail if tail in config.media_paths else "series"


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled error", exc_info=context.error)


async def _post_init(app: Application) -> None:
    config: Config = app.bot_data["config"]

    # Populates the "/" menu next to the message box, which is the discoverable
    # list of what the bot can do -- more reliable than a pinned message, since
    # Telegram keeps it in sync itself.
    await app.bot.set_my_commands(
        [
            BotCommand("search", "Find something to download"),
            BotCommand("status", "What is downloading"),
            BotCommand("cancel", "Stop a download"),
            BotCommand("new", "New episodes of followed shows"),
            BotCommand("follow", "Watch a show for new episodes"),
            BotCommand("following", "What I am watching"),
            BotCommand("unfollow", "Stop watching a show"),
            BotCommand("library", "What is on the server"),
            BotCommand("delete", "Remove a show and its torrent"),
            BotCommand("disk", "Free space"),
            BotCommand("cleanup", "Delete downloads no torrent owns"),
            BotCommand("help", "How this works"),
        ]
    )

    where = f"chat {config.chat_id}"
    if config.thread_id:
        where += f" topic {config.thread_id}"
    log.info("bot ready, answering in %s", where)


def main() -> None:
    config = Config.from_env()

    app = Application.builder().token(config.token).post_init(_post_init).build()
    app.bot_data["config"] = config
    app.bot_data["prowlarr"] = ProwlarrClient(config.prowlarr_url, config.prowlarr_key)
    app.bot_data["qbit"] = QBittorrentClient(
        config.qbit_url, config.qbit_user, config.qbit_pass
    )
    if config.jellyfin_url and config.jellyfin_key:
        app.bot_data["jellyfin"] = JellyfinClient(
            config.jellyfin_url, config.jellyfin_key
        )
    app.bot_data["follows"] = FollowStore(Path(config.follow_state))

    # Only listen to the configured chat: the bot token is shared with the
    # infrastructure notifier, so it can be messaged from anywhere.
    chat_filter = filters.Chat(chat_id=config.chat_id)

    app.add_handler(CommandHandler("start", cmd_start, filters=chat_filter))
    app.add_handler(CommandHandler("help", cmd_start, filters=chat_filter))
    app.add_handler(CommandHandler("search", cmd_search, filters=chat_filter))
    app.add_handler(CommandHandler("status", cmd_status, filters=chat_filter))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=chat_filter))
    app.add_handler(CommandHandler("follow", cmd_follow, filters=chat_filter))
    app.add_handler(CommandHandler("unfollow", cmd_unfollow, filters=chat_filter))
    app.add_handler(CommandHandler("following", cmd_following, filters=chat_filter))
    app.add_handler(CommandHandler("new", cmd_new, filters=chat_filter))
    app.add_handler(CommandHandler("library", cmd_library, filters=chat_filter))
    app.add_handler(CommandHandler("delete", cmd_delete, filters=chat_filter))
    app.add_handler(CommandHandler("disk", cmd_disk, filters=chat_filter))
    app.add_handler(CommandHandler("cleanup", cmd_cleanup, filters=chat_filter))
    app.add_handler(CallbackQueryHandler(on_delete, pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(on_follow_choice, pattern=r"^follow:"))
    app.add_handler(CallbackQueryHandler(on_unfollow_choice, pattern=r"^unfollow:"))
    app.add_handler(CallbackQueryHandler(on_new_choice, pattern=r"^new:"))
    app.add_handler(CallbackQueryHandler(on_delete_show, pattern=r"^del_show:"))
    app.add_handler(CallbackQueryHandler(on_delete_confirm, pattern=r"^del_(yes|no)$"))
    app.add_handler(CallbackQueryHandler(on_cleanup_choice, pattern=r"^cleanup:"))
    app.add_handler(CallbackQueryHandler(on_page, pattern=r"^page:"))
    app.add_handler(CallbackQueryHandler(on_pick, pattern=r"^pick:"))
    app.add_handler(CallbackQueryHandler(on_go, pattern=r"^go:"))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & chat_filter, on_search)
    )
    app.add_error_handler(on_error)

    # Poll for finished downloads. A minute is frequent enough that "ready"
    # lands while you're still looking at the chat, and cheap -- it's one
    # local API call against qBittorrent.
    app.job_queue.run_repeating(check_finished, interval=60, first=20)

    # Followed shows, once a day. New episodes appear on a weekly schedule,
    # so checking more often would mean five sweeps of every indexer for
    # nothing -- and each sweep is one search per alternative title.
    app.job_queue.run_repeating(check_new_episodes, interval=86400, first=300)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
