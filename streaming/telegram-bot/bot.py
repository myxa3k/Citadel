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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
    SonarrClient,
    TorrentNotAdded,
)
from importer import import_download
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
    sonarr_url: str | None
    sonarr_key: str | None
    sonarr_profile_id: int
    save_paths: dict[str, str] = field(default_factory=dict)
    media_paths: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Config":
        raw_users = os.environ.get("ALLOWED_USER_IDS", "").strip()
        allowed = {int(u) for u in raw_users.split(",") if u.strip()}

        thread = os.environ.get("TELEGRAM_TOPIC_STREAMING", "").strip()
        jellyfin_url = os.environ.get("JELLYFIN_URL", "").strip() or None
        jellyfin_key = os.environ.get("JELLYFIN_API_KEY", "").strip() or None
        sonarr_url = os.environ.get("SONARR_URL", "").strip() or None
        sonarr_key = os.environ.get("SONARR_API_KEY", "").strip() or None

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
            sonarr_url=sonarr_url,
            sonarr_key=sonarr_key,
            sonarr_profile_id=int(os.environ.get("SONARR_PROFILE_ID", "1")),
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
        nav.append(InlineKeyboardButton("◀️ назад", callback_data=f"page:{key}:{page - 1}"))
    if start + PAGE_SIZE < len(items):
        nav.append(InlineKeyboardButton("вперёд ▶️", callback_data=f"page:{key}:{page + 1}"))
    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(rows)


