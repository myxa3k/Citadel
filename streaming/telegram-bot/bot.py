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

from clients import JellyfinClient, ProwlarrClient, QBittorrentClient, Release
from ranking import Scored, rank

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
    save_paths: dict[str, str] = field(default_factory=dict)

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
            save_paths={
                "anime": os.environ.get("PATH_ANIME", "/data/downloads/anime"),
                "series": os.environ.get("PATH_SERIES", "/data/downloads/series"),
                "movies": os.environ.get("PATH_MOVIES", "/data/downloads/movies"),
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
        "/help — эта справка",
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
    for t in torrents[:15]:
        pct = (t.get("progress") or 0) * 100
        speed = (t.get("dlspeed") or 0) / (1024**2)
        state = t.get("state", "?")
        eta = t.get("eta") or 0
        eta_txt = f"{eta // 60} мин" if 0 < eta < 8640000 else "—"
        lines.append(
            f"• {html.escape((t.get('name') or '?')[:60])}\n"
            f"   {pct:.1f}%  •  {speed:.1f} MB/s  •  {html.escape(state)}  •  ⏱ {eta_txt}"
        )

    await update.effective_message.reply_text(
        "📥 <b>Загрузки</b>\n\n" + "\n".join(lines), parse_mode=ParseMode.HTML
    )


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

    prowlarr: ProwlarrClient = context.bot_data["prowlarr"]
    try:
        releases = await prowlarr.search(query)
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

    # Only listen to the configured chat: the bot token is shared with the
    # infrastructure notifier, so it can be messaged from anywhere.
    chat_filter = filters.Chat(chat_id=config.chat_id)

    app.add_handler(CommandHandler("start", cmd_start, filters=chat_filter))
    app.add_handler(CommandHandler("help", cmd_start, filters=chat_filter))
    app.add_handler(CommandHandler("search", cmd_search, filters=chat_filter))
    app.add_handler(CommandHandler("status", cmd_status, filters=chat_filter))
    app.add_handler(CallbackQueryHandler(on_page, pattern=r"^page:"))
    app.add_handler(CallbackQueryHandler(on_pick, pattern=r"^pick:"))
    app.add_handler(CallbackQueryHandler(on_go, pattern=r"^go:"))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & chat_filter, on_search)
    )
    app.add_error_handler(on_error)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