def _results_text(query: str, items: list[Scored], page: int) -> str:
    start = page * PAGE_SIZE
    chunk = items[start : start + PAGE_SIZE]
    total_pages = (len(items) + PAGE_SIZE - 1) // PAGE_SIZE

    header = (
        f"🔍 <b>{html.escape(query)}</b> — найдено {len(items)}"
        f"  (стр. {page + 1}/{total_pages})\n\n"
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
        "Ищу по всем трекерам — название можно на русском или английском.\n\n"
        "<b>/search Синий экзорцист</b>\n"
        "<b>/search Blue Exorcist</b>\n\n"
        "Дальше выбираешь релиз кнопкой, указываешь куда положить — "
        "и оно качается. Готовое появится в Jellyfin.\n\n"
        "Команды:\n"
        "/search &lt;название&gt; — искать\n"
        "/status — что сейчас качается\n"
        "/cancel — отменить загрузку\n"
        "/help — эта справка\n\n"
        "🌱 в списке — сколько человек раздаёт. Ноль означает, что "
        "скачать не получится, сколько ни жди.",
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
        await update.effective_message.reply_text(f"qBittorrent не отвечает: {exc}")
        return

    if not torrents:
        await update.effective_message.reply_text("Сейчас ничего не качается.")
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

    text = "📥 <b>Загрузки</b>\n\n" + "\n".join(lines)
    if stuck:
        # Otherwise a dead torrent just sits at 0% forever with no explanation
        # of why, which looks identical to "still starting up".
        text += (
            "\n\n💀 — раздачу никто не раздаёт, скачать её нельзя.\n"
            "Отменить: /cancel — и выбери другой релиз, с сидами."
        )

    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


def _describe(
    torrent: dict[str, Any], seeds: int, swarm_seeds: int, speed: float, eta: int
) -> tuple[str, str]:
    """Turn qBittorrent's state name into something worth reading."""
    state = torrent.get("state", "")
    progress = torrent.get("progress") or 0

    if progress >= 1:
        return "✅ готово", " • раздаётся" if state.endswith("UP") else ""
    if state in {"pausedDL", "stoppedDL"}:
        return "⏸ на паузе", ""
    if state in {"metaDL", "checkingDL", "allocating"}:
        return "⏳ подготовка", ""
    if speed > 0:
        eta_txt = f" • ⏱ {eta // 60} мин" if 0 < eta < 8640000 else ""
        return f"⬇️ {speed:.1f} MB/s", f" • 🌱 {seeds}{eta_txt}"
    # Not moving. Whether that's fatal depends on the swarm, not on us:
    # no seeders anywhere means it can never finish, while seeders that
    # exist but aren't connected yet usually resolve on their own.
    if swarm_seeds == 0:
        return "💀 нет раздающих", ""
    return "⏳ ищу пиров", f" • 🌱 {swarm_seeds} в сети"


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/search <name>` -- the explicit form, needed because the bot keeps
    Telegram's group privacy mode on and so never sees plain group messages."""
    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.effective_message.reply_text(
            "Напиши, что искать: <code>/search Синий экзорцист</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    await _do_search(update, context, query)


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
        await update.effective_message.reply_text(f"qBittorrent не отвечает: {exc}")
        return

    # A finished torrent is seeding, not downloading -- cancelling it would
    # mean deleting something already watchable, which isn't what /cancel is.
    pending = [t for t in torrents if (t.get("progress") or 0) < 1]
    if not pending:
        await update.effective_message.reply_text("Нечего отменять — всё скачано.")
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
        "Что отменить?", reply_markup=InlineKeyboardMarkup(rows)
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
        await query_cb.edit_message_text(f"Не удалось отменить: {exc}")
        return

    await query_cb.edit_message_text("❌ Отменено и удалено.")


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

    status = await update.effective_message.reply_text(f"🔍 Ищу «{query}»…")

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
            f"🔍 Ищу «{query}»…\n<i>также: {html.escape(', '.join(names[1:]))}</i>",
            parse_mode=ParseMode.HTML,
        )

    prowlarr: ProwlarrClient = context.bot_data["prowlarr"]
    try:
        releases = await prowlarr.search_many(names)
    except Exception as exc:  # noqa: BLE001
        log.exception("search failed")
        await status.edit_text(f"Поиск не удался: {exc}")
        return

    if not releases:
        await status.edit_text(
            f"По «{html.escape(query)}» ничего не нашлось.\n"
            "Попробуй другое написание — например, русское название вместо английского.",
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
        await query_cb.edit_message_text("Этот поиск устарел — поищи заново.")
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
                InlineKeyboardButton("🎌 Аниме", callback_data=f"go:{key}:{index}:anime"),
                InlineKeyboardButton("📺 Сериал", callback_data=f"go:{key}:{index}:series"),
                InlineKeyboardButton("🎬 Фильм", callback_data=f"go:{key}:{index}:movies"),
            ]
        ]
    )


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query_cb = update.callback_query
    await query_cb.answer()

    _, key, index_raw = query_cb.data.split(":", 2)
    items = SEARCHES.get(key)
    if not items:
        await query_cb.edit_message_text("Этот поиск устарел — поищи заново.")
        return

    index = int(index_raw)
    item = items[index]
    r = item.release

    await query_cb.edit_message_text(
        f"Выбрано:\n\n<b>{html.escape(r.title[:200])}</b>\n\n"
        f"{' '.join(item.labels)}  •  {r.size_gb:.1f} GB  •  🌱 {r.seeders}\n\n"
        "Куда положить?",
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
        await query_cb.edit_message_text("Этот поиск устарел — поищи заново.")
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
            "Уже скачано — этот релиз есть в загрузках под другим названием.\n"
            "Смотри в Jellyfin, или /status чтобы проверить.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("failed to queue torrent")
        await query_cb.edit_message_text(f"Не удалось поставить на закачку: {exc}")
        return

    await query_cb.edit_message_text(
        f"✅ Поставлено в очередь\n\n<b>{html.escape(release.title[:200])}</b>\n"
        f"📁 {html.escape(savepath or '?')}\n\n"
        "Прогресс — /status",
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
                f"🎬 <b>{html.escape(result.title)}</b> готово\n"
                f"{result.linked} файлов добавлено в библиотеку — можно смотреть в Jellyfin."
            ),
            parse_mode=ParseMode.HTML,
        )

        if kind != "movies":
            await _offer_sonarr(context, config, result.title, media_root)


async def _offer_sonarr(
    context: ContextTypes.DEFAULT_TYPE,
    config: Config,
    title: str,
    media_root: str,
) -> None:
    """Propose registering the show in Sonarr, so Bazarr can fetch subtitles.

    Deliberately a question rather than an automatic add: Sonarr looks titles
    up in TheTVDB, and for a recent or oddly-named show the top hit can be a
    completely unrelated series. Adding that silently would have Bazarr
    downloading subtitles for the wrong programme, which is worse than having
    none.
    """
    sonarr: SonarrClient | None = context.bot_data.get("sonarr")
    if sonarr is None:
        return

    try:
        matches = await sonarr.lookup(title, limit=3)
        known = await sonarr.existing_tvdb_ids()
    except Exception:  # noqa: BLE001
        log.exception("sonarr lookup failed for %s", title)
        return

    matches = [m for m in matches if m.get("tvdbId") not in known]
    if not matches:
        return

    key = f"{len(PENDING_SONARR)}:{title}"
    PENDING_SONARR[key] = (matches, media_root)

    rows = [
        [
            InlineKeyboardButton(
                f"✅ {m.get('title', '?')[:40]} ({m.get('year') or '?'})",
                callback_data=f"sonarr:{key}:{i}",
            )
        ]
        for i, m in enumerate(matches)
    ]
    rows.append([InlineKeyboardButton("✖️ не надо", callback_data=f"sonarr:{key}:x")])

    await context.bot.send_message(
        chat_id=config.chat_id,
        message_thread_id=config.thread_id,
        text=(
            f"Добавить <b>{html.escape(title)}</b> в Sonarr?\n"
            "Тогда подтянутся субтитры и новые серии будут качаться сами.\n\n"
            "<i>Проверь, что это тот сериал:</i>"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


# Pending Sonarr confirmations, keyed per prompt. In memory only: if the bot
# restarts before you answer, the show is still in the library and watchable
# -- only the subtitle wiring is missed, and /sonarr can redo it.
PENDING_SONARR: dict[str, tuple[list[dict[str, Any]], str]] = {}


async def on_sonarr_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query_cb = update.callback_query
    await query_cb.answer()

    _, key, choice = query_cb.data.split(":", 2)
    entry = PENDING_SONARR.pop(key, None)
    if entry is None:
        await query_cb.edit_message_text("Это предложение устарело.")
        return

    if choice == "x":
        await query_cb.edit_message_text("Ок, не добавляю.")
        return

    matches, media_root = entry
    series = matches[int(choice)]
    config: Config = context.bot_data["config"]
    sonarr: SonarrClient = context.bot_data["sonarr"]

    try:
        added = await sonarr.add_series(
            series, media_root, config.sonarr_profile_id
        )
        await sonarr.rescan(added["id"])
    except Exception as exc:  # noqa: BLE001
        log.exception("failed to add series to sonarr")
        await query_cb.edit_message_text(f"Не получилось добавить: {exc}")
        return

    await query_cb.edit_message_text(
        f"✅ <b>{html.escape(series.get('title', '?'))}</b> добавлен в Sonarr.\n"
        "Bazarr подтянет субтитры в течение нескольких минут.",
        parse_mode=ParseMode.HTML,
    )


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
    if config.sonarr_url and config.sonarr_key:
        app.bot_data["sonarr"] = SonarrClient(config.sonarr_url, config.sonarr_key)

    # Only listen to the configured chat: the bot token is shared with the
    # infrastructure notifier, so it can be messaged from anywhere.
    chat_filter = filters.Chat(chat_id=config.chat_id)

    app.add_handler(CommandHandler("start", cmd_start, filters=chat_filter))
    app.add_handler(CommandHandler("help", cmd_start, filters=chat_filter))
    app.add_handler(CommandHandler("search", cmd_search, filters=chat_filter))
    app.add_handler(CommandHandler("status", cmd_status, filters=chat_filter))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=chat_filter))
    app.add_handler(CallbackQueryHandler(on_delete, pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(on_sonarr_choice, pattern=r"^sonarr:"))
    app.add_handler(CallbackQueryHandler(on_page, pattern=r"^page:"))
    app.add_handler(CallbackQueryHandler(on_pick, pattern=r"^pick:"))
    app.add_handler(CallbackQueryHandler(on_go, pattern=r"^go:"))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & chat_filter, on_search)
    )
    app.add_error_handler(on_error)

    # Poll for finished downloads. A minute is frequent enough that "готово"
    # lands while you're still looking at the chat, and cheap -- it's one
    # local API call against qBittorrent.
    app.job_queue.run_repeating(check_finished, interval=60, first=20)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
